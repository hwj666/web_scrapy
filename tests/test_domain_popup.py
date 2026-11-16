"""浏览器弹窗/新标签页换域名识别（用假页面对象，不启动真实浏览器）。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager

from app.core.exceptions import NewDomainError
from app.core.models import JobConfig, VideoItem
from app.core.parsing import ParserContext
from app.core.rules import RuleManager
from app.parsers.play.browser import BrowserResolver
from app.services.domain import DomainProbe
from app.toolkit.http import FetchedResponse


class _FakeLocator:
    def __init__(self, page: _FakePage):
        self.page = page
        self.first = self

    async def click(self, timeout: int = 0) -> None:
        self.page.context.fire_page()


class _FakeMouse:
    async def click(self, x: float, y: float) -> None:
        pass


class _FakePage:
    def __init__(self, url: str, title: str = "", body: str = "",
                 opener=None):
        self.url = url
        self.context = _FakeContext()
        self.mouse = _FakeMouse()
        self._title = title
        self._body = body
        self._opener = opener
        self._handlers: dict[str, list] = {}
        self._closed = False

    def on(self, event: str, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def opener(self):
        return self._opener

    def get_by_text(self, _text: str, exact: bool = False):
        return _FakeLocator(self)

    async def goto(self, *_a, **_k) -> None:
        pass

    async def wait_for_timeout(self, *_a, **_k) -> None:
        pass

    async def wait_for_load_state(self, *_a, **_k) -> None:
        pass

    async def evaluate(self, js: str, *_a, **_k):
        if "getBoundingClientRect" in js:
            return {"x": 0, "y": 0}
        self.context.fire_page()
        return None

    async def title(self) -> str:
        return self._title

    async def inner_text(self, _selector: str) -> str:
        return self._body

    async def close(self) -> None:
        self._closed = True


class _FakeContext:
    def __init__(self) -> None:
        self._page_handlers: list = []
        self.popup: _FakePage | None = None

    def on(self, event: str, handler) -> None:
        if event == "page":
            self._page_handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        if event == "page" and handler in self._page_handlers:
            self._page_handlers.remove(handler)

    def fire_page(self) -> None:
        if self.popup is not None and self._page_handlers:
            for handler in list(self._page_handlers):
                handler(self.popup)
            self.popup = None  # 只触发一次


class BrowserPopupTests(unittest.TestCase):
    def test_popup_with_new_domain_raises(self) -> None:
        main = _FakePage("https://old.com/play/1.html")
        popup = _FakePage("https://new.com/notice", title="发布页",
                          body="最新地址：https://new.com/list", opener=main)
        main.context.popup = popup

        async def fetch(*_args, **_kwargs):
            return ""

        resolver = BrowserResolver(ParserContext(
            fetch=fetch, state={"rules": RuleManager()}))

        item = VideoItem(id="https://old.com/play/1.html", title="x")
        hits: list[str] = []
        with self.assertRaises(NewDomainError) as ctx:
            asyncio.run(resolver.resolve_with_page(
                main, hits, item, "https://old.com"))
        self.assertEqual(ctx.exception.host, "new.com")
        self.assertTrue(popup._closed)


class DomainProbePopupTests(unittest.TestCase):
    def test_publish_popup_switches_list_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            main = _FakePage("https://old.example.com/list/1-{}.html",
                             title="发布页", body="点击进入")
            popup = _FakePage("https://new.example.com/notice",
                              title="发布页",
                              body="新域名：https://new.example.com/list",
                              opener=main)
            main.context.popup = popup
            cfg = JobConfig(
                list_url="https://old.example.com/list/1-{}.html",
                p1=1, p2=1, outdir=tmp)

            async def get_raw(url: str):
                return FetchedResponse(url=url, status=200, headers={},
                                       content="<html>发布页</html>".encode())

            @asynccontextmanager
            async def new_page():
                yield main, []

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=new_page,
                log=lambda msg: None, ua="test")
            got = asyncio.run(probe.resolve_base(cfg.list_url))
            self.assertEqual(got, "https://new.example.com/list/1-{}.html")
            self.assertTrue(popup._closed)


if __name__ == "__main__":
    unittest.main()
