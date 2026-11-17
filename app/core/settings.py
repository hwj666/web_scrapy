"""环境变量统一读取与解析（单一来源）。

把散落在 http / files / probe / hls / dedup / models / main 里的
``os.environ.get(...).strip().lower() not in (...)`` 收敛到这里，提供类型化访问函数，
每个模块只保留一个薄委托，不再各自解析。

说明：当前用纯函数实现（项目尚未引入 ``pydantic-settings`` 依赖）。后续执行
``uv add pydantic-settings`` 后，可把这里替换为 ``BaseSettings`` 的字段，各访问函数
改成 ``get_settings().xxx`` 即可，调用方无需再动。
"""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]  # 仓库根


def _raw(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _flag_default_on(name: str) -> bool:
    """默认开启的开关：空/未设置 = 开；只有 0/false/no/off 才关。"""
    return _raw(name).lower() not in ("0", "false", "no", "off")


def _flag_default_off(name: str) -> bool:
    """默认关闭的开关：只有 1/true/yes/on 才开；空/未设置 = 关。"""
    return _raw(name).lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(_raw(name))
    except ValueError:
        return default
    return max(lo, min(hi, n))


def _float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(_raw(name))
    except ValueError:
        return default
    return max(lo, min(hi, v))


# ---- 应用基础 ----
def host() -> str:
    return _raw("YS_HOST") or "0.0.0.0"


def port() -> int:
    try:
        return int(_raw("YS_PORT") or 8765)
    except ValueError:
        return 8765


def outdir() -> str:
    return _raw("OUTDIR") or str(_ROOT / "downloads")


def log_dir() -> str:
    env = _raw("YS_LOG_DIR")
    if env:
        return env
    return os.path.join(outdir(), "_logs")


# ---- 开关（默认开）----
def tls_verify() -> bool:
    return _flag_default_on("YS_TLS_VERIFY")


def verify_mp4() -> bool:
    return _flag_default_on("YS_VERIFY_MP4")


def vdf_ai() -> bool:
    return _flag_default_on("VDF_AI")


# ---- 开关（默认关）----
def block_private_media() -> bool:
    return _flag_default_off("YS_BLOCK_PRIVATE_MEDIA")


def allow_no_endlist() -> bool:
    return _flag_default_off("YS_ALLOW_NO_ENDLIST")


# ---- 数值 / 其它 ----
def rate_rps() -> float:
    return _float("YS_RATE_RPS", 0.0, 0.0, float("inf"))


def vdf_samples() -> int:
    return _int("VDF_SAMPLES", 24, 1, 200)


def vdf_partial_ratio() -> float:
    return _float("VDF_PARTIAL_MIN_RATIO", 0.05, 0.01, 1.0)


def vdf_bin() -> str:
    return _raw("VDF_BIN")


def token() -> str:
    return _raw("YS_TOKEN")


def allowed_roots() -> list[str]:
    raw = os.environ.get("YS_ALLOWED_ROOTS") or ""
    return [os.path.realpath(x.strip()) for x in raw.split(os.pathsep) if x.strip()]
