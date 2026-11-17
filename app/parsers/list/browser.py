"""浏览器列表解析器（playwright 兜底）：JS 渲染后按几何位置配卡片与标题。"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from app.core.exceptions import JobAborted, StopJob
from app.core.models import ListPage, VideoItem
from app.core.parsing import ListParser, ParserContext
from app.core.rules import RuleManager
from app.toolkit.browser import goto_with_stop, wait_with_stop
from app.toolkit.title import TitleCleaner
from app.toolkit.url import abs_url

_CARD_JS = """() => {
  const titleSel = %(title_sels)s;
  const cards = [...document.querySelectorAll('div[to], a[href]')]
    .map(e => ({h: e.getAttribute('to') || e.getAttribute('href'),
                r: e.getBoundingClientRect(),
                a: (e.getAttribute('title') || '').trim(),
                ti: ((e.querySelector(%(card_sel)s) || {}).textContent || '').trim()}))
    .filter(x => x.h);
  const titles = [...document.querySelectorAll(titleSel)]
    .map(e => ({t: e.textContent.trim(), r: e.getBoundingClientRect()})).filter(x => x.t);
  return cards.map(C => { let best = '', bd = 1e9;
    for (const T of titles) {
      const ox = Math.min(C.r.right, T.r.right) - Math.max(C.r.left, T.r.left);
      if (ox <= 10) continue;
      const d = Math.abs((T.r.top + T.r.bottom) / 2 - (C.r.top + C.r.bottom) / 2);
      if (d < bd) { bd = d; best = T.t; } }
    return {h: C.h, t: bd < 120 ? best : '', a: C.a, ti: C.ti}; }); }"""

# SPA 列表: 卡片上没有 href/to, 只能点进去让前端路由跳转, 再从地址栏取详情页地址
_CLICK_JS = """(i) => {
  const cards = document.querySelectorAll(%(cards)s);
  const c = cards[i];
  if (!c) return '';
  try { c.scrollIntoView({block: 'center'}); } catch (e) {}
  const t = c.querySelector(%(card_sel)s);
  const txt = t ? (t.getAttribute('title') || t.textContent || '') : '';
  c.click();
  return txt.trim();
}"""


class BrowserListParser(ListParser):
    name = "browser_list"
    uses_browser = True

    # 加密 SPA: 挂钩 TextDecoder.prototype.decode, 捕获 libsodium 解密后的列表 JSON
    # (响应体是 {"code":0,"data":{"nonce":...,"cipher":...}}, 前端用 secretbox 解密成
    # {"items":[...],"pagination":{...}} 后才走 TextDecoder 解 UTF-8, 所以在此拦截)。
    _API_HOOK_JS = """
window.__list_pages = [];
(function(){
  const orig = TextDecoder.prototype.decode;
  TextDecoder.prototype.decode = function(input, opts){
    const r = orig.call(this, input, opts);
    try {
      if (typeof r === 'string' && r.length > 100 && r.startsWith('{"items"')) {
        window.__list_pages.push(r);
      }
    } catch(e){}
    return r;
  };
})();
"""
    # 弹窗/遮罩层选择器(zd3xy 的 .van-overlay z-index 99999 会拦截所有点击)
    _OVERLAY_HIDE_JS = """
document.querySelectorAll('.van-overlay, .popup-wrapper, [class*=popup], .van-tabbar')
  .forEach(e => { e.style.display='none'; e.style.pointerEvents='none'; });
"""

    # 规则里的选择器没命中时，按文字/aria-label 找「下一页/加载更多」并点击。
    # 只在 URL 翻页确认无效后作为兜底，避免误点普通导航里的“更多”。
    _NEXT_JS = """() => {
  const words = ['下一页','下一頁','下页','下頁','加载更多','加載更多','Load More','Next','next','›','»'];
  const sel = 'a,button,[role=button],[class*=next],[class*=page],[class*=more]';
  const cands = [...document.querySelectorAll(sel)];
  const visible = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const usable = el => visible(el) && !el.disabled
    && (el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
  const label = el => ((el.textContent || '') + ' '
    + (el.getAttribute('aria-label') || '')).trim();
  const inPager = el => !!el.closest(
    '.pagination,.pager,.page-nav,.pg,[class*=pagination],[class*=pager],[class*=page-]');
  const match = el => words.some(w => label(el) === w
    || (w.length > 2 && label(el).includes(w)));
  const hit = cands.find(el => usable(el) && inPager(el) && match(el))
    || cands.find(el => usable(el) && match(el));
  if (hit) { hit.click(); return true; }
  return false;
}"""

    def __init__(self, ctx: ParserContext | None = None):
        super().__init__(ctx)
        self.manager: RuleManager = (self.ctx.state or {}).get("rules") or RuleManager()
        self.cleaner = TitleCleaner(self.manager)

    def _check_stop(self) -> None:
        self.ctx.stop()

    async def _goto(self, page, url: str, timeout_ms: int = 45000) -> None:
        await goto_with_stop(page, url, stop=self._check_stop, timeout_ms=timeout_ms)

    async def _wait(self, page, ms: int) -> None:
        await wait_with_stop(page, ms, stop=self._check_stop)

    async def collect_via_api_capture(self, page, list_url: str, p1: int, p2: int) -> list[VideoItem]:
        """加密 SPA 列表采集: 挂钩 TextDecoder 拿解密后的 JSON, 点「下一页」翻页。

        与 parse_with_page 的区别:
        - 不按 URL 的 page 参数走(站点会剥掉它), 而是在同一页面内点下一页;
        - 直接用 detail_url_template 拼详情页地址, 不逐个点卡片;
        - 一次返回所有页的 items(已按 pagination.page 标好页码)。
        """
        b = self.manager.browser
        tpl = b.detail_url_template
        if not tpl:
            return []
        # 钩子必须在 goto 之前注入
        await page.add_init_script(self._API_HOOK_JS)
        await self._goto(page, list_url)
        await self._wait(page, 4000)
        # 隐藏弹窗遮罩, 否则「下一页」点击会被拦截
        try:
            await page.evaluate(self._OVERLAY_HIDE_JS)
        except Exception:
            pass

        out: list[VideoItem] = []
        processed = 0  # 已处理的 __list_pages 下标

        def ingest(pages_json: list[str]) -> bool:
            nonlocal processed
            new_items = 0
            for s in pages_json[processed:]:
                processed += 1
                try:
                    data = json.loads(s)
                except Exception:
                    continue
                pag = data.get("pagination") or {}
                page_no = int(pag.get("page") or 0)
                if page_no > p2:
                    return True  # 已超出目标范围, 停止翻页
                if page_no < p1:
                    continue
                for it in data.get("items") or []:
                    vid = it.get("id")
                    if vid is None:
                        continue
                    detail = abs_url(tpl.format(id=vid), list_url)
                    title = self.cleaner.clean(it.get("name") or "")
                    if self.cleaner.is_generic(title):
                        title = ""
                    out.append(VideoItem(id=detail, title=title, page=page_no))
                    new_items += 1
            return False

        # 第 1 页(初始加载)
        pages = await page.evaluate("window.__list_pages")
        if ingest(pages):
            return out

        # 点翻页按钮直到 p2 或无新数据
        next_sel = b.next_page_selector or ".pg-btn, .load-more button"
        texts = b.next_page_texts or ["下一页", "加载更多", "更多"]
        max_clicks = max(1, p2 - p1 + 5)
        for _ in range(max_clicks):
            self._check_stop()
            clicked = False
            for txt in texts:
                try:
                    loc = page.locator(next_sel, has_text=txt)
                    if await loc.count() > 0:
                        await loc.first.click(force=True)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                break
            await self._wait(page, 2500)
            try:
                await page.evaluate(self._OVERLAY_HIDE_JS)
            except Exception:
                pass
            pages = await page.evaluate("window.__list_pages")
            if len(pages) <= processed:
                break  # 没有新数据, 可能已到末尾
            if ingest(pages):
                break
        return out

    async def parse(self, html: str, page: int, base: str) -> ListPage:
        raise NotImplementedError("browser_list 需要 page 对象，请使用 parse_with_page")

    async def parse_with_page(self, page, url: str, page_no: int) -> ListPage:
        await self._goto(page, url)
        items = await self._extract_items(page, url, page_no)
        if not items:
            items = await self._collect_by_click(page, url, page_no)
        return ListPage(page=page_no, items=items)

    async def _extract_items(self, page, url: str, page_no: int) -> list[VideoItem]:
        """从当前页面提取列表条目（不导航，供正常解析和点“下一页”复用）。"""
        b = self.manager.browser
        js = _CARD_JS % {
            "title_sels": json.dumps(",".join(b.title_selectors) or ".rank-title"),
            "card_sel": json.dumps(b.card_title_selector),
        }
        els: list[dict] = []
        for _ in range(10):
            self._check_stop()
            try:
                els = await page.evaluate(js) or []
            except Exception:
                els = []
            if els and any((x.get("t") or x.get("a") or x.get("ti")) for x in els):
                break
            await self._wait(page, 1200)

        out: list[VideoItem] = []
        seen: set[str] = set()
        play_hint = self.manager.link.play_hint_re
        for x in els:
            h = x.get("h") or ""
            if not h or h in seen:
                continue
            if play_hint and not play_hint.search(h):
                continue
            seen.add(h)
            t = (
                self.cleaner.clean(x.get("t") or "")
                or self.cleaner.clean(x.get("a") or "")
                or self.cleaner.clean(x.get("ti") or "")
            )
            if self.cleaner.is_generic(t):
                t = ""
            out.append(VideoItem(id=abs_url(h, url), title=t, page=page_no))
        return out

    async def _click_next(self, page) -> bool:
        """点「下一页/加载更多」；URL 不变、纯 JS 换内容的列表靠它翻页。"""
        b = self.manager.browser
        next_sel = b.next_page_selector or ".pg-btn, .load-more button"
        texts = b.next_page_texts or ["下一页", "加载更多", "更多"]
        before = page.url
        for txt in texts:
            try:
                loc = page.locator(next_sel, has_text=txt)
                if await loc.count() > 0:
                    await loc.first.click(force=True)
                    await self._after_click(page, before)
                    return True
            except (StopJob, JobAborted):
                raise
            except Exception:
                continue
        # DOM 结构不匹配规则时，按文字/aria-label 找可点击的翻页元素
        try:
            clicked = await page.evaluate(self._NEXT_JS)
        except Exception:
            clicked = False
        if clicked:
            await self._after_click(page, before)
            return True
        return False

    async def _after_click(self, page, before_url: str) -> None:
        """等一小会儿让新一页加载完；URL 变了就等 DOM 完成。"""
        if page.url != before_url:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
        await self._wait(page, 2000)

    async def _extract_until_new(
        self, page, url: str, page_no: int, seen: set[str], timeout_s: float = 8.0
    ) -> list[VideoItem]:
        """点完下一页后等新条目出现：SPA 换内容有延迟，不能拿到旧卡片就收手。"""
        deadline = time.monotonic() + timeout_s
        while True:
            items = await self._extract_items(page, url, page_no)
            if any(it.id not in seen for it in items):
                return items
            if time.monotonic() >= deadline:
                return items
            await self._wait(page, 500)

    async def _scroll_more(self, page, url: str, page_no: int, seen: set[str]) -> bool:
        """没有「下一页」按钮时尝试无限滚动/下拉加载：滚到底看是否出现新条目。"""
        for _ in range(4):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                return False
            await self._wait(page, 1200)
            items = await self._extract_items(page, url, page_no)
            if any(it.id not in seen for it in items):
                return True
        return False

    async def collect_via_next_click(self, page, first_url: str, p1: int, p2: int) -> list[VideoItem]:
        """URL 翻页无效时（page 参数被忽略/纯 JS 换内容）用浏览器点下一页采集。

        ``first_url`` 必须是**第 1 页**的地址：点翻页只能从第 1 页开始逐页前进。
        前 p1-1 页只点不收集，从 p1 页开始按逻辑页码标记 item.page，最多走到 p2。
        某一页点完没有新条目就停止（说明已到末尾或按钮失效）。
        """
        await self._goto(page, first_url)
        await self._wait(page, 1500)
        out: list[VideoItem] = []
        seen: set[str] = set()
        page_no = 1
        items = await self._extract_items(page, first_url, page_no)
        while True:
            fresh = [it for it in items if it.id not in seen]
            if not fresh and page_no > 1:
                break
            for it in fresh:
                seen.add(it.id)
                if page_no >= p1:
                    it.page = page_no
                    out.append(it)
            if page_no >= p2:
                break
            if await self._click_next(page):
                page_no += 1
                items = await self._extract_until_new(page, first_url, page_no, seen)
                continue
            # 没有翻页按钮：可能是无限滚动 / 下拉加载更多
            if await self._scroll_more(page, first_url, page_no + 1, seen):
                page_no += 1
                items = await self._extract_items(page, first_url, page_no)
                continue
            break
        return out

    async def _collect_by_click(self, page, url: str, page_no: int) -> list[VideoItem]:
        """SPA 列表兜底: 逐个点击卡片, 让前端路由跳到详情页, 再从地址栏取 URL。

        代价是每张卡片都要经历一次「点击 -> 路由跳转 -> 后退」, 所以只在前面的
        链接提取一无所获时启用, 并有数量上限(规则里的 card_click_max)。
        """
        b = self.manager.browser
        cards_sel = ", ".join(b.card_selectors)
        if not cards_sel:
            return []
        click_js = _CLICK_JS % {"cards": json.dumps(cards_sel), "card_sel": json.dumps(b.card_title_selector)}
        try:
            await page.wait_for_selector(cards_sel, timeout=12000)
            n = await page.locator(cards_sel).count()
        except Exception:
            return []
        if not n:
            return []
        play_hint = self.manager.link.play_hint_re
        host = urlparse(url).netloc
        out: list[VideoItem] = []
        seen: set[str] = set()

        async def back_to_list() -> bool:
            if page.url.rstrip("/") != url.rstrip("/"):
                try:
                    await page.go_back(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            if not page.url.startswith("http"):
                # 路由跳转还没落盘就后退会落到 about:blank, 直接重新打开列表页
                try:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    return False
            try:
                await page.wait_for_selector(cards_sel, timeout=10000)
            except Exception:
                try:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_selector(cards_sel, timeout=10000)
                except Exception:
                    return False
            return True

        for i in range(min(n, max(1, b.card_click_max))):
            self._check_stop()
            if i and not await back_to_list():
                break
            try:
                title = await page.evaluate(click_js, i) or ""
            except Exception:
                continue
            try:
                await page.wait_for_url(lambda u: u.rstrip("/") != url.rstrip("/"), timeout=8000)
            except Exception:
                continue
            href = page.url
            if not href.startswith("http") or href.rstrip("/") == url.rstrip("/") or href in seen:
                continue
            # 卡片点进去一般是详情页; 外域链接当广告丢掉(同域或无 play 特征也收下)
            if urlparse(href).netloc != host and not (play_hint and play_hint.search(href)):
                continue
            seen.add(href)
            t = self.cleaner.clean(title)
            if self.cleaner.is_generic(t):
                t = ""
            out.append(VideoItem(id=href, title=t, page=page_no))
        return out
