"""任务编排：把列表解析、单视频 worker、HLS 下载、浏览器/域名探测组合成一条流水线。

具体职责已拆到独立模块：
- `job_state.JobState`：状态、进度、快照、视频队列、熔断计数
- `list_runner.ListRunner`：候选域名 → 列表页解析 → 有序入队派发
- `video_worker.VideoWorker`：单视频解析、下载、重试、失败记录
- `hls.HlsDownloader`：密钥/分片/解密/ffmpeg
- `browser.BrowserSession`：浏览器与上下文生命周期
- `domain.DomainProbe`：域名探测、跳转与重探

对外契约不变：`run()` 仍是同步入口（内部 asyncio.run），`ev` / `st` /
`snapshot()` / `retry_failed()` 等保留。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import traceback
from typing import Any

from app.core.exceptions import JobAborted, NewDomainError, StopJob
from app.core.models import JobConfig, JobSnapshot, VideoItem
from app.core.parsing import ParserContext
from app.core.rules import RuleManager
from app.parsers.list.browser import BrowserListParser
from app.parsers.list.static import StaticListParser
from app.parsers.play.browser import BrowserResolver
from app.parsers.play.static import StaticResolver
from app.services.browser import BrowserSession
from app.services.domain import DomainProbe
from app.services.hls import HlsDownloader
from app.services.job_state import JobState
from app.services.list_runner import ListRunner
from app.services.video_worker import VideoWorker
from app.toolkit.files import AsyncLogWriter, dbg_log, log_dir
from app.toolkit.http import AsyncHttpClient


def _echo(line: str) -> None:
    """把日志同步到终端/stdout；Windows 重定向到文件时可能是 GBK，编码失败也别中断任务。"""
    try:
        print(line, flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass


class Job:
    """一次完整的视频下载任务，在后台线程里用 asyncio 事件循环执行。"""

    def __init__(self, cfg: JobConfig):
        self.cfg = cfg
        self.rules = RuleManager()
        self.ev = threading.Event()
        self.state = JobState(cfg)
        # 日志落盘/打屏全部丢给后台线程，事件循环只负责入队
        self._logw = AsyncLogWriter(os.path.join(cfg.outdir, "_web_log.txt"), echo=_echo)
        self.http = AsyncHttpClient()
        self.browser = BrowserSession(self.http.app_config.ua)
        self.domain = DomainProbe(
            cfg, self.rules,
            get_raw=lambda url: self.http.get_raw(
                url, retries=1, timeout=20, stop=self._stop_check),
            new_page=self.browser.new_page,
            log=self.log,
            ua=self.http.app_config.ua)
        # 每部视频的分片并发额度（seg_concurrency 平均分给 workers）
        self.hls = HlsDownloader(
            cfg.outdir, cfg.seg_concurrency // max(1, cfg.workers),
            state=self.state, fetch=self._fetch, fetch_media=self._fetch_media,
            log=self.log,
            is_stopped=self.ev.is_set,
            abort_msg=lambda: self.state.abort_msg,
            domain_epoch=lambda: self.domain.epoch,
            recover=self._recover_domain)
        self.worker: VideoWorker | None = None
        self.list_runner: ListRunner | None = None
        self._tasks: list[asyncio.Task] = []
        self._video_sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---------- 兼容属性（main.py / 测试仍按原接口读取） ----------
    @property
    def st(self) -> dict[str, Any]:
        return self.state.st

    @property
    def items(self) -> list[VideoItem]:
        return self.state.items

    @property
    def total(self) -> int:
        return self.state.total

    @total.setter
    def total(self, value: int) -> None:
        self.state.total = value

    @property
    def vmap(self) -> dict[str, int]:
        return self.state.vmap

    def snapshot(self) -> JobSnapshot:
        return self.state.snapshot()

    def set_stage(self, name: str, **kw: Any) -> None:
        self.state.set_stage(name, **kw)

    def _vset(self, vi: int | None, **kw: Any) -> None:
        self.state.vset(vi, **kw)

    def _progress(self) -> None:
        self.state.progress()

    # ---------- 解析器 / 运行时 ----------
    def _ensure_parsers(self) -> None:
        """实例化内置解析器，并把它们接到列表流水线与单视频 worker 上。"""
        ctx = ParserContext(fetch=self._fetch, cfg=self.cfg.model_dump(),
                            state={"rules": self.rules})
        self._static_list = StaticListParser(ctx)
        self._browser_list = BrowserListParser(ctx)
        self._static_resolver = StaticResolver(ctx)
        self._browser_resolver = BrowserResolver(ctx)
        self.worker = VideoWorker(
            self.cfg, self.rules, self.state, self.hls,
            resolve=self._resolve, reprobe=self.domain.reprobe, log=self.log,
            stop_check=self._stop_check, is_stopped=self.ev.is_set)
        self.list_runner = ListRunner(
            self.cfg, self.rules, self.state, fetch=self._fetch,
            static_list=self._static_list, browser_list=self._browser_list,
            new_page=self.browser.new_page, domain=self.domain, log=self.log,
            is_stopped=self.ev.is_set, dispatch=self._dispatch)

    def _init_async(self) -> None:
        """在事件循环线程里初始化协程原语。"""
        self._loop = asyncio.get_running_loop()
        self._video_sem = asyncio.Semaphore(max(1, self.cfg.workers))

    def log(self, msg: str) -> None:
        self._logw.write(self.state.log_line(msg))

    # ---------- 网络 / 停止 ----------
    def _stop_check(self) -> bool:
        if self.ev.is_set():
            raise StopJob()
        if self.state.abort_msg:
            # 熔断已触发：让在跑的下载/解析立刻收手，而不是把整部视频跑完
            raise JobAborted(self.state.abort_msg)
        return False

    async def _fetch(self, url: str, *, binary: bool = False, retries: int = 3,
                     timeout: float = 30, **kw: Any) -> Any:
        self._stop_check()
        self.http.set_referer(self.cfg.base + "/")
        return await self.http.get(url, binary=binary, retries=retries,
                                   timeout=timeout, stop=self._stop_check, **kw)

    async def _fetch_media(self, url: str, query: str, **kw: Any) -> Any:
        """分片/密钥取数：先按 HLS 标准用裸 URL，失败再补播放列表查询串兜底。"""
        try:
            return await self._fetch(url, **kw)
        except (StopJob, JobAborted):
            raise
        except Exception:
            if not query or "?" in url:
                raise
            return await self._fetch(f"{url}?{query}", **kw)

    # ---------- 单视频 ----------
    def _dispatch(self, n: int, item: VideoItem) -> None:
        self._tasks.append(asyncio.create_task(self._work(n, item)))

    async def _work(self, n: int, item: VideoItem) -> None:
        assert self._video_sem is not None
        async with self._video_sem:
            if self.worker is None:
                raise RuntimeError("worker 未初始化")
            await self.worker.run(n, item)

    def _rebase_state_urls(self) -> None:
        """切换域名后同步已入队 URL（队列里的链接、vmap、后续重试都用新域名）。"""
        with self.state.lock:
            for video in self.st["videos"]:
                url = video.get("u")
                if url:
                    video["u"] = self.domain.rebase_url(url)
            for item in self.state.items:
                item.id = self.domain.rebase_url(item.id)
                if item.url:
                    item.url = self.domain.rebase_url(item.url)
            self.state.vmap = {item.id: idx for idx, item in enumerate(self.state.items)}

    async def _recover_domain(self, reason: str) -> bool:
        """分片连续失败时立即重探域名；成功则让所有在跑视频重新解析。"""
        self.log(f"分片连续失败, 立即重探域名 ({reason})")
        changed = await self.domain.reprobe(reason, force=True)
        if changed:
            self._rebase_state_urls()
            self.log(f"域名已切换, 在跑视频将重新解析: {self.cfg.base}")
        return changed

    async def _resolve(self, item: VideoItem, _depth: int = 0) -> tuple[str, str]:
        item.id = self.domain.rebase_url(item.id)
        if item.url:
            item.url = self.domain.rebase_url(item.url)
        try:
            try:
                resolved = await self._static_resolver.resolve(item, self.cfg.base)
            except (StopJob, JobAborted, NewDomainError):
                raise
            except Exception:
                async with self.browser.new_page() as (page, hits):
                    resolved = await self._browser_resolver.resolve_with_page(
                        page, hits, item, self.cfg.base)
        except NewDomainError as e:
            if _depth >= 2 or not self.domain.adopt_host(e.host):
                raise
            self.log(f"播放页提示新域名 {e.host}, 切换后重试当前视频")
            self._rebase_state_urls()
            return await self._resolve(item, _depth + 1)
        with self.state.lock:
            self.st["stages"]["play"]["resolved"] += 1
        return resolved.m3u8_url, resolved.title

    async def _hls_to_mp4(self, m3u8_url: str, name: str, mp4: str,
                          vi: int | None = None) -> float:
        """兼容旧调用：HLS 下载本体已移到 `hls.HlsDownloader`。"""
        return await self.hls.download(m3u8_url, name, mp4, vi=vi)

    # ---------- 生命周期 ----------
    def run(self) -> None:
        asyncio.run(self._async_run())

    async def _async_run(self) -> None:
        self._init_async()
        try:
            self._ensure_parsers()
            await self._run()
        except StopJob:
            await self._finish(stopped=True)
        except JobAborted as e:
            with self.state.lock:
                self.st["error"] = str(e)
            self.log(f"任务中止: {e}")
            await self._finish(error=True)
        except Exception as e:
            # 完整 traceback 落到 _server_dbg.log，界面只显示异常类型+消息。
            # 注意别把 ValueError 单独当成「参数错误」：任务启动前配置已校验，
            # 运行期出现的 ValueError 大多是代码/页面解析问题，应留下 traceback。
            with self.state.lock:
                self.st["error"] = f"{type(e).__name__}: {e}"
            self.log(f"FATAL: {type(e).__name__}: {e}")
            dbg_log(os.path.join(log_dir(), "_server_dbg.log"),
                    "JOB FATAL:\n" + traceback.format_exc())
            await self._finish(error=True)
        else:
            await self._finish()

    async def _run(self) -> None:
        self._tasks = []
        try:
            if self.list_runner is None:
                raise RuntimeError("列表解析器未初始化")
            await self.list_runner.run()
        except BaseException:
            await self._cancel_tasks(self._tasks)
            raise
        self.set_stage("list", status="done")
        self.log(f"列表解析完成: 共 {self.total} 个视频")
        try:
            with open(os.path.join(self.cfg.outdir, "manifest_web.json"), "w",
                      encoding="utf-8") as f:
                json.dump([it.model_dump() for it in self.items], f, ensure_ascii=False)
        except OSError:
            pass
        for name in ("play", "key", "seg", "dec", "out"):
            self.set_stage(name, status="running")
        await self._await_tasks(self._tasks)
        if self.state.abort_msg:
            raise JobAborted(self.state.abort_msg)
        self.state.set_percent(100)

    async def _finish(self, stopped: bool = False, error: bool = False) -> None:
        self.state.finish(stopped=stopped, error=error)
        self.log("=== 已停止 ===" if stopped else
                 ("=== 出错终止 ===" if error else "=== 任务完成 ==="))
        self.hls.kill_ffmpeg()
        await self.browser.close()
        await self.http.aclose()
        # 任务状态已经落定，等日志写完再让前端看到「已完成」
        self._logw.flush()

    def _kill_ffmpeg(self) -> None:
        self.hls.kill_ffmpeg()

    def dispose(self) -> None:
        """任务对象被新任务替换时回收后台资源（日志线程、残留 ffmpeg）。"""
        self.hls.kill_ffmpeg()
        try:
            self._logw.close()
        except Exception:
            pass

    def shutdown(self) -> None:
        """进程退出/优雅停机：停信号 + 杀 ffmpeg + 关浏览器，可从其它线程调用。"""
        self.ev.set()
        self.hls.kill_ffmpeg()
        loop = self._loop
        if loop is not None and loop.is_running() and self.browser.active:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.browser.close(), loop)
                fut.result(timeout=8)
            except Exception:
                pass
        try:
            self._logw.close()
        except Exception:
            pass

    # ---------- 任务控制 ----------
    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _await_tasks(self, tasks: list[asyncio.Task]) -> None:
        """等全部视频跑完；任一协程抛错（停止/熔断）就取消其余的，别留下后台任务。"""
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            await self._cancel_tasks(tasks)
            raise

    def retry_failed(self) -> tuple[int, str]:
        pairs, err = self.state.prepare_retry()
        if err:
            return 0, err
        self.ev.clear()
        self.domain.reset_cooldown()
        threading.Thread(target=self._retry_thread, args=(pairs,), daemon=True).start()
        return len(pairs), ""

    def _retry_thread(self, pairs: list[tuple[int, VideoItem]]) -> None:
        asyncio.run(self._retry_run(pairs))

    async def _retry_run(self, pairs: list[tuple[int, VideoItem]]) -> None:
        self._init_async()
        self.log(f"=== 一键重试: {len(pairs)} 个视频 ===")
        for name in ("play", "key", "seg", "dec", "out"):
            self.set_stage(name, status="running")
        self.state.progress()
        self._tasks = [asyncio.create_task(self._work(n, item)) for n, item in pairs]
        try:
            await self._await_tasks(self._tasks)
        except StopJob:
            await self._finish(stopped=True)
            return
        except JobAborted as e:
            with self.state.lock:
                self.st["error"] = str(e)
            await self._finish(error=True)
            return
        except Exception:
            with self.state.lock:
                self.st["error"] = traceback.format_exc()[-400:]
            await self._finish(error=True)
            return
        await self._finish()
