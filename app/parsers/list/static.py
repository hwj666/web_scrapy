"""通用静态列表解析解析器：无站点知识，靠规则引擎 + 模式聚类提取播放页链接。"""
from __future__ import annotations

import asyncio
import html as html_mod
import re
from collections import Counter
from urllib.parse import urlparse

from app.core.models import ListPage, VideoItem
from app.core.parsing import ListParser, ParserContext
from app.core.rules import RuleManager
from app.toolkit.codec import decode_page
from app.toolkit.title import TitleCleaner
from app.toolkit.url import abs_url


class StaticListParser(ListParser):
    name = "static_list"

    def __init__(self, ctx: ParserContext | None = None):
        super().__init__(ctx)
        self.manager: RuleManager = (self.ctx.state or {}).get("rules") or RuleManager()
        self.cleaner = TitleCleaner(self.manager)

    def _clean_title(self, text: str) -> str:
        """列表标题也要过一遍标题清洗规则：去站名/套话/前后缀，过滤泛化标题。"""
        title = self.cleaner.clean(text or "")
        if len(title) < 3 or len(title) > 40 or self.cleaner.is_generic(title):
            return ""
        return title

    async def parse(self, html: str, page: int, base: str) -> ListPage:
        # HTML 正则/模式聚类是纯 CPU，丢到线程里解析，避免大页面卡住事件循环
        return await asyncio.to_thread(self._parse_sync, html, page, base)

    def _parse_sync(self, html: str, page: int, base: str) -> ListPage:
        rules = self.manager.link
        d = decode_page(html)
        out: list[VideoItem] = []
        seen: set[str] = set()

        def add(href: str, title: str) -> None:
            if href in seen:
                return
            seen.add(href)
            out.append(VideoItem(id=href, title=title, page=page))

        for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', d, re.S | re.I):
            href, inner = m.group(1), m.group(2)
            if href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            if rules.ext_re and rules.ext_re.search(href):
                continue
            if rules.exclude_re and rules.exclude_re.search(href):
                continue
            if rules.ad_re and rules.ad_re.search(href):
                continue
            txt = html_mod.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
            title = self._clean_title(txt)
            # 链接是否像视频页看原始文本，别因为标题清洗成空就把候选也丢了
            if ((rules.play_hint_re and rules.play_hint_re.search(href))
                    or (txt and rules.id_re and rules.id_re.search(href))):
                add(abs_url(href, base), title)

        if not out and rules.div_to_re:
            for m in rules.div_to_re.finditer(d):
                href, blk = m.group(1), m.group(2)
                if not (rules.play_hint_re and rules.play_hint_re.search(href)):
                    continue
                tm = re.search(r">([^<>]{4,40})<", blk)
                add(abs_url(href, base), self._clean_title(tm.group(1)) if tm else "")

        cands = [x for x in out if not re.fullmatch(r"[/#?]+", x.id)]
        if len(cands) >= 4:
            top, n = Counter(self._link_pat(x.id) for x in cands).most_common(1)[0]
            if n >= 3:
                if rules.play_hint_re and rules.play_hint_re.search(top):
                    cands = [x for x in cands if self._link_pat(x.id) == top]
                else:
                    cands = []
        # 静态候选全是「外域绝对链接」且数量很少 -> 多为广告/跳转页，交给浏览器兜底。
        # 注意只排除外域：同域绝对链接（如 /play/1.html 被 abs_url 转成绝对地址）
        # 不能因为数量少就丢掉，否则小分类页会被误判成空页。
        base_host = urlparse(base).netloc
        if cands and len(cands) < 8 and all(
                urlparse(x.id).netloc and urlparse(x.id).netloc != base_host
                for x in cands):
            cands = []

        return ListPage(page=page, items=cands)

    @staticmethod
    def _link_pat(h: str) -> str:
        if "://" not in h:
            h = "http://x" + (h if h.startswith("/") else "/" + h)
        pu = urlparse(h)
        return re.sub(r"\d+", "N", pu.netloc + pu.path)
