"""浏览器播放页解析解析器（playwright 兜底）：网络层拦截 .m3u8，主动触发播放。"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from app.core.exceptions import NewDomainError
from app.core.models import ResolvedVideo, VideoItem
from app.core.parsing import ParserContext, Resolver
from app.core.rules import RuleManager
from app.toolkit.domain_notice import close_pages, page_notice_target, page_opener_matches
from app.toolkit.title import TitleCleaner
from app.toolkit.url import abs_url


def _trigger_js(selectors: list[str]) -> str:
    sels = json.dumps(selectors or [])
    return """() => {
      const done = [];
      document.querySelectorAll('video').forEach(v => {
        try { v.muted = true; v.setAttribute('muted','');
              const p = v.play(); if (p && p.catch) p.catch(()=>{});
              done.push('video'); } catch (e) {}
      });
      const sels = %s;
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el) { try { el.click(); done.push(s); } catch (e) {} }
      }
      return done;
    }""" % sels


def _title_js(selectors: list[str]) -> str:
    title_sel = json.dumps(",".join(selectors) or ".rank-title")
    return """() => {
      const one = (s) => { const e = document.querySelector(s); return e ? (e.getAttribute('content') || '').trim() : ''; };
      const text = (s) => [...document.querySelectorAll(s)].map(e => (e.textContent || '').trim()).filter(Boolean);
      return {
        og: one('meta[property="og:title"]') || one('meta[name="twitter:title"]'),
        meta: one('meta[name="title"]'),
        site: [one('meta[property="og:site_name"]'), one('meta[name="application-name"]')].filter(Boolean),
        h1: [...document.querySelectorAll('h1')].filter(e => !/logo|header|nav/i.test(`${e.className || ''} ${e.id || ''}`)).map(e => (e.textContent || '').trim()).filter(Boolean),
        known: text(%s),
        doc: document.title || ''
      };
    }""" % title_sel


class BrowserResolver(Resolver):
    name = "browser_resolver"
    uses_browser = True

    def __init__(self, ctx: ParserContext | None = None):
        super().__init__(ctx)
        self.manager: RuleManager = (self.ctx.state or {}).get("rules") or RuleManager()
        self.cleaner = TitleCleaner(self.manager)
        self._trigger_js = _trigger_js(self.manager.browser.trigger_selectors)
        self._title_js = _title_js(self.manager.browser.title_selectors)

    async def resolve(self, item: VideoItem, base: str) -> ResolvedVideo:
        raise NotImplementedError("browser_resolver 需要 page 对象，请使用 resolve_with_page")

    async def resolve_with_page(self, page, hits: list[str], item: VideoItem,
                                base: str) -> ResolvedVideo:
        url = abs_url(item.id, base)
        del hits[:]
        exclude = {urlparse(base).netloc.lower(), urlparse(url).netloc.lower()}
        exclude.discard("")
        popups: list = []
        ctx = page.context

        def _on_request(req) -> None:
            if ".m3u8" in req.url and len(hits) < 200:
                hits.append(req.url)

        def _on_page(new_page) -> None:
            if page_opener_matches(new_page, page):
                popups.append(new_page)
                new_page.on("request", _on_request)

        ctx.on("page", _on_page)
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            m3u8s = []
            for i in range(12):  # 最多等 30 秒
                await page.wait_for_timeout(2500)
                m3u8s = [h for h in hits if ".m3u8" in h]
                if m3u8s:
                    break
                try:
                    await page.evaluate(self._trigger_js)
                except Exception:
                    pass
                if i in (1, 3, 6):  # 再用真实鼠标点一次播放器中心，应付挡在最上层的遮罩
                    try:
                        box = await page.evaluate(
                            "() => { const v = document.querySelector('video')"
                            " || document.querySelector('#player') || document.body;"
                            " const r = v.getBoundingClientRect();"
                            " return {x: r.x + r.width/2, y: r.y + r.height/2}; }")
                        await page.mouse.click(box["x"], box["y"])
                    except Exception:
                        pass
                # 弹窗已经加载出“新域名”提示时不用再等满 30 秒
                if popups and not any(".m3u8" in h for h in hits):
                    for popup in popups:
                        host, _notice_url = await page_notice_target(
                            popup, self.manager.pipeline.new_domain_re,
                            self.manager.pipeline.shell_hints, exclude)
                        if host:
                            raise NewDomainError(host)
            if m3u8s:
                return ResolvedVideo(m3u8_url=m3u8s[-1],
                                     title=await self._browser_title(page))

            # 没拿到 m3u8：检查当前页和弹窗，是否在提示“站点已换新域名”
            host, _notice_url = await page_notice_target(
                page, self.manager.pipeline.new_domain_re,
                self.manager.pipeline.shell_hints, exclude)
            for popup in popups:
                try:
                    await popup.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                if host:
                    continue
                host, _notice_url = await page_notice_target(
                    popup, self.manager.pipeline.new_domain_re,
                    self.manager.pipeline.shell_hints, exclude)
            if host:
                raise NewDomainError(host)
            raise RuntimeError("未发出 m3u8")
        finally:
            try:
                ctx.remove_listener("page", _on_page)
            except Exception:
                pass
            await close_pages(popups)

    async def _browser_title(self, page) -> str:
        try:
            data = await page.evaluate(self._title_js) or {}
        except Exception:
            return ""
        doc = (data.get("doc") or "").strip()
        site = [x for x in (data.get("site") or []) if x]
        doc_clean = self.cleaner.clean(doc)
        if (doc_clean and not self.cleaner.has_separator(doc)
                and (self.cleaner.looks_like_site(doc_clean)
                     or any(doc_clean == str(x).strip() for x in site))):
            site.append(doc)
        cands = []
        if data.get("og"):
            cands.append((100, data["og"]))
        if data.get("meta"):
            cands.append((95, data["meta"]))
        cands += [(90, t) for t in (data.get("known") or [])]
        cands += [(85, t) for t in (data.get("h1") or [])]
        cands.append((40, doc))
        return self.cleaner.pick(cands, site) or self.cleaner.from_title_tag(doc, site)
