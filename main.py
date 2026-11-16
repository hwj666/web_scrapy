"""FastAPI 入口：路由 + 启动钩子（依赖倒置，业务在 app/services 与 app/core）。

启动: uv run uvicorn main:app --host 0.0.0.0 --port 8765 --reload
结构: main.py / index.html 在根目录，业务代码在 app/ 子目录（包内绝对导入）。
"""
from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.core.models import AppConfig, DedupConfig, JobConfig  # noqa: E402
from app.services.dedup_service import DedupService  # noqa: E402
from app.services.job import Job  # noqa: E402
from app.toolkit.files import ORPHAN_MARK, dbg_log, log_dir, no_window  # noqa: E402
from app.toolkit.url import split_urls  # noqa: E402

_ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(_ROOT, "index.html")

app_config = AppConfig()
dedup_svc = DedupService()

_current_job: Job | None = None
_job_lock = threading.Lock()


def _kill_orphans() -> None:
    """清理上次进程异常死亡遗留的 chromium 孤儿。

    只匹配我们自己 launch 时带的标记开关（ORPHAN_MARK），不像旧实现那样按
    `*ms-playwright*` 全机扫描——那会误杀本机其它项目的 Playwright 进程。
    """
    import subprocess

    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | "
                  f"Where-Object {{ $_.CommandLine -like '*{ORPHAN_MARK}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15, **no_window())
        else:
            subprocess.run(["pkill", "-f", ORPHAN_MARK.lstrip("-")],
                           capture_output=True, timeout=10, **no_window())
    except Exception:
        pass


def _start_orphan_cleanup() -> None:
    """放到后台线程跑，别让服务启动被 PowerShell/CIM 查询卡住。"""
    threading.Thread(target=_kill_orphans, name="orphan-cleanup", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _start_orphan_cleanup()
    log_root = log_dir()
    try:
        os.makedirs(log_root, exist_ok=True)
    except OSError:
        pass
    orig_err = sys.stderr
    fh = None
    try:
        fh = open(os.path.join(log_root, "_faulthandler.log"), "a",
                  buffering=1, encoding="utf-8")
        faulthandler.enable(fh)
        sys.stderr = fh
    except OSError:
        fh = None

    dbg_path = os.path.join(log_root, "_server_dbg.log")

    def _hook(t, v, tb):
        msg = "".join(traceback.format_exception(t, v, tb))
        dbg_log(dbg_path, "EXCEPT: " + msg)
        try:
            orig_err.write(msg)
            orig_err.flush()
        except Exception:
            pass

    def _thread_hook(args):
        dbg_log(dbg_path, "THREAD-EXCEPT: " + "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = _hook
    threading.excepthook = _thread_hook

    print(f"pipeline server starting... (root: {app_config.root}, logs: {log_root})",
          flush=True)
    try:
        yield
    finally:
        # 优雅停机：别把在跑的任务、ffmpeg 子进程、浏览器丢给操作系统
        try:
            if _current_job is not None:
                _current_job.shutdown()
        except Exception:
            pass
        try:
            dedup_svc.stop()
        except Exception:
            pass
        sys.stderr = orig_err
        if fh is not None:
            try:
                faulthandler.disable()
                fh.close()
            except Exception:
                pass


app = FastAPI(title="视频下载流水线", lifespan=lifespan)


# ---------------- 参数校验 ----------------
def _build_job_config(body: dict) -> JobConfig:
    urls = split_urls(body.get("list_url", ""))
    if not urls:
        raise ValueError("请粘贴视频列表页链接")
    for x in urls:
        up = urlsplit(x)
        if up.scheme not in ("http", "https") or not up.netloc:
            raise ValueError(f"列表链接格式不对: {x} (例: https://xxx/xxxx/xxx)")
    try:
        p1, p2 = int(body.get("p1")), int(body.get("p2"))
    except (TypeError, ValueError):
        raise ValueError("页码必须是数字") from None
    if not (1 <= p1 <= p2 <= 999):
        raise ValueError("页码范围应为 1~999 且 起始页 ≤ 结束页")
    outdir = str(body.get("outdir", "")).strip() or app_config.default_outdir
    if not os.path.isabs(outdir):
        raise ValueError("输出目录必须是绝对路径，例: /downloads")
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as e:
        raise ValueError(f"无法创建输出目录 {outdir}: {e}") from None
    try:
        workers = int(body.get("workers", 2))
    except (TypeError, ValueError):
        workers = 2
    try:
        segs = int(body.get("seg_concurrency", 8))
    except (TypeError, ValueError):
        segs = 8
    return JobConfig(
        list_url=", ".join(urls), p1=p1, p2=p2, outdir=outdir,
        workers=min(4, max(1, workers)),
        seg_concurrency=min(24, max(1, segs)),
    )


def _build_dedup_config(body: dict) -> DedupConfig:
    d = str(body.get("dir") or "").strip()
    defaulted = not d
    if defaulted:
        d = app_config.default_outdir
    if not os.path.isabs(d):
        raise ValueError("扫描目录必须是绝对路径")
    if not os.path.isdir(d):
        if not defaulted:
            raise ValueError(f"扫描目录不存在: {d}")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise ValueError(f"无法创建扫描目录 {d}: {e}") from None
    try:
        percent = float(body.get("percent", 96))
    except (TypeError, ValueError):
        percent = 96
    return DedupConfig(dir=d, percent=max(50.0, min(100.0, percent)))


# ---------------- 页面 / 状态 ----------------
@app.get("/")
def index():
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/job")
def api_job():
    if _current_job is None:
        return {"empty": True}
    return _current_job.snapshot().model_dump()


@app.get("/api/events")
async def api_events(request: Request):
    """任务状态 SSE：前端优先用它，断开时自动退回轮询。"""

    async def stream():
        while True:
            if await request.is_disconnected():
                break
            job = _current_job
            payload = job.snapshot().model_dump() if job is not None else {"empty": True}
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 任务控制 ----------------
@app.post("/api/start")
async def api_start(request: Request):
    global _current_job
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad json"}
    try:
        cfg = _build_job_config(body)
    except ValueError as e:   # 含 pydantic ValidationError：都是参数问题，直接回给前端
        return {"ok": False, "error": str(e)}
    with _job_lock:
        if _current_job is not None and _current_job.st["running"]:
            return {"ok": False, "error": "已有任务在运行，请先停止当前任务"}
        try:
            # 旧任务已经结束：先回收它的日志线程/残留子进程，再换新任务
            if _current_job is not None:
                _current_job.dispose()
            _current_job = Job(cfg)
            threading.Thread(target=_current_job.run, daemon=True).start()
        except Exception as e:
            return {"ok": False, "error": f"启动失败: {type(e).__name__}: {e}"}
    return {"ok": True, "error": ""}


@app.post("/api/stop")
def api_stop():
    with _job_lock:
        job = _current_job
        if job is None or not job.st["running"]:
            return {"ok": False, "error": "没有正在运行的任务"}
        job.ev.set()
    return {"ok": True}


@app.post("/api/retry_failed")
def api_retry_failed():
    with _job_lock:
        job = _current_job
        if job is None:
            return {"ok": False, "error": "还没有任务记录, 请先运行一次"}
        # 持锁调用：避免「重试刚启动、新任务就把它替换掉」的竞态
        n, err = job.retry_failed()
    return {"ok": n > 0, "error": err, "count": n}


# ---------------- 重复视频检测 (VDF) ----------------
@app.get("/api/dedup/status")
def api_dedup_status():
    return dedup_svc.snapshot()


@app.post("/api/dedup/start")
async def api_dedup_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        cfg = _build_dedup_config(body)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return dedup_svc.start(cfg)


@app.post("/api/dedup/stop")
def api_dedup_stop():
    return dedup_svc.stop()


@app.post("/api/dedup/apply")
async def api_dedup_apply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "bad json"}
    # vdf mark 是阻塞子进程（最多 15 分钟），必须丢到线程里，否则整个事件循环
    # 会被卡死：扫描进度、任务轮询、停止按钮全部无响应。
    return await asyncio.to_thread(
        dedup_svc.apply, str(body.get("strategy") or ""),
        str(body.get("mode") or "dry-run"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=app_config.host, port=app_config.port, reload=False)
