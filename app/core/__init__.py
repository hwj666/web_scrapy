"""ys_web 核心抽象层：模型、插件接口、规则引擎。"""

from .models import (
    AppConfig,
    DedupConfig,
    JobConfig,
    JobSnapshot,
    ListPage,
    ResolvedVideo,
    VideoItem,
    VideoState,
)
from .plugins import PluginContext

__all__ = [
    "AppConfig",
    "JobConfig",
    "VideoItem",
    "ListPage",
    "ResolvedVideo",
    "VideoState",
    "JobSnapshot",
    "DedupConfig",
    "PluginContext",
]
