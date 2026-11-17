"""HLS 下载器：播放列表解析 → 密钥/IV → 分片并发下载 → 解密合并 → ffmpeg 合成。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from app.core import settings
from app.core.exceptions import DomainChanged, JobAborted, StopJob
from app.services.job_state import JobState
from app.toolkit.files import dbg_log, ffmpeg_exe, log_dir, no_window
from app.toolkit.m3u8 import (
    abs_segment,
    has_byterange,
    has_endlist,
    has_init_segment,
    key_schedule,
    media_lines,
    media_sequence,
    resolve_playlist,
)
from app.toolkit.probe import probe_media
from app.toolkit.segments import concat_segments, save_segment

_DBG_PATH = os.path.join(log_dir(), "_server_dbg.log")
# 分片缓存元数据版本：格式升级后旧目录里的 .enc 一律作废（避免混用不同指纹体系）
_PARTS_META_VERSION = 2


def _allow_no_endlist() -> bool:
    """没有 #EXT-X-ENDLIST 的播放列表（直播/滚动窗口）默认拒绝，避免把半截流当成品。"""
    return settings.allow_no_endlist()


async def _no_recover(_reason: str) -> bool:
    return False


class HlsDownloader:
    """一部视频的 HLS 下载与合成。状态写入由 `JobState` 统一负责。"""

    def __init__(
        self,
        outdir: str,
        seg_per_video: int,
        *,
        state: JobState,
        fetch: Callable[..., Awaitable[Any]],
        fetch_media: Callable[..., Awaitable[bytes]],
        log: Callable[[str], None],
        is_stopped: Callable[[], bool],
        abort_msg: Callable[[], str],
        domain_epoch: Callable[[], int] | None = None,
        recover: Callable[[str], Awaitable[bool]] | None = None,
        seg_limiter: asyncio.Semaphore | None = None,
    ):
        self.outdir = outdir
        self.seg_per_video = max(1, seg_per_video)
        # 任务级总并发闸门（所有视频共享）；None 表示只按单视频额度限制
        self.seg_limiter = seg_limiter
        self.state = state
        self._fetch = fetch
        self._fetch_media = fetch_media
        self.log = log
        self._is_stopped = is_stopped
        self._abort_msg = abort_msg
        self._epoch = domain_epoch or (lambda: 0)
        self._recover = recover or _no_recover
        self._ffmpeg_procs: set[subprocess.Popen] = set()
        self._ffmpeg_lock = threading.Lock()
        self._ffmpeg_ok = False
        self._ffmpeg_err = ""

    # ---------- 停止 ----------
    def _stopped(self) -> bool:
        return bool(self._is_stopped() or self._abort_msg())

    def _raise_if_stopped(self) -> None:
        if self._is_stopped():
            raise StopJob()
        abort = self._abort_msg()
        if abort:
            raise JobAborted(abort)

    async def _ensure_ffmpeg(self) -> None:
        """第一次下载前确认 ffmpeg 可用；失败信息缓存，避免每个视频都重试探测。"""
        if self._ffmpeg_ok:
            return
        if self._ffmpeg_err:
            raise RuntimeError(self._ffmpeg_err)
        try:
            await asyncio.to_thread(ffmpeg_exe)
        except RuntimeError as e:
            self._ffmpeg_err = str(e) or "找不到可执行的 ffmpeg"
            raise RuntimeError(self._ffmpeg_err) from e
        except Exception as e:
            self._ffmpeg_err = f"ffmpeg 不可用: {type(e).__name__}: {e}"
            raise RuntimeError(self._ffmpeg_err) from e
        self._ffmpeg_ok = True

    async def download(self, m3u8_url: str, name: str, mp4: str, vi: int | None = None) -> float:
        await self._ensure_ffmpeg()
        self._raise_if_stopped()
        self.state.set_stage("seg", current=name)
        text, final_url = await resolve_playlist(self._fetch, m3u8_url)
        if has_init_segment(text):
            # fMP4 需要先拼接 EXT-X-MAP 初始化段，当前合成链路只支持 TS 分片。
            # 这里提前给出可读错误，避免几百片下载完才在 ffmpeg 阶段报"合成失败"。
            raise RuntimeError("fMP4(EXT-X-MAP) 播放列表暂不支持")
        if has_byterange(text):
            # 同一个文件按字节区间切成多片：当前按 URL 逐片下载会把整个文件重复下 N 次。
            raise RuntimeError("EXT-X-BYTERANGE 播放列表暂不支持")
        if not has_endlist(text):
            # 直播/滚动窗口：只下当前窗口会把几分钟内容当完整视频落盘并永久跳过。
            msg = (
                "播放列表没有 #EXT-X-ENDLIST（直播/滚动窗口），无法确认是完整视频；"
                "已跳过。确需下载当前窗口可设 YS_ALLOW_NO_ENDLIST=1"
            )
            if not _allow_no_endlist():
                raise RuntimeError(msg)
            self.state.add_warning(f"[{name}] {msg}")
            self.log(f"[{name}] 警告: 播放列表没有 #EXT-X-ENDLIST, 按当前窗口下载（YS_ALLOW_NO_ENDLIST=1）")
        segs = [abs_segment(s, final_url) for s in media_lines(text)]
        if not segs:
            raise RuntimeError("no segments in m3u8")
        self.state.vset(vi, step=2)

        # 播放列表的查询串（通常是签名）：默认不给分片继承，仅在裸 URL 失败时兜底
        pl_query = urlparse(final_url).query
        # 按分片解析密钥计划：支持密钥轮换、中途转明文；没有显式 IV 时按 RFC 8216
        # 用媒体序列号派生，而不是所有分片都拿全 0 IV（那样解出来是花屏/坏流）。
        schedule = key_schedule(text)
        if len(schedule) != len(segs):
            schedule = (schedule + [("", None, "", 0)] * len(segs))[: len(segs)]
        key_uris = list(dict.fromkeys(s[0] for s in schedule if s[0]))
        keys: dict[str, bytes] = {}
        for uri in key_uris:
            data = await self._fetch_media(abs_segment(uri, final_url), pl_query, binary=True)
            if len(data) != 16:
                raise RuntimeError(f"密钥长度异常: {len(data)} 字节")
            keys[uri] = data
        seg_keys = [keys.get(s[0]) for s in schedule]
        seg_ivs = [s[1] if s[1] is not None else s[3].to_bytes(16, "big") for s in schedule]
        if key_uris:
            iv_hex = next((s[2] for s in schedule if s[2]), "")
            iv_text = f"0x{iv_hex[:8]}…" if iv_hex else f"seq {schedule[0][3]}…"
            method = "AES-128-CBC" if len(key_uris) == 1 else f"AES-128-CBC (密钥轮换 {len(key_uris)} 组)"
            self.state.set_key_info(len(key_uris), method, iv_text)
        else:
            self.state.set_stage("key", method="无加密(明文分片)")
        self.state.vset(vi, step=3, sn=len(segs), sd=0)

        ts = os.path.join(self.outdir, name + ".tmp.ts")
        parts = ts + ".parts"
        os.makedirs(parts, exist_ok=True)
        # 指纹带上 host / 媒体序列号 / ENDLIST：换域名或播放列表版本变化时旧分片不能复用，
        # 否则会把两个站点/两次直播的分片拼成一条流（花屏、错段、时长不对）。
        fingerprint = self._plan_fingerprint(
            segs,
            seg_keys,
            seg_ivs,
            host=urlparse(final_url).netloc.lower(),
            media_seq=media_sequence(text),
            endlist=has_endlist(text),
        )
        await asyncio.to_thread(self._prepare_parts, parts, name, fingerprint)
        done = [0]
        start_epoch = self._epoch()
        fail_count = [0]
        recover_tried = [False]
        recover_at = min(3, max(1, len(segs)))

        async def get_seg(i: int, url: str) -> None:
            path = os.path.join(parts, f"{i:05d}.enc")
            if not (os.path.exists(path) and os.path.getsize(path) > 0):
                self._raise_if_stopped()
                try:
                    if self.seg_limiter is not None:
                        async with self.seg_limiter:
                            data = await self._fetch_media(url, pl_query, binary=True, retries=4)
                    else:
                        data = await self._fetch_media(url, pl_query, binary=True, retries=4)
                except (StopJob, JobAborted):
                    raise
                except Exception as e:
                    fail_count[0] += 1
                    if fail_count[0] >= recover_at and not recover_tried[0]:
                        recover_tried[0] = True
                        if await self._recover(f"分片连续失败: {str(e)[:100]}"):
                            raise DomainChanged() from e
                    # 分片级失败明细：只记一次最终失败，避免把重试过程刷屏
                    self.log(f"[{name}] 分片 {i + 1}/{len(segs)} 下载失败: {str(e)[:120]}")
                    raise
                # 服务器限流时可能返回 200 + HTML 错误页：落盘前先做基本校验，
                # 否则会把错误页当成分片缓存起来，之后每次运行都复放同一批坏片。
                bad = self._bad_segment(data, bool(seg_keys[i] if i < len(seg_keys) else None))
                if bad:
                    fail_count[0] += 1
                    raise RuntimeError(f"分片 {i + 1}/{len(segs)} 内容异常: {bad}")
                await asyncio.to_thread(save_segment, path, data)
            fail_count[0] = 0
            done[0] += 1
            self.state.vset(vi, sd=done[0])

        # 固定数量的下载协程消费分片下标：几千片也不会瞬间创建几千个 Task，
        # 并发上限仍是本视频的分片额度（cfg.seg_concurrency / workers）。
        next_seg = 0

        async def seg_worker() -> None:
            nonlocal next_seg
            while True:
                if self._epoch() != start_epoch:
                    raise DomainChanged()
                self._raise_if_stopped()
                i = next_seg
                next_seg += 1
                if i >= len(segs):
                    return
                await get_seg(i, segs[i])

        workers = max(1, min(self.seg_per_video, len(segs)))
        tasks = [asyncio.create_task(seg_worker()) for _ in range(workers)]
        try:
            await asyncio.gather(*tasks)
        except BaseException as e:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 并发任务可能来不及把失败计数累到阈值就被 gather 取消；
            # 这里兜底：只要本次下载有分片最终失败，就立即重探一次域名。
            if not recover_tried[0] and fail_count[0] and not self._is_stopped() and not self._abort_msg():
                recover_tried[0] = True
                if await self._recover(f"分片失败: {str(e)[:100]}"):
                    raise DomainChanged() from e
            # 域名已切换时统一抛 DomainChanged，让 worker 丢弃旧 m3u8 重新解析
            if self._epoch() != start_epoch:
                raise DomainChanged() from e
            raise
        if self._epoch() != start_epoch:
            raise DomainChanged()
        self.state.add_seg_done(len(segs))
        self.state.vset(vi, step=4)

        self._raise_if_stopped()
        try:
            await asyncio.to_thread(concat_segments, ts, parts, len(segs), seg_keys, seg_ivs)
        except Exception:
            # 解密/合并失败通常意味着密钥或分片缓存已损坏：丢掉缓存，下次重新下载，
            # 否则每次重试都会复放同一批坏片、永远失败。
            await self._drop_parts(parts, ts)
            raise
        if not await asyncio.to_thread(self._looks_like_ts, ts):
            await self._drop_parts(parts, ts)
            raise RuntimeError("分片解密后不是 TS 流(密钥/IV 或分片缓存可能不匹配)")

        # 先写 <name>.mp4.tmp，成功后再 os.replace 到目标名：进程被杀/磁盘满时
        # 不会留下一个「看起来已完成」的半截 MP4，下次任务也不会误判为跳过。
        tmp_mp4 = mp4 + ".tmp"
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass
        # 用 to_thread + Popen：ffmpeg 是阻塞子进程，且要能被 shutdown() 杀掉
        exe, rc, detail = await asyncio.to_thread(self._ffmpeg_run, ts, tmp_mp4, self._stopped)
        if rc != 0 or not os.path.exists(tmp_mp4) or os.path.getsize(tmp_mp4) == 0:
            try:
                os.remove(tmp_mp4)
            except OSError:
                pass
            # 被停机/熔断杀掉的 ffmpeg 不是"合成失败"，别写成失败记录
            if self._is_stopped():
                raise StopJob()
            abort = self._abort_msg()
            if abort:
                raise JobAborted(abort)
            tsz = os.path.getsize(ts) if os.path.exists(ts) else 0
            kept = ""
            if tsz > 0:
                tsv = os.path.join(self.outdir, name + ".ts")
                try:
                    shutil.move(ts, tsv)
                    kept = f" | 已保留合并流: {name}.ts"
                except OSError:
                    pass
            dbg_log(_DBG_PATH, f"ffmpeg 合成失败 exe={exe} rc={rc} ts={tsz}B err={detail[:600]}")
            raise RuntimeError(
                f"ffmpeg 合成失败 rc={rc}, 合并流 {tsz / 1048576:.1f}MB, 输出: {detail[:140]}{kept}"
            )
        try:
            os.replace(tmp_mp4, mp4)
        except OSError as e:
            try:
                os.remove(tmp_mp4)
            except OSError:
                pass
            raise RuntimeError(f"MP4 落盘失败: {e}") from e
        mb = os.path.getsize(mp4) / 1048576
        # 成品校验：容器/时长都解析不出来就删掉重下，别让坏片被“已存在跳过”永久保留
        ok, why = await asyncio.to_thread(probe_media, mp4)
        if not ok:
            try:
                os.remove(mp4)
            except OSError:
                pass
            raise RuntimeError(f"成品校验未通过({name}.mp4): {why}")
        # 中间流清理失败（Windows 上文件可能被杀毒/索引占用）不应把成功的视频判失败；
        # 分片缓存可能有上千个小文件，删除放线程里，别卡住事件循环。
        try:
            os.remove(ts)
        except OSError:
            pass
        await asyncio.to_thread(shutil.rmtree, parts, ignore_errors=True)
        return mb

    @staticmethod
    def _plan_fingerprint(
        segs: list[str],
        keys: list[bytes | None],
        ivs: list[bytes],
        *,
        host: str = "",
        media_seq: int = 0,
        endlist: bool = True,
    ) -> str:
        """分片缓存指纹：分片路径 + 密钥内容 + IV + 来源 host/媒体序列号/ENDLIST。

        不把带签名的查询串算进去（每次请求都可能变），否则断点续传永远失效；
        但 host 与播放列表版本必须算进去，否则换域名/换一轮直播后会复用旧分片，
        把两次不同内容拼成一条流（花屏/错段）。
        """
        digest = hashlib.sha256()
        digest.update(f"v{_PARTS_META_VERSION}|{host.lower()}|{media_seq}|{1 if endlist else 0}|".encode())
        for url, key, iv in zip(segs, keys, ivs, strict=True):
            digest.update((urlparse(url).path or url).encode("utf-8", "replace"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(key).digest() if key else b"\0" * 32)
            digest.update(iv)
            digest.update(b"\0")
        return digest.hexdigest()

    def _prepare_parts(self, parts: str, name: str, fingerprint: str) -> None:
        meta = os.path.join(parts, "_plan.json")
        old = ""
        try:
            with open(meta, encoding="utf-8") as f:
                data = json.load(f) or {}
            if data.get("v") == _PARTS_META_VERSION:
                old = str(data.get("fp") or "")
        except (OSError, ValueError):
            old = ""
        has_parts = any(entry.endswith(".enc") for entry in os.listdir(parts))
        if has_parts and old != fingerprint:
            self.log(f"[{name}] 播放列表/密钥/域名已变化, 清空旧分片缓存")
            shutil.rmtree(parts, ignore_errors=True)
            os.makedirs(parts, exist_ok=True)
        tmp = meta + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": _PARTS_META_VERSION, "fp": fingerprint}, f)
            os.replace(tmp, meta)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    async def _drop_parts(self, parts: str, ts: str) -> None:
        """密钥/IV 或分片缓存不匹配时丢弃缓存，避免每次重试都复放同一批坏片。"""

        def _do() -> None:
            shutil.rmtree(parts, ignore_errors=True)
            try:
                os.remove(ts)
            except OSError:
                pass

        await asyncio.to_thread(_do)

    @staticmethod
    def _bad_segment(data: bytes, encrypted: bool) -> str:
        """落盘前校验分片内容；返回非空字符串表示这条分片不可用。"""
        if not data:
            return "空响应"
        head = data[:512].lstrip().lower()
        if head.startswith(b"<") or b"<html" in head or b"<!doctype" in head:
            return "返回了 HTML 页面(可能被限流/拦截)"
        if encrypted:
            if len(data) % 16:
                return f"长度 {len(data)} 不是 AES 块大小整数倍"
            return ""
        if data[0] != 0x47:
            return "不是 MPEG-TS 分片(首字节不是 0x47)"
        return ""

    @staticmethod
    def _looks_like_ts(path: str) -> bool:
        """检查合并流是否像 MPEG-TS：首尾都要落在包边界上（0x47 同步字节）。"""
        try:
            size = os.path.getsize(path)
            if size < 188:
                return False
            with open(path, "rb") as f:
                head = f.read(188)
                f.seek(-188, os.SEEK_END)
                tail = f.read(188)
        except OSError:
            return False
        if not head or head[0] != 0x47 or not tail or tail[0] != 0x47:
            return False
        # 正常 TS 合并流长度是 188 的整数倍；多出半包说明分片被截断/混入垃圾
        return size % 188 == 0

    def _ffmpeg_run(self, ts: str, mp4: str, stop: Callable[[], bool] | None = None) -> tuple[str, int, str]:
        """阻塞地跑 ffmpeg 合成（在线程里执行），返回 (exe, 返回码, 输出摘要)。

        轮询而不是一次 `communicate(timeout=1800)`：停止/熔断信号能在 ~0.5 秒内
        杀掉 ffmpeg，而不是让用户点完“停止”还要再等半小时合成结束。
        """
        kw = no_window()
        exe = ffmpeg_exe()
        detail = ""
        rc = -1
        proc = None
        timed_out = False
        stopped = False
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                [
                    exe,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    ts,
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    mp4,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kw,
            )
            with self._ffmpeg_lock:
                self._ffmpeg_procs.add(proc)
            out = err = ""
            while True:
                try:
                    out, err = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if stop is not None and stop():
                        stopped = True
                    elif time.monotonic() - started > 1800:
                        timed_out = True
                    else:
                        continue
                    try:
                        proc.kill()
                    except OSError:
                        pass
            rc = proc.returncode
            detail = (err or "").strip() or (out or "").strip()
            if stopped:
                detail = "已被停止信号终止" + (f": {detail[:200]}" if detail else "")
            elif timed_out:
                detail = "ffmpeg 合成超时(30 分钟)" + (f": {detail[:200]}" if detail else "")
            elif not detail:
                detail = "(ffmpeg 无任何输出)"
        finally:
            if proc is not None:
                with self._ffmpeg_lock:
                    self._ffmpeg_procs.discard(proc)
        return exe, rc, detail

    def kill_ffmpeg(self) -> None:
        with self._ffmpeg_lock:
            procs, self._ffmpeg_procs = list(self._ffmpeg_procs), set()
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
