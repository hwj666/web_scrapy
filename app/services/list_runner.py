"""列表页流水线：候选域名探测 → 逐页解析（静态/浏览器兜底/加密 SPA）→ 有序入队派发。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.exceptions import JobAborted, StopJob
from app.core.models import JobConfig, ListPage, VideoItem
from app.core.rules import RuleManager
from app.services.domain import DomainProbe
from app.services.job_state import JobState
from app.toolkit.http import is_conn_error, should_retry_with_browser
from app.toolkit.url import auto_list_url


class ListRunner:
    """把列表页解析成视频队列；每解析完一页就通过 `dispatch` 立刻派发下载。"""

    def __init__(self, cfg: JobConfig, rules: RuleManager, state: JobState, *,
                 fetch: Callable[..., Awaitable[str]],
                 static_list: Any,
                 browser_list: Any,
                 new_page: Callable[[], Any],
                 domain: DomainProbe,
                 log: Callable[[str], None],
                 is_stopped: Callable[[], bool],
                 dispatch: Callable[[int, VideoItem], None]):
        self.cfg = cfg
        self.rules = rules
        self.state = state
        self._fetch = fetch
        self.static_list = static_list
        self.browser_list = browser_list
        self._new_page = new_page
        self.domain = domain
        self.log = log
        self._is_stopped = is_stopped
        self._dispatch = dispatch

    async def run(self) -> None:
        cfg = self.cfg
        state = self.state
        state.reset_items()
        cands = list(cfg.candidates)
        list_url = None
        for cand in cands:
            got = await self.domain.resolve_base(cand, cfg.p1)
            if got:
                list_url = got
                break
            self.log(f"候选链接不可用, 试下一个: {cand}")
        if not list_url:
            raise RuntimeError(
                f"所有候选列表链接都无法访问: {', '.join(cands)}"
                f" —— 站点若已换域名, 请把新域名一起填进列表链接(可逗号分隔填多个)")
        if list_url != cfg.list_url:
            self.domain.set_base(list_url)
            if list_url not in cands:
                self.log(f"本次实际使用链接: {list_url}")
        # 换域名时只换了 host、路径可能已改版：校验真实列表页并按需修复路径
        cfg.list_url = await self.domain.ensure_list_url(
            cfg.list_url, cfg.p1, strict=True)

        state.set_stage("list", status="running")
        # 普通站先走静态/浏览器列表；加密 SPA 的 API 捕获只作为兜底。
        # 这样默认配置里即使保留了某个站点的 detail_url_template，也不会影响其它站点。
        await self._run_normal()
        use_api_cap = (self.rules.browser.use_api_capture
                       and bool(self.rules.browser.detail_url_template))
        if not state.items and use_api_cap:
            self.log("普通列表解析未得到视频, 尝试加密 SPA API 捕获")
            await self._run_api_capture()

        if state.abort_msg:
            raise JobAborted(state.abort_msg)
        if not state.items:
            raise RuntimeError("未解析到任何视频，请检查站点地址/分类ID/页码范围")

    # ---------- 普通逐页解析 ----------
    async def _run_normal(self) -> None:
        cfg = self.cfg
        state = self.state
        pages_total = cfg.p2 - cfg.p1 + 1
        zero_streak = 0

        async def fetch_page(p: int) -> list[VideoItem]:
            url = auto_list_url(self.cfg.list_url, p)
            try:
                html = await self._fetch(url, retries=2, timeout=20)
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                # 只有「被 WAF/限流/连不上」才值得开浏览器重试；
                # 404/410 说明路径就是不存在，开浏览器只会多等 20 多秒。
                if self.browser_list is None or not should_retry_with_browser(e):
                    raise
                self.log(f"page {p} HTTP 取页失败({type(e).__name__}: {str(e)[:80]}), "
                         f"改用浏览器")
                async with self._new_page() as (page, _hits):
                    await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
                    html = await page.content()
                    page_result = await self.static_list.parse(html, p, self.cfg.base)
                    if page_result.items:
                        return page_result.items
                    result = await self.browser_list.parse_with_page(page, url, p)
                    return result.items
            try:
                page_result: ListPage = await self.static_list.parse(
                    html, p, self.cfg.base)
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                # 静态解析器自身抛错（页面结构异常/正则问题）时不要整页当空，
                # 直接落到浏览器兜底；两者都失败才由 load_page 记日志。
                self.log(f"page {p} 静态解析失败: {type(e).__name__}: {str(e)[:100]}")
                page_result = ListPage(page=p, items=[])
            if page_result.items:
                # 有些站点标题是 JS 解密出来的（data-decrypt），静态解析只能拿到空标题；
                # 这时用浏览器再解析一次，拿到真实标题再下载，避免全是 untitled。
                if self.browser_list is not None and self._mostly_untitled(
                        page_result.items):
                    titled = await _browser_titles(url, p)
                    if titled:
                        return titled
                return page_result.items
            if self.browser_list is not None:
                async with self._new_page() as (page, _hits):
                    result = await self.browser_list.parse_with_page(page, url, p)
                return result.items
            return []

        async def _browser_titles(url: str, p: int) -> list[VideoItem] | None:
            try:
                async with self._new_page() as (page, _hits):
                    result = await self.browser_list.parse_with_page(page, url, p)
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                self.log(f"page {p} 浏览器补标题失败: {type(e).__name__}: "
                         f"{str(e)[:100]}")
                return None
            if result.items and any(it.title for it in result.items):
                self.log(f"page {p} 标题由浏览器解析(静态页标题为空)")
                return result.items
            return None

        async def load_page(p: int) -> list[VideoItem]:
            """抓一页；连接类错误先重探域名再试一次，其它错误记日志后当空页处理。"""
            old_url = self.cfg.list_url
            try:
                return await fetch_page(p)
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                if not is_conn_error(e):
                    self.log(f"page {p} ERROR: {e}")
                    return []
                # 别的并发页可能已经完成域名重探并更新了 list_url：这种直接重试，
                # 不要因为重探冷却期还没过就白白丢掉这一页。
                if self.cfg.list_url == old_url:
                    await self.domain.reprobe(f"列表页 {p} 连接失败")
                try:
                    return await fetch_page(p)
                except (StopJob, JobAborted):
                    raise
                except Exception as e2:
                    self.log(f"page {p} ERROR: {e2}")
                    return []

        # 列表页并发抓取：窗口内同时请求/解析，但结果严格按页码顺序入队，
        # 视频编号、进度和日志顺序都与串行时一致；连续两页为空就提前收工。
        conc = max(1, min(6, self.rules.pipeline.list_concurrency))
        pending: dict[asyncio.Task, int] = {}
        results: dict[int, list[VideoItem]] = {}
        next_page = cfg.p1
        next_done = cfg.p1

        def submit() -> None:
            nonlocal next_page
            # 同时在飞的页数 <= conc；且最多领先「下一个待处理页」conc-1 页，
            # 否则提前结束翻页时窗口里会多抓好几页。
            while (next_page <= cfg.p2 and len(pending) < conc
                   and next_page < next_done + conc):
                pending[asyncio.create_task(load_page(next_page))] = next_page
                next_page += 1

        submit()
        stop_paging = False
        try:
            while (pending or results) and not stop_paging:
                # 先按页码顺序消费已经就绪的结果，乱序完成的页只暂存、不抢跑
                while next_done in results and not stop_paging:
                    p = next_done
                    new = self._add_and_dispatch(results.pop(p), p)
                    state.set_list_progress(
                        p - cfg.p1 + 1, len(state.items),
                        2 + 8 * (p - cfg.p1 + 1) / pages_total)
                    self.log(f"列表 {p} 页: +{new} (累计 {len(state.items)})")
                    if new == 0:
                        zero_streak += 1
                        if zero_streak >= 2:
                            self.log("连续 2 页无新增，判定已到分类末尾，停止翻页")
                            stop_paging = True
                    else:
                        zero_streak = 0
                    if state.abort_msg:
                        stop_paging = True
                    next_done += 1
                if stop_paging:
                    break
                submit()
                if not pending:
                    break
                finished, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    p = pending.pop(task)
                    try:
                        results[p] = task.result()
                    except (StopJob, JobAborted):
                        await self._cancel_pages(pending)
                        raise
        finally:
            # 正常结束/异常/停止都清掉窗口里没跑完的页，别留后台任务
            await self._cancel_pages(pending)

    # ---------- 加密 SPA 兜底 ----------
    async def _run_api_capture(self) -> None:
        cfg = self.cfg
        state = self.state
        self.log(f"加密 SPA 列表模式: 用 TextDecoder 钩子采集 {cfg.p1}-{cfg.p2} 页")
        async with self._new_page() as (page, _hits):
            cap_items = await self.browser_list.collect_via_api_capture(
                page, self.cfg.list_url, cfg.p1, cfg.p2)
        # 按页码分组, 逐页走 add_items + 派发, 保持进度/日志与普通模式一致
        by_page: dict[int, list[VideoItem]] = {}
        for item in cap_items:
            by_page.setdefault(item.page or cfg.p1, []).append(item)
        for idx, p in enumerate(sorted(by_page)):
            if self._is_stopped():
                raise StopJob()
            new = self._add_and_dispatch(by_page[p], p)
            state.set_list_progress(
                idx + 1, len(state.items),
                2 + 8 * (idx + 1) / max(1, len(by_page)))
            self.log(f"列表 {p} 页: +{new} (累计 {len(state.items)})")
            if state.abort_msg:
                break
            await asyncio.sleep(0.1)

    # ---------- 工具 ----------
    def _add_and_dispatch(self, got: list[VideoItem], page: int) -> int:
        added = self.state.add_items(got, page, self.cfg.base)
        for vi, item in added:
            self._dispatch(vi, item)
        return len(added)

    @staticmethod
    def _mostly_untitled(items: list[VideoItem]) -> bool:
        """一半以上的条目没有标题，就认为标题需要浏览器(JS)才能拿到。"""
        if not items:
            return False
        missing = sum(1 for it in items if not (it.title or "").strip())
        return missing * 2 >= len(items)

    @staticmethod
    async def _cancel_pages(pending: dict[asyncio.Task, int]) -> None:
        tasks = list(pending)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        pending.clear()
