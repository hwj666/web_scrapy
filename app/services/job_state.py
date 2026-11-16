"""任务状态容器：快照、进度、视频队列、日志缓冲、熔断计数。

只做「状态」这一件事：不碰网络、文件、浏览器。`Job` 及其它服务通过它读写
共享状态，避免几十个方法各自直接操作同一份 `st` 字典。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.core.models import JobConfig, JobSnapshot, VideoItem, VideoState
from app.toolkit.url import abs_url


class JobState:
    """一次任务的全部可变状态。"""

    def __init__(self, cfg: JobConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.items: list[VideoItem] = []
        self.vmap: dict[str, int] = {}
        self.total = 0
        self.ok = self.fail = self.skip = 0
        self.streak_fail = 0
        self.log_seq = 0
        self._abort_msg = ""
        pages_total = cfg.p2 - cfg.p1 + 1
        self.st: dict[str, Any] = {
            "running": True, "stopped": False, "error": "",
            "percent": 0, "t0": time.time(), "t1": None,
            "config": {"list_url": cfg.list_url, "pages": f"{cfg.p1}-{cfg.p2}",
                       "outdir": cfg.outdir, "workers": cfg.workers,
                       "seg_concurrency": cfg.seg_concurrency},
            "stages": {
                "list": {"status": "idle", "pages": 0, "pages_total": pages_total,
                         "found": 0},
                "play": {"status": "idle", "resolved": 0, "total": 0},
                "key": {"status": "idle", "got": 0, "total": 0, "method": "", "iv": ""},
                "seg": {"status": "idle", "done": 0, "skip": 0, "current": ""},
                "dec": {"status": "idle", "ok": 0, "fail": 0, "total": 0},
                "out": {"status": "idle", "count": 0, "gb": 0.0,
                        "dir": cfg.outdir, "recent": []},
            },
            "log": [],
            "videos": [], "vseq": 0,
        }

    # ---------- 只读属性 ----------
    @property
    def abort_msg(self) -> str:
        return self._abort_msg

    # ---------- 日志 ----------
    def log_line(self, msg: str) -> str:
        """把时间戳行写入内存缓冲（最多 400 行）并返回，写文件由调用方决定。"""
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            self.log_seq += 1
            self.st["log"].append(line)
            self.st["log"][:] = self.st["log"][-400:]
        return line

    # ---------- 阶段 / 视频 ----------
    def set_stage(self, name: str, **kw: Any) -> None:
        with self.lock:
            self.st["stages"][name].update(kw)

    def vset(self, vi: int | None, **kw: Any) -> None:
        if vi is None:
            return
        with self.lock:
            videos = self.st["videos"]
            if vi >= len(videos):
                return
            videos[vi].update(kw)
            if "s" in kw or "step" in kw or "t" in kw:
                self.st["vseq"] += 1

    def set_percent(self, value: float) -> None:
        with self.lock:
            self.st["percent"] = value

    def progress(self) -> None:
        """按视频状态（而不是累计计数器）刷新总进度，重试后仍然幂等。"""
        with self.lock:
            done = sum(1 for v in self.st["videos"] if v["s"] in (2, 3, 4))
            self.st["percent"] = min(100.0, 10 + 88 * done / max(1, self.total))

    def add_seg_done(self, count: int) -> None:
        with self.lock:
            self.st["stages"]["seg"]["done"] += count

    def set_key_info(self, key_count: int, method: str, iv_text: str) -> None:
        with self.lock:
            key = self.st["stages"]["key"]
            key["got"] += key_count
            key["method"] = method
            key["iv"] = iv_text

    def set_list_progress(self, pages: int, found: int, percent: float) -> None:
        with self.lock:
            self.st["stages"]["list"].update(pages=pages, found=found)
            self.st["percent"] = percent

    # ---------- 视频入队 ----------
    def add_items(self, got: list[VideoItem], page: int,
                  base: str) -> list[tuple[int, VideoItem]]:
        """去重入队，返回 [(视频编号, item)]；编号从 1 开始，保持列表页顺序。"""
        added: list[tuple[int, VideoItem]] = []
        newvids: list[dict[str, Any]] = []
        for item in got:
            if item.id in self.vmap:
                continue
            # API 捕获模式下 item 已带正确页码, 普通逐页模式用 page 覆盖
            item.page = item.page or page
            self.items.append(item)
            vi = len(self.items) - 1
            self.vmap[item.id] = vi
            newvids.append({
                "i": vi + 1, "t": item.title or item.id, "s": int(VideoState.QUEUED),
                "step": 0, "sd": 0, "sn": 0, "mb": 0.0, "e": "",
                "u": abs_url(item.id, base)})
            added.append((vi + 1, item))
        if added:
            with self.lock:
                self.st["videos"].extend(newvids)
                self.total = len(self.items)
                for name in ("play", "key", "seg", "dec", "out"):
                    self.st["stages"][name]["total"] = self.total
                self.st["vseq"] += 1
        return added

    def reset_items(self) -> None:
        with self.lock:
            self.items = []
            self.vmap = {}
            self.total = 0
            self.st["videos"] = []
            self.st["vseq"] += 1

    # ---------- 计数 ----------
    def record_success(self, name: str, mb: float) -> None:
        with self.lock:
            self.ok += 1
            self.streak_fail = 0
            out = self.st["stages"]["out"]
            out["count"] += 1
            out["gb"] += mb / 1024
            out["recent"].insert(0, f"{name} ({mb:.0f}MB)")
            del out["recent"][8:]
            self.st["stages"]["dec"]["ok"] = self.ok

    def record_skip(self) -> None:
        with self.lock:
            self.skip += 1
            self.st["stages"]["seg"]["skip"] = self.skip

    def record_failure(self, error: str, streak_limit: int) -> str:
        """记一次失败；达到熔断阈值时返回中止原因（由调用方决定是否抛出）。"""
        trip = ""
        with self.lock:
            self.fail += 1
            self.st["stages"]["dec"]["fail"] = self.fail
            self.streak_fail += 1
            if self.streak_fail >= streak_limit and not self._abort_msg:
                self._abort_msg = (
                    f"连续 {streak_limit} 个视频下载失败, "
                    f"播放页/密钥链路可能已失效, 已自动中止以免继续空跑。"
                    f"最后一次错误: {error[:160]}")
                trip = self._abort_msg
        return trip

    def set_abort(self, msg: str) -> bool:
        with self.lock:
            if self._abort_msg:
                return False
            self._abort_msg = msg
            return True

    # ---------- 收尾 / 重试 ----------
    def finish(self, stopped: bool = False, error: bool = False) -> None:
        with self.lock:
            self.st["running"] = False
            self.st["stopped"] = stopped
            self.st["t1"] = time.time()
            for stage in self.st["stages"].values():
                if stage["status"] == "running":
                    stage["status"] = (
                        "stopped" if stopped else ("error" if error else "done"))
            reset = 0
            for video in self.st["videos"]:
                if video["s"] == 1:
                    video.update(s=0, step=0, sd=0, e="")
                    reset += 1
            if reset:
                self.st["vseq"] += 1

    def prepare_retry(self) -> tuple[list[tuple[int, VideoItem]], str]:
        """把失败/未完成的视频复位成排队，返回 [(视频编号, item)] 或错误信息。"""
        with self.lock:
            if self.st["running"]:
                return [], "任务正在运行中"
            vids = self.st["videos"]
            vis = [n for n, v in enumerate(vids) if v["s"] in (0, 3)]
            if not vis:
                return [], "没有失败或未完成的视频"
            for vi in vis:
                video = vids[vi]
                if video["s"] == 3:
                    self.fail -= 1
                video.update(s=0, step=0, sd=0, sn=0, mb=0.0, e="")
            self.st["stages"]["dec"]["fail"] = self.fail
            self.st["running"] = True
            self.st["stopped"] = False
            self.st["error"] = ""
            self.st["t0"] = time.time()   # 本轮重试单独计时
            self.st["t1"] = None
            self.st["vseq"] += 1
            self.streak_fail = 0
            self._abort_msg = ""
            pairs = [(vi + 1, self.items[vi]) for vi in vis if vi < len(self.items)]
            return pairs, ""

    def snapshot(self) -> JobSnapshot:
        with self.lock:
            stages: dict[str, dict[str, Any]] = {}
            for name, stage in self.st["stages"].items():
                item = dict(stage)
                if "recent" in item:
                    item["recent"] = list(item["recent"])
                stages[name] = item
            return JobSnapshot(
                running=self.st["running"], stopped=self.st["stopped"],
                error=self.st["error"], percent=self.st["percent"],
                t0=self.st["t0"], t1=self.st["t1"],
                config=dict(self.st["config"]),
                stages=stages,
                log=list(self.st["log"]),
                log_seq=self.log_seq,
                videos=[dict(v) for v in self.st["videos"]],
                vseq=self.st["vseq"],
            )
