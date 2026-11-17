"""解析器基类：列表 / 播放页解析接口 + 运行期上下文。

解析器在 `Job._ensure_parsers()` 里显式实例化（内置四种：静态列表 / 浏览器列表 /
静态播放页 / 浏览器播放页）。需要适配新站点时，在 `app/parsers/` 下新增一个类，
并在 `Job._ensure_parsers()` 里登记即可，没有运行期自动发现。

接口全部是 async：取数走同一个 asyncio 事件循环，不再为每线程建 Session。
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .models import ListPage, ResolvedVideo, VideoItem


def _noop_stop() -> None:
    return None


@dataclass
class ParserContext:
    """解析器运行时的上下文：取数回调 + 配置 + 共享状态（规则等）+ 停止检查。"""

    fetch: Callable[..., Awaitable[Any]]
    cfg: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    # 停止/熔断检查：解析器在浏览器等待循环里调用它，抛 StopJob/JobAborted
    stop: Callable[[], Any] = field(default=_noop_stop)


class ListParser(abc.ABC):
    """列表页解析器接口。"""

    name = "list"
    uses_browser = False  # 需要无头浏览器页面对象的实现置 True

    def __init__(self, ctx: ParserContext | None = None) -> None:
        self.ctx = ctx or ParserContext(_noop_fetch)

    @abc.abstractmethod
    async def parse(self, html: str, page: int, base: str) -> ListPage: ...

    async def parse_with_page(self, page: Any, url: str, page_no: int) -> ListPage:
        """浏览器解析入口（uses_browser=True 的实现覆盖它）。"""
        raise NotImplementedError("该列表解析器不需要浏览器页面")


class Resolver(abc.ABC):
    """播放页解析器接口。"""

    name = "resolver"
    uses_browser = False

    def __init__(self, ctx: ParserContext | None = None) -> None:
        self.ctx = ctx or ParserContext(_noop_fetch)

    @abc.abstractmethod
    async def resolve(self, item: VideoItem, base: str) -> ResolvedVideo: ...

    async def resolve_with_page(
        self, page: Any, hits: list[str], item: VideoItem, base: str
    ) -> ResolvedVideo:
        """浏览器解析入口（uses_browser=True 的实现覆盖它）。"""
        raise NotImplementedError("该解析器不需要浏览器页面")


async def _noop_fetch(*_a: Any, **_k: Any) -> None:
    return None
