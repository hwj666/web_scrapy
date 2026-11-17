"""成品媒体校验：用 ffprobe（没有则退回 ffmpeg -i）确认 MP4 至少能被解析。

目的：避免“解密/合成看似成功、实际是坏片”的 MP4 被当成完成品，之后被
「文件已存在就跳过」永久保留。校验只看容器/时长，不做完整解码（大文件也很快）。

- ``YS_VERIFY_MP4=0`` 可关闭校验（默认开启）。
- ffprobe/ffmpeg 都找不到时跳过校验（不让环境问题把下载判失败）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from app.core import settings
from app.toolkit.files import ffmpeg_exe, no_window

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.I)


def verify_enabled() -> bool:
    return settings.verify_mp4()


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    try:
        ff = ffmpeg_exe()
    except RuntimeError:
        return ""
    cand = os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    return cand if os.path.isfile(cand) else ""


def _ffprobe_duration(path: str) -> tuple[float, str]:
    exe = _ffprobe()
    if not exe:
        return 0.0, "no-ffprobe"
    try:
        p = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration,format_name", "-of", "json", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **no_window(),
        )
    except Exception as e:  # noqa: BLE001
        return 0.0, f"{type(e).__name__}: {e}"
    if p.returncode != 0:
        return 0.0, (p.stderr or "ffprobe 失败").strip()[:300]
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        return 0.0, "ffprobe 输出不是 JSON"
    fmt = data.get("format") or {}
    if not fmt.get("format_name"):
        return 0.0, "ffprobe 未识别出容器格式"
    try:
        return float(fmt.get("duration") or 0), ""
    except (TypeError, ValueError):
        return 0.0, ""


def _ffmpeg_duration(path: str) -> tuple[float, str]:
    """没有 ffprobe 时从 ffmpeg -i 的 stderr 里读时长/容器。"""
    try:
        exe = ffmpeg_exe()
    except RuntimeError as e:
        return 0.0, f"找不到 ffmpeg: {e}"
    try:
        p = subprocess.run(
            [exe, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **no_window(),
        )
    except Exception as e:  # noqa: BLE001
        return 0.0, f"{type(e).__name__}: {e}"
    err = p.stderr or ""
    if "Invalid data found" in err or "moov atom not found" in err:
        return 0.0, "ffmpeg 无法解析该 MP4(可能已损坏)"
    m = _DUR_RE.search(err)
    if not m:
        return 0.0, "ffmpeg 未读出时长"
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, ""


def probe_media(path: str) -> tuple[bool, str]:
    """返回 (是否可用, 错误信息)。找不到工具时视为通过。"""
    if not verify_enabled():
        return True, ""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"文件不可读: {e}"
    if size < 1024:
        return False, f"文件过小({size} 字节)"
    dur, err = _ffprobe_duration(path)
    if err == "no-ffprobe":
        dur, err = _ffmpeg_duration(path)
    if err:
        return False, err
    if dur <= 0:
        return False, "时长为 0(合成可能失败)"
    return True, ""
