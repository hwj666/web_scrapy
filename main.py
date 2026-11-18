"""FastAPI 入口：路由 + 启动钩子（依赖倒置，业务在 app/services 与 app/core）。

启动: uv run uvicorn main:app --host 0.0.0.0 --port 8765 --reload
结构: main.py / index.html 在根目录，业务代码在 app/ 子目录（包内绝对导入）。

采用 app factory：所有运行期状态由 ``create_app()`` 装配并挂在 ``app.state.runtime``，
模块级只保留无副作用的常量与纯函数，不再在 import 时读环境变量 / 实例化服务。
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import os
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.core import settings
from app.core.models import AppConfig, DedupConfig, JobConfig
from app.services.dedup_service import DedupService
from app.services.job import Job
from app.toolkit.files import ORPHAN_MARK, dbg_log, log_dir, no_window
from app.toolkit.url import page_mode_warning, split_urls

_ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(_ROOT, "index.html")


def _safe_dir(raw: str, allowed_roots: list[str], must_exist: bool = False) -> str:
    """校验目录：必须是绝对路径，且（配置了 YS_ALLOWED_ROOTS 时）落在允许根目录内。"""
    d = os.path.abspath(raw)
    if not os.path.isabs(d):
        raise ValueError(f"路径必须是绝对路径: {raw}")
    real = os.path.realpath(d)
    if allowed_roots and not any(real == r or real.startswith(r + os.sep) for r in allowed_roots):
        raise ValueError(
            f"目录不在允许范围内: {real}（可用环境变量 YS_ALLOWED_ROOTS="
            f"{os.pathsep.join(allowed_roots)} 追加，多个用系统路径分隔符隔开）"
        )
    if must_exist and not os.path.isdir(real):
        raise ValueError(f"目录不存在: {real}")
    return real


def _build_job_config(body: dict, default_outdir: str, safe_dir: Callable[[str], str]) -> JobConfig:
    urls = split_urls(body.get("list_url", ""))
    if not urls:
        raise ValueError("请粘贴视频列表页链接")
    for x in urls:
        up = urlsplit(x)
        if up.scheme not in ("http", "https") or not up.netloc:
            raise ValueError(f"列表链接格式不对: {x} (例: https://xxx/xxxx/xxx)")
    try:
        p1 = int(body.get("p1") or 0)
        p2 = int(body.get("p2") or 0)
    except (TypeError, ValueError):
        raise ValueError("页码必须是数字") from None
    if not (1 <= p1 <= p2 <= 10000):
        raise ValueError("页码范围应为 1~10000 且 起始页 ≤ 结束页")
    outdir = str(body.get("outdir", "")).strip() or default_outdir
    if not os.path.isabs(outdir):
        raise ValueError("输出目录必须是绝对路径，例: /downloads")
    outdir = safe_dir(outdir)
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
    only_new = bool(body.get("only_new"))
    return JobConfig(
        list_url=", ".join(urls),
        p1=p1,
        p2=p2,
        outdir=outdir,
        workers=min(4, max(1, workers)),
        seg_concurrency=min(24, max(1, segs)),
        only_new=only_new,
    )


def _build_dedup_config(body: dict, default_outdir: str, safe_dir: Callable[[str], str]) -> DedupConfig:
    d = str(body.get("dir") or "").strip()
    defaulted = not d
    if defaulted:
        d = default_outdir
    if not os.path.isabs(d):
        raise ValueError("扫描目录必须是绝对路径")
    d = safe_dir(d)
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


def _restore_last_job(last_job_ptr: str, default_outdir: str) -> Job | None:
    """启动时恢复上一次任务状态（仅内存展示 + 一键重试，不自动续跑）。"""
    paths: list[str] = []
    try:
        with open(last_job_ptr, encoding="utf-8") as f:
            ptr = f.read().strip()
        if ptr:
            paths.append(ptr)
    except OSError:
        pass
    paths.append(os.path.join(default_outdir, "_job_state.json"))
    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        try:
            cfg = JobConfig.model_validate(data.get("job_config") or data.get("config") or {})
        except Exception:
            continue
        try:
            job = Job(cfg)
            if not job.state.restore_state(data):
                job.dispose()
                continue
            return job
        except Exception:
            continue
    return None


def _kill_orphans() -> None:
    """清理上次进程异常死亡遗留的 chromium 孤儿。

    只匹配我们自己 launch 时带的标记开关（ORPHAN_MARK），不像旧实现那样按
    `*ms-playwright*` 全机扫描——那会误杀本机其它项目的 Playwright 进程。
    """
    import subprocess

    try:
        if os.name == "nt":
            ps = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.CommandLine -like '*{ORPHAN_MARK}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15, **no_window()
            )
        else:
            subprocess.run(
                ["pkill", "-f", ORPHAN_MARK.lstrip("-")], capture_output=True, timeout=10, **no_window()
            )
    except Exception:
        pass


def _start_orphan_cleanup() -> None:
    """放到后台线程跑，别让服务启动被 PowerShell/CIM 查询卡住。"""
    threading.Thread(target=_kill_orphans, name="orphan-cleanup", daemon=True).start()


@dataclass
class Runtime:
    """``create_app()`` 装配的运行期状态，挂在 ``app.state.runtime``。"""

    app_config: AppConfig
    dedup: DedupService
    api_token: str
    allowed_roots: list[str]
    last_job_ptr: str
    job_lock: threading.Lock = field(default_factory=threading.Lock)
    current_job: Job | None = None
    restored_job: Job | None = None

    def safe_dir(self, raw: str, must_exist: bool = False) -> str:
        return _safe_dir(raw, self.allowed_roots, must_exist)

    def pipeline_running(self) -> bool:
        job = self.current_job
        return bool(job is not None and job.st.get("running"))


def create_app() -> FastAPI:
    runtime = Runtime(
        app_config=AppConfig(),
        dedup=DedupService(),
        api_token=settings.token(),
        allowed_roots=settings.allowed_roots(),
        last_job_ptr=os.path.join(log_dir(), "_last_job.txt"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _start_orphan_cleanup()
        log_root = log_dir()
        try:
            os.makedirs(log_root, exist_ok=True)
        except OSError:
            pass
        try:
            runtime.restored_job = _restore_last_job(runtime.last_job_ptr, runtime.app_config.default_outdir)
            if runtime.restored_job is not None:
                print(f"已恢复上次任务状态: {runtime.restored_job.cfg.outdir}", flush=True)
        except Exception:
            runtime.restored_job = None
        orig_err = sys.stderr
        fh = None
        try:
            fh = open(os.path.join(log_root, "_faulthandler.log"), "a", buffering=1, encoding="utf-8")
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
            dbg_log(
                dbg_path,
                "THREAD-EXCEPT: "
                + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            )

        sys.excepthook = _hook
        threading.excepthook = _thread_hook

        print(f"pipeline server starting... (root: {runtime.app_config.root}, logs: {log_root})", flush=True)
        try:
            yield
        finally:
            # 优雅停机：别把在跑的任务、ffmpeg 子进程、浏览器丢给操作系统
            try:
                if runtime.current_job is not None:
                    runtime.current_job.shutdown()
            except Exception:
                pass
            try:
                if runtime.restored_job is not None:
                    runtime.restored_job.dispose()
            except Exception:
                pass
            try:
                runtime.dedup.stop()
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
    app.state.runtime = runtime

    @app.middleware("http")
    async def _api_auth(request: Request, call_next):
        """可选 Token 鉴权：设置 YS_TOKEN 后，除 /api/health 外的 /api 都需要 X-API-Token。"""
        path = request.url.path
        if runtime.api_token and path.startswith("/api") and path != "/api/health":
            got = request.headers.get("x-api-token") or ""
            if not secrets.compare_digest(got, runtime.api_token):
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ---------------- 页面 / 状态 ----------------
    @app.get("/")
    def index():
        return FileResponse(
            INDEX_HTML,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/api/job")
    def api_job():
        job = runtime.current_job or runtime.restored_job
        if job is None:
            return {"empty": True}
        return job.snapshot().model_dump()

    @app.get("/api/events")
    async def api_events(request: Request):
        """任务状态 SSE：前端优先用它，断开时自动退回轮询。"""

        async def stream():
            last_text: str | None = None
            last_sent = 0.0
            while True:
                if await request.is_disconnected():
                    break
                job = runtime.current_job or runtime.restored_job
                payload = job.snapshot().model_dump() if job is not None else {"empty": True}
                text = json.dumps(payload, ensure_ascii=False)
                now = time.monotonic()
                # 状态没变就不重复推：任务空闲/完成时不再每秒刷一份全量快照；
                # 15 秒一次心跳，保证前端能及时发现连接已断并退回轮询。
                if text != last_text or now - last_sent >= 15:
                    yield "data: " + text + "\n\n"
                    last_text, last_sent = text, now
                await asyncio.sleep(1.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---------------- 任务控制 ----------------
    @app.get("/api/health")
    def api_health():
        """轻量健康检查：给 Docker healthcheck / 反代探活使用。"""
        return {"ok": True, "job_running": runtime.pipeline_running(), "dedup_busy": runtime.dedup.busy}

    @app.post("/api/start")
    async def api_start(request: Request):
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "error": "bad json"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "bad json"}
        try:
            cfg = _build_job_config(body, runtime.app_config.default_outdir, runtime.safe_dir)
        except ValueError as e:  # 含 pydantic ValidationError：都是参数问题，直接回给前端
            return {"ok": False, "error": str(e)}
        if runtime.dedup.busy:
            return {"ok": False, "error": "查重扫描进行中，请先停止扫描再启动下载（避免读到写了一半的文件）"}
        with runtime.job_lock:
            if runtime.current_job is not None and runtime.current_job.st["running"]:
                return {"ok": False, "error": "已有任务在运行，请先停止当前任务"}
            try:
                # 旧任务已经结束：先回收它的日志线程/残留子进程，再换新任务
                if runtime.current_job is not None:
                    runtime.current_job.dispose()
                if runtime.restored_job is not None:
                    runtime.restored_job.dispose()
                    runtime.restored_job = None
                runtime.current_job = Job(cfg)
                threading.Thread(target=runtime.current_job.run, daemon=True).start()
            except Exception as e:
                return {"ok": False, "error": f"启动失败: {type(e).__name__}: {e}"}
        warnings = list(
            dict.fromkeys(w for u in split_urls(cfg.list_url) if (w := page_mode_warning(u, cfg.p1, cfg.p2)))
        )
        return {"ok": True, "error": "", "warning": "；".join(warnings)}

    @app.post("/api/stop")
    def api_stop():
        with runtime.job_lock:
            job = runtime.current_job
            if job is None or not job.st["running"]:
                return {"ok": False, "error": "没有正在运行的任务"}
            # 不只是置停止信号：立刻杀掉 ffmpeg 并取消在跑协程，
            # 否则合成阶段最长要等 30 分钟才真正停下。
            job.request_stop()
        return {"ok": True}

    @app.post("/api/retry_failed")
    def api_retry_failed():
        if runtime.dedup.busy:
            return {"ok": False, "error": "查重扫描进行中，请先停止扫描"}
        with runtime.job_lock:
            job = runtime.current_job
            if job is None and runtime.restored_job is not None:
                # 进程重启后内存里没有任务对象：用落盘状态恢复一个，重试照常可用
                runtime.current_job = job = runtime.restored_job
                runtime.restored_job = None
            if job is None:
                return {"ok": False, "error": "还没有任务记录, 请先运行一次"}
            # 持锁调用：避免「重试刚启动、新任务就把它替换掉」的竞态
            n, err = job.retry_failed()
        return {"ok": n > 0, "error": err, "count": n}

    @app.post("/api/retry_video")
    async def api_retry_video(request: Request):
        """失败清单里单条重试：body = {"i": 视频编号}。"""
        if runtime.dedup.busy:
            return {"ok": False, "error": "查重扫描进行中，请先停止扫描"}
        try:
            body = await request.json()
            idx = int(body.get("i"))
        except Exception:
            return {"ok": False, "error": '参数错误: 需要 {"i": 视频编号}'}
        with runtime.job_lock:
            job = runtime.current_job
            if job is None and runtime.restored_job is not None:
                runtime.current_job = job = runtime.restored_job
                runtime.restored_job = None
            if job is None:
                return {"ok": False, "error": "还没有任务记录, 请先运行一次"}
            n, err = job.retry_failed(only=[idx])
        return {"ok": n > 0, "error": err, "count": n}

    # ---------------- 重复视频检测 (VDF) ----------------
    @app.get("/api/dedup/status")
    def api_dedup_status():
        return runtime.dedup.snapshot()

    @app.post("/api/dedup/start")
    async def api_dedup_start(request: Request):
        if runtime.pipeline_running():
            return {"ok": False, "error": "下载任务运行中，请等任务结束后再扫描（避免扫描到写了一半的 MP4）"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            cfg = _build_dedup_config(body, runtime.app_config.default_outdir, runtime.safe_dir)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return runtime.dedup.start(cfg)

    @app.post("/api/dedup/stop")
    def api_dedup_stop():
        return runtime.dedup.stop()

    @app.post("/api/dedup/apply")
    async def api_dedup_apply(request: Request):
        if runtime.pipeline_running():
            return {"ok": False, "error": "下载任务运行中，请等任务结束后再清理"}
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "error": "bad json"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "bad json"}
        # vdf mark 是阻塞子进程（最多 15 分钟），必须丢到线程里，否则整个事件循环
        # 会被卡死：扫描进度、任务轮询、停止按钮全部无响应。
        return await asyncio.to_thread(
            runtime.dedup.apply, str(body.get("strategy") or ""), str(body.get("mode") or "dry-run")
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host=app.state.runtime.app_config.host, port=app.state.runtime.app_config.port, reload=False
    )
