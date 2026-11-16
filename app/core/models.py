"""Pydantic 模型：用类型约束替代裸字典，减少硬编码字符串键。"""
from __future__ import annotations

import os
import re
from enum import IntEnum
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

# 仓库根目录（app/core/models.py → 往上两级是 app，再往上一级是仓库根）
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTDIR = os.environ.get("OUTDIR") or str(_ROOT / "downloads")
_HOST = (os.environ.get("YS_HOST") or "0.0.0.0").strip() or "0.0.0.0"
try:
    _PORT = int(os.environ.get("YS_PORT") or 8765)
except ValueError:
    _PORT = 8765
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class VideoState(IntEnum):
    """视频在流水线中的生命周期状态（快照里以 int 形式呈现给前端）。"""
    QUEUED = 0
    RUNNING = 1
    DONE = 2
    FAILED = 3
    SKIPPED = 4


class AppConfig(BaseModel):
    """全局应用配置。"""
    host: str = _HOST
    port: int = _PORT
    root: str = str(_ROOT)
    default_outdir: str = _DEFAULT_OUTDIR
    ua: str = _UA


class JobConfig(BaseModel):
    """一次任务的完整配置。"""
    list_url: str
    candidates: list[str] = Field(default_factory=list)
    base: str = ""
    p1: int = 1
    p2: int = 1
    outdir: str = ""
    workers: int = 2
    # 分片下载总并发（所有视频加起来同时飞的分片数），界面可调，范围 1-24
    seg_concurrency: int = 8

    @field_validator("list_url")
    @classmethod
    def _strip_list_url(cls, v: str) -> str:
        return v.strip()

    @field_validator("p1", "p2")
    @classmethod
    def _valid_page(cls, v: int) -> int:
        if v < 1:
            raise ValueError("页码必须 >= 1")
        return v

    @field_validator("workers")
    @classmethod
    def _valid_workers(cls, v: int) -> int:
        return min(4, max(1, int(v)))

    @field_validator("seg_concurrency")
    @classmethod
    def _valid_seg_concurrency(cls, v: int) -> int:
        return min(24, max(1, int(v)))

    @model_validator(mode="after")
    def _fill_candidates_base(self) -> Self:
        if not self.candidates:
            self.candidates = list(dict.fromkeys(
                x for x in re.split(r"[,，;；\s]+", self.list_url) if x))
        if not self.base and self.candidates:
            up = urlparse(self.candidates[0])
            self.base = f"{up.scheme}://{up.netloc}"
        return self


class VideoItem(BaseModel):
    """列表页解析出的一个视频项。"""
    id: str
    title: str = ""
    duration: int = 0
    page: int = 0
    url: str = ""

    @model_validator(mode="after")
    def _fill_url(self) -> Self:
        if not self.url and self.id:
            self.url = self.id
        return self


class ListPage(BaseModel):
    """一个列表页的解析结果。"""
    page: int
    items: list[VideoItem]


class ResolvedVideo(BaseModel):
    """播放页解析结果。"""
    m3u8_url: str
    title: str = ""


class DedupConfig(BaseModel):
    """VDF 查重配置。"""
    dir: str
    percent: float = Field(default=96.0, ge=50.0, le=100.0)


class JobSnapshot(BaseModel):
    """给前端的状态快照（字段名与 index.html 约定保持一致）。"""
    running: bool = False
    stopped: bool = False
    error: str = ""
    percent: float = 0.0
    t0: float = 0.0
    t1: float | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    log: list[str] = Field(default_factory=list)
    # 日志总条数（只增不减）。前端用它判断有没有新行：log 数组本身会被裁剪到 400 行。
    log_seq: int = 0
    videos: list[dict[str, Any]] = Field(default_factory=list)
    vseq: int = 0
