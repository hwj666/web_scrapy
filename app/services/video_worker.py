"""单视频处理：跳过判断 → 播放页解析 → HLS 下载 → 重试 / 熔断 / 失败记录。"""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable

import httpx

from app.core.exceptions import DomainChanged, JobAborted, StopJob
from app.core.models import JobConfig, VideoItem
from app.core.rules import RuleManager
from app.services.hls import HlsDownloader
from app.services.job_state import JobState
from app.toolkit.files import sanitize
from app.toolkit.http import is_conn_error


class VideoWorker:
    """处理视频队列中的一项；并发额度由 `Job` 的信号量提供。"""

    def __init__(self, cfg: JobConfig, rules: RuleManager, state: JobState,
                 hls: HlsDownloader, *,
                 resolve: Callable[[VideoItem], Awaitable[tuple[str, str]]],
                 reprobe: Callable[[str], Awaitable[bool]],
                 log: Callable[[str], None],
                 stop_check: Callable[[], bool],
                 is_stopped: Callable[[], bool]):
        self.cfg = cfg
        self.rules = rules
        self.state = state
        self.hls = hls
        self._resolve = resolve
        self._reprobe = reprobe
        self.log = log
        self._stop_check = stop_check
        self._is_stopped = is_stopped

    async def run(self, n: int, item: VideoItem) -> None:
        state = self.state
        if self._is_stopped() or state.abort_msg:
            return
        vi = state.vmap.get(item.id)
        state.vset(vi, s=1)

        def exists(path: str) -> bool:
            return os.path.exists(path) and os.path.getsize(path) > 0

        name = f"{n:03d}_{sanitize(item.title or '')}"
        mp4 = os.path.join(self.cfg.outdir, name + ".mp4")
        m3u8_url: str | None = None

        # 标题以列表页为准；只有列表页没拿到标题时才提前解析播放页取标题
        need_resolve_first = not item.title
        if not need_resolve_first:
            if exists(mp4):
                self._skip(name, vi)
                self.log(f"[{n}/{state.total}] SKIP {name} (已存在)")
                return
        else:
            title = m3u8_url = None
            for attempt in (1, 2):
                try:
                    m3u8_url, title = await self._resolve(item)
                    state.vset(vi, step=1, t=title or item.id)
                    break
                except StopJob:
                    raise
                except JobAborted:
                    raise
                except Exception as e:
                    if is_conn_error(e) and await self._reprobe(f"视频 {n} 连接失败"):
                        continue
                    if attempt == 2 or not self._is_retryable(e):
                        self._fail(item, n, e, vi=vi, record=True)
                        return
                    await self._sleep(3)
            # 列表页没标题 -> 用播放页自动识别的标题兜底
            final = title or item.title
            if final:
                item.title = final
                name = f"{n:03d}_{sanitize(final)}"
                mp4 = os.path.join(self.cfg.outdir, name + ".mp4")
                if exists(mp4):
                    self._skip(name, vi)
                    self.log(f"[{n}/{state.total}] SKIP {name} (已存在)")
                    return

        try:
            free_gb = shutil.disk_usage(self.cfg.outdir).free / (1 << 30)
        except OSError:
            free_gb = 0.0
        if free_gb < self.rules.pipeline.disk_min_gb:
            self.log(f"磁盘剩余 {free_gb:.1f}GB, 不足 {self.rules.pipeline.disk_min_gb}GB, 中止任务")
            self._trip_abort(f"DISK FULL: 输出目录所在磁盘剩余 {free_gb:.1f}GB, 任务已中止")

        tries, max_tries, bonus = 0, 3, 0
        domain_retries = 0
        while tries < max_tries:
            if self._is_stopped():
                return
            tries += 1
            try:
                if m3u8_url is None:
                    m3u8_url, t2 = await self._resolve(item)
                    state.vset(vi, step=1, t=item.title or t2 or item.id)
                    if t2 and not item.title:
                        item.title = t2
                        name = f"{n:03d}_{sanitize(t2)}"
                        mp4 = os.path.join(self.cfg.outdir, name + ".mp4")
                mb = await self.hls.download(m3u8_url, name, mp4, vi=vi)
                state.record_success(name, mb)
                state.vset(vi, s=2, mb=round(mb, 1))
                state.progress()
                self.log(f"[{n}/{state.total}] OK {name} | {mb:.0f}MB")
                return
            except StopJob:
                raise
            except JobAborted:
                raise
            except DomainChanged:
                # 其他视频触发了域名切换：丢弃旧 m3u8，重新解析；不消耗正常重试次数
                domain_retries += 1
                if domain_retries > 3:
                    self._fail(item, n, RuntimeError("域名切换后仍无法恢复"), vi=vi, record=True)
                    break
                tries -= 1
                m3u8_url = None
                self.log(f"[{n}] 域名已切换, 重新解析播放页 ({domain_retries}/3)")
                await self._sleep(0.5)
                continue
            except Exception as e:
                if (is_conn_error(e) and bonus == 0
                        and await self._reprobe(f"视频 {n} 连接失败")):
                    bonus = 1
                    m3u8_url = None
                    tries -= 1
                    continue
                if tries >= max_tries or not self._is_retryable(e):
                    self._fail(item, n, e, vi=vi, record=True)
                    break
                m3u8_url = None
                self.log(f"[{n}] retry {tries}/{max_tries}: {str(e)[:100]}")
                await self._sleep(5)

    async def _sleep(self, seconds: float) -> None:
        """可被停止/熔断信号打断的等待（替代 time.sleep，别卡住事件循环）。"""
        left = seconds
        while left > 0:
            self._stop_check()
            step = min(0.2, left)
            await asyncio.sleep(step)
            left -= step

    def _skip(self, name: str, vi: int | None = None) -> None:
        self.state.record_skip()
        self.state.vset(vi, s=4)
        self.state.progress()

    def _trip_abort(self, msg: str) -> None:
        self.state.set_abort(msg)
        raise JobAborted(msg)

    def _fail(self, item: VideoItem, idx: int, e: Exception, record: bool = False,
              vi: int | None = None) -> None:
        trip = self.state.record_failure(str(e), self.rules.pipeline.streak_limit)
        self.state.vset(vi, s=3, e=str(e)[:80])
        self.state.progress()
        self.log(f"[{idx}/{self.state.total}] FAIL {item.id}: {str(e)[:140]}")
        if trip:
            self.log("熔断中止: " + trip)
        if record:
            try:
                with open(os.path.join(self.cfg.outdir, "_failed_web.txt"), "a",
                          encoding="utf-8") as f:
                    f.write(f"{item.id}\t{item.title}\t{str(e)[:140]}\n")
            except OSError:
                pass

    def _is_retryable(self, e: Exception) -> bool:
        if isinstance(e, httpx.HTTPError):
            return True
        msg = str(e)
        return not any(k in msg for k in self.rules.pipeline.non_retryable)
