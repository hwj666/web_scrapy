"""文件与外部命令工具。"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

from app.core import settings

# 本轮启动的 chromium 会带上这个标记开关，用于精确清理上次残留的浏览器进程。
# 只匹配自己起的进程，不再像旧实现那样按 *ms-playwright* 全机扫描（会误杀别的项目）。
ORPHAN_MARK = "--ys-web-orphan-guard"

_FFMPEG_OK: list[str] = []
_LOG_LOCK = threading.Lock()


def log_dir() -> str:
    """运行期日志目录：优先 YS_LOG_DIR，否则 <OUTDIR>/_logs（不写源码目录）。"""
    return settings.log_dir()


def append_log(path: str, text: str, max_bytes: int = 5 << 20) -> bool:
    """追加写日志，超过 max_bytes 轮转成 .1（运行期日志不能无限增长）。

    返回是否写入成功：调用方可以据此提示"日志文件写不进去"，而不是静默丢弃。
    """
    with _LOG_LOCK:
        try:
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            return False
    return True


class _Flush:
    """让调用方等待队列里已有的日志全部落盘。"""

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


_STOP = object()


class AsyncLogWriter:
    """后台线程写日志：调用方只入队，事件循环不再被磁盘/终端 IO 卡住。

    - 批量落盘：一次 open 写完积压的多行，减少系统调用；
    - 写失败只提示一次，输出目录不可写时不会每行刷屏；
    - `flush()` 在任务收尾时等日志落盘，`close()` 排空队列后退出线程。

    `Job` 在事件循环线程里调用 `write()`；线程只持有队列与路径，不反向引用 Job，
    因此任务对象被替换时调用 `close()` 即可回收。
    """

    def __init__(
        self, path: str, echo: Callable[[str], None] | None = None, max_bytes: int = 5 << 20
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._echo = echo
        self._q: queue.Queue[object] = queue.Queue(maxsize=5000)
        self._failed = False
        self._closed = False
        self._dropped = 0
        self._thread = threading.Thread(target=self._loop, name="ys-log-writer", daemon=True)
        self._thread.start()

    def write(self, line: str) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(line)
        except queue.Full:
            # 磁盘/终端卡住时不能无限吃内存：丢最旧的行并计数，恢复后提示一次
            self._dropped += 1

    def flush(self, timeout: float = 3.0) -> None:
        if self._closed:
            return
        marker = _Flush()
        try:
            self._q.put(marker, timeout=timeout)
        except queue.Full:
            return
        marker.event.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(_STOP)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            if isinstance(item, _Flush):
                item.event.set()
                continue
            batch = [str(item)]
            stop = False
            flush_marker: _Flush | None = None
            # 一次最多取 200 行，避免日志突发时长时间不让出线程
            for _ in range(199):
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                if isinstance(nxt, _Flush):
                    flush_marker = nxt
                    break
                batch.append(str(nxt))
            self._write(batch)
            if flush_marker is not None:
                flush_marker.event.set()
            if stop:
                return

    def _write(self, lines: list[str]) -> None:
        if self._echo is not None:
            for line in lines:
                try:
                    self._echo(line)
                except Exception:
                    pass
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            lines.append(f"[警告] 日志写入跟不上, 已丢弃 {dropped} 行（界面日志不受影响）")
        if self._failed:
            return
        try:
            ok = append_log(self.path, "".join(f"{ln}\n" for ln in lines), self.max_bytes)
        except Exception:
            ok = False
        if not ok:
            self._failed = True
            try:
                print(f"[警告] 日志文件写入失败: {self.path}（界面与终端日志不受影响）", flush=True)
            except Exception:
                pass


def ffmpeg_exe() -> str:
    if _FFMPEG_OK:
        return _FFMPEG_OK[0]
    cands = [x for x in (shutil.which("ffmpeg"), _default_ffmpeg()) if x]
    for exe in cands:
        try:
            r = subprocess.run(
                [exe, "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            if r.returncode == 0:
                _FFMPEG_OK.append(exe)
                return exe
        except Exception:
            pass
    raise RuntimeError("找不到可执行的 ffmpeg")


def _default_ffmpeg() -> str:
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def no_window() -> dict:
    """Windows 下隐藏外部进程弹窗；其它系统返回空字典，可直接 **kw 展开。"""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def sanitize(name: str, limit: int = 40) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")[:limit] or "untitled"


def dbg_log(path: str, msg: str) -> None:
    append_log(path, f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")
