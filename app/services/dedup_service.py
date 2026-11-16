"""重复视频检测服务：调用 VDF（videoduplicatefinder）CLI 做画面内容查重。

扫描与清理刻意拆成两步：扫描慢（要解码视频），清理只重读上次 JSON，很快。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from app.core.models import DedupConfig
from app.toolkit.files import no_window

RESULT_FILE = "_vdf_results.json"
DB_DIR = "_vdf_db"
SETTINGS_FILE = "_ys_settings.json"

SAMPLES_DEFAULT = 24
SAMPLES_MAX = 200
AI_DEFAULT = True
PARTIAL_RATIO_DEFAULT = 0.05

STRATEGIES = frozenset({
    "lowest-quality", "smallest-file", "shortest-duration",
    "worst-resolution", "100-percent-only",
})
MODE_FLAGS = {
    "dry-run": "--dry-run",
    "trash": "--delete",
    "permanent": "--delete-permanent",
}

_BIN_NAMES = ("vdf", "vdf-cli")
_EXE_CACHE: tuple[str, float] | None = None
_EXE_TTL = 30.0
_PROG_RE = re.compile(r"\[\s*(\d{1,3})%\]\s*(\d+)/(\d+)")
_STAGE_RE = re.compile(r"\(([^()]{1,80})\)\s*$")
_ETA_RE = re.compile(r"ETA\s+\S+")
_STAGE_CNT_RE = re.compile(r"^(.*?)\s+(\d+)/(\d+)$")

# 进度行里的阶段关键字（VDF 输出为英文, 且随语言设置可能变化, 只做关键字兜底）
_STAGE_WORDS = (
    ("ai", ("ai ", " ai", "neural", "onnx", "dinov", "embedding")),
    ("compare", ("compar", "match", "similar")),
    ("hash", ("hash", "phash", "perceptual")),
    ("sample", ("sampl", "frame", "thumb", "extract", "decode")),
)
_STAGE_CN = {
    "ai": "AI 特征比对", "compare": "相似度比对", "hash": "感知哈希",
    "sample": "抽帧解码", "scan": "扫描解码",
}

VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".wmv", ".mov", ".flv", ".ts", ".m4v", ".rmvb",
    ".rm", ".webm", ".mpg", ".mpeg", ".3gp", ".m2ts", ".vob", ".f4v", ".asf",
    ".divx", ".ogv",
})
FILES_MAX = 5000        # 单次扫描枚举文件数上限（避免把整块盘塞进内存）
FILES_IN_SNAPSHOT = 500  # /api/dedup/status 最多回传多少行


def _stage_kind(label: str) -> str:
    low = (label or "").lower()
    for kind, words in _STAGE_WORDS:
        if any(w in low for w in words):
            return kind
    return "scan"


def _list_videos(root: str) -> list[dict]:
    """枚举待扫描的视频文件（VDF 只吃目录, 这份清单用于界面展示逐个文件的进度）。"""
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (DB_DIR, ".tmp.ts.parts")]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in VIDEO_EXTS:
                continue
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            out.append({"p": p, "n": fn, "sz": size, "s": 0, "st": "",
                        "c": 0, "fc": 0, "fm": 0})
            if len(out) >= FILES_MAX:
                out.sort(key=lambda f: f["p"].lower())
                return out
    out.sort(key=lambda f: f["p"].lower())
    return out



def _vdf_exe() -> str:
    global _EXE_CACHE
    env = (os.environ.get("VDF_BIN") or "").strip()
    if env:
        if os.path.isfile(env):
            return env
        return shutil.which(env) or ""
    now = time.time()
    if _EXE_CACHE is not None and now - _EXE_CACHE[1] < _EXE_TTL:
        return _EXE_CACHE[0]
    found = ""
    for name in _BIN_NAMES:
        path = shutil.which(name)
        if path:
            found = path
            break
    _EXE_CACHE = (found, now)
    return found


def _gb(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / 1024:.0f} KB"


def _dur(v) -> str:
    m = re.search(r"\d{1,2}:\d{2}:\d{2}", str(v or ""))
    return m.group(0) if m else ""


def _flags(v) -> str:
    s = str(v or "").replace(" ", "")
    if s in ("", "None", "0"):
        return ""
    return ",".join(x for x in s.split(",") if x and x != "None")


def _fmt_num(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _samples() -> int:
    raw = (os.environ.get("VDF_SAMPLES") or "").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return SAMPLES_DEFAULT
    return max(1, min(SAMPLES_MAX, n))


def _ai_enabled() -> bool:
    return (os.environ.get("VDF_AI") or "").strip().lower() not in (
        "0", "false", "no", "off")


def _partial_ratio() -> float:
    raw = (os.environ.get("VDF_PARTIAL_MIN_RATIO") or "").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return PARTIAL_RATIO_DEFAULT
    return max(0.01, min(1.0, v))


def _write_settings(dbdir: str, samples: int) -> str:
    p = os.path.join(dbdir, SETTINGS_FILE)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ThumbnailCount": samples}, f)
    return p


def _mark_keep(items: list[dict]) -> None:
    keep = ([i for i in items if i["_bb"]]
            or [i for i in items if i["_bf"]]
            or [max(items, key=lambda i: i["kbps"])])
    winner = keep[0]
    for i in items:
        i["keep"] = i is winner
        i.pop("_bb", None)
        i.pop("_bf", None)


class DedupService:
    """VDF 扫描 + 清理服务。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.ev = threading.Event()
        self.proc = None
        self._starting = False   # 防并发 POST /dedup/start 起两个 vdf 进程
        self.st = {
            "available": False, "running": False, "stage": "idle",
            "percent": 0, "current": "", "dir": "", "groups": [],
            "dup_groups": 0, "dup_files": 0, "wasted": 0,
            "error": "", "log": [], "t0": None, "t1": None,
            "result_file": "", "last_apply": None,
            # 逐文件进度: phase = enum/scan/compare/ai/done
            "phase": "idle", "stage_cn": "", "stage_cur": 0, "stage_max": 0,
            "files": [], "file_total": 0, "scanned": 0,
        }
        self._cur_idx: int | None = None
        self._cur_cmp: int | None = None
        # 文件名 -> 下标：VDF 进度行通常只截断目录、保留文件名，用字典匹配比
        # 每行遍历几千个文件快得多；只有文件名也被截断时才退回后缀扫描。
        self._by_base: dict[str, int] = {}
        self._last_file_key = ""
        self._last_cmp_key = ""

    def snapshot(self) -> dict:
        avail = bool(_vdf_exe())
        with self.lock:
            st = dict(self.st)
            st["log"] = list(self.st["log"])
            st["files"] = [dict(f) for f in self.st["files"][:FILES_IN_SNAPSHOT]]
            st["last_apply"] = dict(self.st["last_apply"]) if self.st["last_apply"] else None
            st["available"] = avail
            st["ai"] = _ai_enabled()
            st["samples"] = _samples()
            st["partial_ratio"] = _partial_ratio()
            if self.st["t0"]:
                st["elapsed"] = (self.st["t1"] or time.time()) - self.st["t0"]
            else:
                st["elapsed"] = 0
            return st

    def _log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            self.st["log"].append(line)
            self.st["log"][:] = self.st["log"][-300:]

    def _finish(self, stage: str, error: str = "", stopped: bool = False) -> None:
        with self.lock:
            self.st["running"] = False
            self.st["stage"] = stage
            if stage == "done":
                self.st["percent"] = 100
                self.st["phase"] = "done"
                self.st["stage_cn"] = ""
                self.st["stage_cur"] = self.st["stage_max"] = 0
            elif stage == "idle":
                self.st["phase"] = "idle"
            elif stage == "error":
                self.st["phase"] = "error"
            self.st["t1"] = time.time()
            if error:
                self.st["error"] = error
            if stopped:
                self.st["current"] = ""
        if error:
            self._log("错误: " + error)

    def _set_groups(self, groups: list[dict]) -> None:
        with self.lock:
            self.st["groups"] = groups
            self.st["dup_groups"] = len(groups)
            self.st["dup_files"] = sum(len(g["items"]) for g in groups)
            self.st["wasted"] = sum(g["wasted"] for g in groups)

    def start(self, cfg: DedupConfig) -> dict:
        # 检查与置位必须在同一个临界区：旧实现先放锁再准备目录，两个并发请求都能通过检查，
        # 结果起了两个 vdf 进程，而 stop() 只能停掉后一个。
        with self.lock:
            if self.st["running"] or self._starting:
                return {"ok": False, "error": "已有扫描在进行中"}
            self._starting = True
        try:
            exe = _vdf_exe()
            if not exe:
                return {"ok": False, "error":
                        "未找到 vdf 命令: 请安装 videoduplicatefinder CLI, "
                        "或用环境变量 VDF_BIN 指定 vdf-cli 的绝对路径"}
            dbdir = os.path.join(cfg.dir, DB_DIR)
            try:
                os.makedirs(dbdir, exist_ok=True)
            except OSError as e:
                return {"ok": False, "error": f"无法创建扫描数据库目录 {dbdir}: {e}"}
            result = os.path.join(cfg.dir, RESULT_FILE)
            with self.lock:
                self.ev.clear()
                self._cur_idx = None
                self._cur_cmp = None
                self._by_base = {}
                self._last_file_key = ""
                self._last_cmp_key = ""
                self.st.update(running=True, stage="scanning", percent=0, current="",
                               dir=cfg.dir, groups=[], dup_groups=0, dup_files=0, wasted=0,
                               error="", log=[], t0=time.time(), t1=None,
                               result_file=result, last_apply=None,
                               phase="enum", stage_cn="", stage_cur=0, stage_max=0,
                               files=[], file_total=0, scanned=0)
            threading.Thread(target=self._run, args=(cfg, exe, dbdir, result),
                             daemon=True).start()
            return {"ok": True, "error": ""}
        finally:
            with self.lock:
                self._starting = False

    def _run(self, cfg: DedupConfig, exe: str, dbdir: str, result: str) -> None:
        samples = _samples()
        ai = _ai_enabled()
        ratio = _partial_ratio()
        files = _list_videos(cfg.dir)
        with self.lock:
            self.st["files"] = files
            self.st["file_total"] = len(files)
            self.st["phase"] = "scan"
            self._by_base = {}
            for i, f in enumerate(files):
                self._by_base.setdefault(f["n"].lower(), i)
        self._log(f"开始扫描 {cfg.dir} · {len(files)} 个视频 · 相似度 ≥{cfg.percent}%"
                  f" · 每视频采样 {samples} 帧"
                  f" · 感知哈希 + 片段检测{' + AI 匹配' if ai else ''} · 片段下限 {ratio:.0%}")
        argv = [exe, "scan-and-compare", "--include", cfg.dir, "--db", dbdir,
                "--percent", _fmt_num(cfg.percent), "--use-phash",
                "--partial-clip-detection", "--partial-clip-min-ratio", _fmt_num(ratio),
                "--format", "json", "--output", result]
        if ai:
            argv += ["--ai-matching", "--ai-partial"]
        try:
            argv += ["--settings", _write_settings(dbdir, samples)]
        except OSError as e:
            self._log(f"写采样设置失败, 回退到 VDF 默认 1 帧: {e}")
        ok, err = self._exec(argv, self._on_scan_line)
        if self.ev.is_set():
            self._finish("idle", stopped=True)
            self._log("扫描已停止")
            return
        if not ok:
            self._finish("error", error=err or "vdf 扫描失败")
            return
        try:
            groups = self._load_groups(result)
        except Exception as e:
            self._finish("error", error=f"解析扫描结果失败: {type(e).__name__}: {e}")
            return
        self._set_groups(groups)
        self._finish("done")
        if groups:
            self._log(f"扫描完成: {len(groups)} 组重复, "
                      f"{sum(len(g['items']) for g in groups)} 个文件, "
                      f"预计可释放 {_gb(self.st['wasted'])}")
        else:
            self._log("扫描完成: 没有发现重复视频")

    def _on_scan_line(self, line: str) -> None:
        low = line.lower()
        if line.startswith("[scan]"):
            if "hashing complete" in low:
                self._mark_all_done()
                with self.lock:
                    self.st["phase"] = "compare"
                    self.st["percent"] = max(self.st["percent"], 60)
                    self.st["stage_cn"] = ""
                    self.st["stage_cur"] = self.st["stage_max"] = 0
            elif "comparison complete" in low:
                with self.lock:
                    st = self.st
                    st["phase"] = "done"
                    st["stage_cn"] = ""
                    st["stage_cur"] = st["stage_max"] = 0
                    for f in st["files"]:
                        f["c"] = 2
                    self._cur_cmp = None
            elif "enumeration complete" in low:
                with self.lock:
                    self.st["phase"] = "scan"
            self._log(line)
            return
        m = _PROG_RE.search(line)
        if not m:
            return
        # VDF 的进度行: \r[ 45%] 12/30  ETA 3m05s  .../path/file.mp4  (Scanning 3/20)
        # 先把行尾的阶段括号摘掉, 否则下面的 12/30 会被阶段里的 3/20 抢走。
        pos_cur = int(m.group(2))
        rest = _PROG_RE.sub("", line, count=1)
        stage_raw = ""
        sm = _STAGE_RE.search(rest)
        if sm:
            stage_raw = sm.group(1).strip()
            rest = rest[:sm.start()]
        rest = _ETA_RE.sub("", rest, count=1)
        shown = rest.strip()

        label, s_cur, s_max = stage_raw, 0, 0
        cm = _STAGE_CNT_RE.match(stage_raw)
        if cm:
            label = cm.group(1).strip()
            s_cur, s_max = int(cm.group(2)), int(cm.group(3))
        kind = _stage_kind(label) if label else ""
        cn = _STAGE_CN.get(kind, "")

        with self.lock:
            st = self.st
            raw = min(100, int(m.group(1)))
            # VDF 每个阶段各自 0~100；拼成整条进度条：扫描/哈希 0~60, 比对与 AI 60~100
            st["percent"] = (60 + raw * 0.4) if kind in ("ai", "compare") else raw * 0.6
            if shown:
                st["current"] = shown[-80:]
            st["stage_cn"] = cn
            st["stage_cur"], st["stage_max"] = s_cur, s_max
            if kind == "ai":
                st["phase"] = "ai"
            elif kind == "compare":
                st["phase"] = "compare"
            else:
                st["phase"] = "scan"
                st["scanned"] = min(st["file_total"], pos_cur)
        # VDF 真实流程: 每部视频先「扫描」(抽帧 + 感知哈希, 逐帧推进), 全部扫完再统一「比对」。
        # AI 匹配只是叠加在比对之上的另一路匹配, 不是每部视频的串行步骤。
        if kind in ("ai", "compare"):
            self._touch_cmp(shown)
        else:
            self._touch_file(shown, cn or "扫描中", s_cur, s_max)

    def _match_file(self, files: list[dict], key: str) -> int | None:
        """把 VDF 截断后的路径（最长 60 字符、前缀 ...）对回枚举出来的文件。"""
        k = key.strip().replace("\\", "/").lower()
        if not k:
            return None
        base = k.rsplit("/", 1)[-1]
        idx = self._by_base.get(base)
        if idx is not None:
            return idx
        # 文件名本身也被截断（超长文件名）时，才退回后缀/文件名扫描
        for i, f in enumerate(files):
            if f["p"].replace("\\", "/").lower().endswith(k):
                return i
        for i, f in enumerate(files):
            if f["n"].lower() == base:
                return i
        return None

    def _touch_file(self, shown: str, label: str, s_cur: int = 0, s_max: int = 0) -> None:
        key = shown.strip()
        if key.startswith("..."):
            key = key[3:]
        with self.lock:
            files = self.st["files"]
            if key and key == self._last_file_key and self._cur_idx is not None:
                idx = self._cur_idx
            else:
                idx = self._match_file(files, key)
                if idx is not None:
                    self._last_file_key = key
            if idx is None:
                return
            prev = self._cur_idx
            if prev is not None and prev != idx and 0 <= prev < len(files):
                pf = files[prev]
                if pf["s"] == 1:
                    pf["s"] = 2
                    pf["st"] = "完成"
            cur = files[idx]
            if cur["s"] != 2:
                cur["s"] = 1
                cur["st"] = label
                cur["fc"], cur["fm"] = s_cur, s_max
            self._cur_idx = idx

    def _touch_cmp(self, shown: str) -> None:
        """比对阶段: 上一个参与比对的行标记为完成, 当前行点亮。"""
        key = shown.strip()
        if key.startswith("..."):
            key = key[3:]
        with self.lock:
            files = self.st["files"]
            if key and key == self._last_cmp_key and self._cur_cmp is not None:
                idx = self._cur_cmp
            else:
                idx = self._match_file(files, key)
                if idx is not None:
                    self._last_cmp_key = key
            if idx is None:
                return
            prev = self._cur_cmp
            if prev is not None and 0 <= prev < len(files):
                files[prev]["c"] = 2
            files[idx]["c"] = 1
            self._cur_cmp = idx

    def _mark_all_done(self) -> None:
        with self.lock:
            for f in self.st["files"]:
                if f["s"] != 2:
                    f["s"] = 2
                    f["st"] = "完成"
            self.st["scanned"] = self.st["file_total"]
            self._cur_idx = None

    def _exec(self, argv: list, on_line=None) -> tuple[bool, str]:
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, bufsize=0, **no_window())
        except OSError as e:
            return False, f"无法启动 vdf: {e}"
        with self.lock:
            self.proc = p
        tail: list[str] = []
        buf = b""
        try:
            while True:
                if self.ev.is_set():
                    break
                try:
                    chunk = p.stdout.read(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for raw in parts:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    tail.append(line)
                    if len(tail) > 400:
                        del tail[:-200]
                    if on_line:
                        on_line(line)
                if len(buf) > 8192:
                    buf = buf[-8192:]
        finally:
            if self.ev.is_set():
                try:
                    p.terminate()
                except OSError:
                    pass
            try:
                p.wait(timeout=20)
            except Exception:
                try:
                    p.kill()
                except OSError:
                    pass
                try:
                    p.wait(timeout=10)
                except Exception:
                    pass
            with self.lock:
                self.proc = None
        if self.ev.is_set():
            return False, "扫描已停止"
        if p.returncode != 0:
            return False, f"vdf 退出码 {p.returncode}: {' | '.join(tail[-3:])[:400]}"
        return True, ""

    def _load_groups(self, path: str) -> list[dict]:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f) or []
        if isinstance(raw, dict):
            raw = raw.get("groups") or raw.get("Groups") or []
        groups = []
        for g in raw:
            items = []
            for it in (g.get("Items") or []):
                p = str(it.get("Path") or "")
                if not p:
                    continue
                items.append({
                    "path": p, "name": os.path.basename(p),
                    "size": int(it.get("SizeLong") or 0),
                    "sim": round(float(it.get("Similarity") or 0.0), 2),
                    "res": str(it.get("FrameSize") or ""),
                    "fps": round(float(it.get("Fps") or 0.0), 2),
                    "kbps": int(it.get("BitRateKbs") or 0),
                    "dur": _dur(it.get("Duration")),
                    "flags": _flags(it.get("Flags")),
                    "best": bool(it.get("IsBestBitRateKbs") or it.get("IsBestFrameSize")
                                  or it.get("IsBestDuration") or it.get("IsBestSize")
                                  or it.get("IsBestAudioBitRateKbs") or it.get("IsBestHdrFormat")),
                    "_bb": bool(it.get("IsBestBitRateKbs")),
                    "_bf": bool(it.get("IsBestFrameSize")),
                })
            if len(items) < 2:
                continue
            _mark_keep(items)
            groups.append({"id": str(g.get("GroupId") or ""), "items": items,
                           "wasted": sum(i["size"] for i in items if not i["keep"])})
        groups.sort(key=lambda g: -g["wasted"])
        return groups

    def apply(self, strategy: str, mode: str) -> dict:
        with self.lock:
            if self.st["running"]:
                return {"ok": False, "error": "扫描进行中, 请等它结束后再清理"}
            result = self.st["result_file"]
        if not result or not os.path.isfile(result):
            return {"ok": False, "error": "还没有可用的扫描结果, 请先扫描一次"}
        exe = _vdf_exe()
        if not exe:
            return {"ok": False, "error": "未找到 vdf 命令"}
        if strategy not in STRATEGIES:
            return {"ok": False, "error": f"未知策略: {strategy}"}
        flag = MODE_FLAGS.get(mode)
        if not flag:
            return {"ok": False, "error": f"未知清理模式: {mode}"}
        argv = [exe, "mark", "--input", result, "--strategy", strategy, flag]
        rc, out = self._capture(argv)
        tail = out[-4000:]
        deleted = len(re.findall(r"^\[[^\]]+\]\s+(?:would delete|deleted|trashed)\s*:", out, re.M))
        with self.lock:
            self.st["last_apply"] = {"strategy": strategy, "mode": mode, "rc": rc,
                                     "at": time.time(), "deleted": deleted,
                                     "out": tail[-1500:]}
        if rc != 0:
            self._log(f"清理失败(rc={rc}): {tail[-300:]}")
            return {"ok": False, "error": f"vdf mark 退出码 {rc}", "out": tail}
        if mode == "dry-run":
            self._log(f"预览完成[{strategy}]: 将删除 {deleted} 个文件")
        else:
            self._log(f"清理完成[{strategy}/{mode}]: 已处理 {deleted} 个文件")
            self._prune_missing()
        return {"ok": True, "error": "", "out": tail, "deleted": deleted}

    def _capture(self, argv: list, timeout: int = 900) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", **no_window())
        except subprocess.TimeoutExpired:
            return 1, "vdf mark 超时"
        except Exception as e:
            return 1, f"{type(e).__name__}: {e}"
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    def _prune_missing(self) -> None:
        with self.lock:
            result = self.st["result_file"]
        if not result or not os.path.isfile(result):
            self._set_groups([])
            return
        try:
            with open(result, encoding="utf-8") as f:
                raw = json.load(f) or []
            kept = []
            for g in raw:
                items = [it for it in (g.get("Items") or [])
                         if os.path.isfile(str(it.get("Path") or ""))]
                if len(items) >= 2:
                    g["Items"] = items
                    kept.append(g)
            with open(result, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
            self._set_groups(self._load_groups(result))
        except (OSError, ValueError, TypeError) as e:
            self._log(f"回写结果文件失败, 需重新扫描: {type(e).__name__}: {e}")
            with self.lock:
                self.st["result_file"] = ""
            self._set_groups([])

    def stop(self) -> dict:
        with self.lock:
            if not self.st["running"]:
                return {"ok": False, "error": "没有正在进行的扫描"}
            self.ev.set()
            p = self.proc
        self._log("正在停止扫描…")
        if p is not None:
            try:
                p.terminate()
            except OSError:
                pass
        return {"ok": True}
