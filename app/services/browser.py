"""无头浏览器会话：整个任务共用一个浏览器和一个上下文，页面按需借还。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from app.toolkit.files import ORPHAN_MARK


class BrowserSession:
    """惰性启动 Playwright，复用 context；退出时统一关闭页面/上下文/驱动。"""

    def __init__(self, user_agent: str):
        self.user_agent = user_agent
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        # asyncio.Lock 必须绑定到运行中的事件循环，所以在协程里惰性创建
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _launch(self):
        import shutil as _sh

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        sys_chromium = _sh.which("chromium") or _sh.which("chromium-browser")
        launch_kwargs = dict(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", ORPHAN_MARK])
        # 容器里用 apt 的系统 chromium；本地开发没有系统 chromium 时退用自带浏览器
        try:
            if sys_chromium:
                browser = await pw.chromium.launch(
                    executable_path=sys_chromium, **launch_kwargs)
            else:
                browser = await pw.chromium.launch(**launch_kwargs)
        except Exception:
            # 启动失败要把 playwright 驱动收掉，否则会漏一个 node 进程
            try:
                await pw.stop()
            except Exception:
                pass
            raise
        self._pw = pw
        return browser

    async def _ensure_context(self):
        async with self._get_lock():
            if self._ctx is not None:
                return self._ctx
            if self._browser is None:
                self._browser = await self._launch()
            self._ctx = await self._browser.new_context(
                user_agent=self.user_agent, ignore_https_errors=True)
            return self._ctx

    @asynccontextmanager
    async def new_page(self):
        """借一个页面，返回 (page, m3u8 命中列表)；退出时只关页面、保留上下文。"""
        ctx = await self._ensure_context()
        page = await ctx.new_page()
        hits: list[str] = []

        def _on_request(req) -> None:
            # 只留少量命中（取最后一个），别让长页面把列表撑爆
            if ".m3u8" in req.url and len(hits) < 200:
                hits.append(req.url)

        page.on("request", _on_request)
        try:
            yield page, hits
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @property
    def active(self) -> bool:
        return any(x is not None for x in (self._ctx, self._browser, self._pw))

    async def close(self) -> None:
        ctx, browser, pw = self._ctx, self._browser, self._pw
        self._ctx = self._browser = self._pw = None
        for obj in (ctx, browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
