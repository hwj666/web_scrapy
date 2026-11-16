"""域名探测：直连 → HTTP 30x 采用最终域名 → DoH CNAME 兜底 → 发布页/JS 跳转跟随。"""
from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from app.core.exceptions import JobAborted, StopJob
from app.core.models import JobConfig
from app.core.rules import RuleManager
from app.toolkit.codec import decode_body
from app.toolkit.dns import cdn_alias
from app.toolkit.domain_notice import (
    clean_host,
    close_pages,
    page_notice_host,
    page_opener_matches,
    replace_host,
)
from app.toolkit.url import auto_list_url


class DomainProbe:
    """按候选列表链接探测当前可用域名，并在运行期做冷却重探。"""

    def __init__(self, cfg: JobConfig, rules: RuleManager, *,
                 get_raw: Callable[[str], Awaitable[Any]],
                 new_page: Callable[[], Any],
                 log: Callable[[str], None],
                 ua: str):
        self.cfg = cfg
        self.rules = rules
        self._get_raw = get_raw
        self._new_page = new_page
        self.log = log
        self.ua = ua
        self._lock = threading.Lock()
        self._reprobe_at = 0.0
        # 每次域名切换 +1；HLS 下载器用它判断“是不是该丢弃旧 m3u8 重新解析”
        self.epoch = 0
        # 旧 host -> 新 host；换域名后历史上已入队的绝对 URL 也能重写
        self._host_map: dict[str, str] = {}

    def _remap(self, old_host: str, new_host: str) -> None:
        old = (old_host or "").lower()
        new = (new_host or "").lower()
        if not old or not new or old == new:
            return
        self._host_map[old] = new
        for key, value in list(self._host_map.items()):
            if value == old:
                self._host_map[key] = new

    def set_base(self, list_url: str) -> None:
        old_host = urlparse(self.cfg.base).netloc
        new_host = urlparse(list_url).netloc
        if old_host and new_host and old_host.lower() != new_host.lower():
            self.epoch += 1
        self._remap(old_host, new_host)
        self.cfg.list_url = list_url
        up = urlparse(list_url)
        self.cfg.base = f"{up.scheme}://{up.netloc}"

    def rebase_url(self, url: str) -> str:
        """域名切换后，把旧域名的绝对 URL 改到新域名（保留路径/查询串）。"""
        up = urlparse(url)
        new = self._host_map.get(up.netloc.lower())
        if not new:
            return url
        return replace_host(url, {up.netloc}, new)

    def adopt_host(self, host: str) -> bool:
        """从发布页/弹窗识别到新域名后，统一切换 list_url/candidates/base。"""
        new_host = clean_host(host)
        if not new_host:
            return False
        scheme = urlparse(self.cfg.base).scheme or "https"
        current_host = urlparse(self.cfg.base).netloc.lower()
        if not current_host or new_host == current_host:
            return False
        old_hosts = {current_host}
        old_hosts.update(urlparse(u).netloc.lower() for u in self.cfg.candidates)
        old_hosts.discard("")
        for old in old_hosts:
            self._remap(old, new_host)
        self.cfg.list_url = replace_host(self.cfg.list_url, old_hosts, new_host)
        self.cfg.candidates = [replace_host(u, old_hosts, new_host)
                               for u in self.cfg.candidates]
        self.cfg.base = f"{scheme}://{new_host}"
        self.epoch += 1
        self.log(f"识别到站点新域名, 自动切换: {new_host}")
        return True

    def _adopt_redirect(self, tpl: str, final_url: str) -> str:
        old, new = urlparse(tpl), urlparse(final_url or "")
        if not old.netloc or not new.netloc or new.netloc == old.netloc:
            return tpl
        if new.scheme not in ("http", "https"):
            return tpl
        self.log(f"页面跳转到 {new.netloc}, 自动改用新域名(原 {old.netloc})")
        return tpl.replace(f"{old.scheme}://{old.netloc}",
                           f"{new.scheme}://{new.netloc}", 1)

    def _adopt_notice_host(self, tpl: str, host: str) -> str:
        old = urlparse(tpl)
        if not old.netloc or not host or old.netloc.lower() == host.lower():
            return tpl
        self.log(f"页面提示新域名 {host}, 自动切换")
        return replace_host(tpl, {old.netloc}, host)

    async def _click_publish(self, page) -> None:
        for text in self.rules.pipeline.publish_click_texts:
            try:
                await page.get_by_text(text, exact=False).first.click(timeout=1500)
                await page.wait_for_timeout(2500)
                return
            except Exception:
                continue

    @staticmethod
    async def _page_text(page) -> str:
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        return f"{title}\n{body}"[:50000]

    async def resolve_base(self, cand: str, p1: int = 1) -> str | None:
        tpl = cand.strip()
        probe = auto_list_url(tpl, p1)
        try:
            r0 = await self._get_raw(probe)
        except (StopJob, JobAborted):
            raise
        except Exception as e:
            self.log(f"{probe} 连接失败({type(e).__name__}), 尝试同站其他主机名")
            alt = await cdn_alias(tpl, self.ua)
            if alt:
                self.log(f"域名被阻断, 自动改用同站主机名: {probe} -> {auto_list_url(alt, p1)}")
            else:
                self.log(f"无可用替代主机名: {probe}")
            return alt
        tpl = self._adopt_redirect(tpl, str(r0.url))
        if r0.status >= 400:
            self.log(f"提示: {probe} 返回 HTTP {r0.status}, 该链接可能不是列表页")
            return tpl
        head = decode_body(r0.content, r0.headers.get("Content-Type", ""))[:30000]
        if not any(k in head for k in self.rules.pipeline.shell_hints) \
                and not (self.rules.pipeline.redirect_hint_re
                         and self.rules.pipeline.redirect_hint_re.search(head)):
            return tpl

        popups: list[Any] = []
        ctx = None
        listener = None
        try:
            async with self._new_page() as (page, _hits):
                ctx = page.context

                def _on_page(new_page) -> None:
                    if page_opener_matches(new_page, page):
                        popups.append(new_page)

                ctx.on("page", _on_page)
                listener = _on_page
                await page.goto(probe, timeout=30000)
                await page.wait_for_timeout(4000)
                text = await self._page_text(page)
                if any(k in text for k in self.rules.pipeline.shell_hints) \
                        or "发布页" in text:
                    await self._click_publish(page)
                    # 弹窗可能延迟打开，给它一点时间
                    for _ in range(8):
                        if popups:
                            break
                        await page.wait_for_timeout(500)
                exclude = {urlparse(self.cfg.base).netloc.lower()}
                host = await page_notice_host(
                    page, self.rules.pipeline.new_domain_re,
                    self.rules.pipeline.shell_hints, exclude)
                for popup in popups:
                    try:
                        await popup.wait_for_load_state(
                            "domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    if host:
                        continue
                    host = await page_notice_host(
                        popup, self.rules.pipeline.new_domain_re,
                        self.rules.pipeline.shell_hints, exclude)
                if host:
                    tpl = self._adopt_notice_host(tpl, host)
                else:
                    tpl = self._adopt_redirect(tpl, page.url)
        except Exception as e:
            self.log(f"发布页跟随: {type(e).__name__} {str(e)[:80]}")
        finally:
            if ctx is not None and listener is not None:
                try:
                    ctx.remove_listener("page", listener)
                except Exception:
                    pass
            await close_pages(popups)
        return tpl

    async def reprobe(self, why: str = "", force: bool = False) -> bool:
        now = time.time()
        with self._lock:
            # 运行期分片连续失败时允许绕过 300 秒冷却，但仍限 5 秒一次，避免并发视频同时重探
            interval = 5.0 if force else self.rules.pipeline.reprobe_cooldown
            if now - self._reprobe_at < interval:
                return False
            self._reprobe_at = now
        cands = list(dict.fromkeys(
            x.strip() for x in ([self.cfg.list_url] + list(self.cfg.candidates))
            if x and x.strip()))
        for cand in cands:
            try:
                got = await self.resolve_base(cand, self.cfg.p1)
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                self.log(f"域名重探失败 {cand}: {type(e).__name__} {str(e)[:80]}")
                continue
            if not got:
                continue
            if got.strip() == self.cfg.list_url.strip():
                self.log(f"域名重探: 当前域名仍可访问{(' (' + why + ')') if why else ''}")
                return False
            self.set_base(got)
            self.log(f"域名已变更, 自动切换{(' (' + why + ')') if why else ''}: {self.cfg.base}")
            return True
        self.log(f"域名重探: 所有候选都不可用{(' (' + why + ')') if why else ''}")
        return False

    def reset_cooldown(self) -> None:
        with self._lock:
            self._reprobe_at = 0.0
