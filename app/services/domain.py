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
from app.services.list_url import (
    DEFAULT_PATTERNS,
    MAX_CANDIDATES,
    _origin,
    candidate_templates,
    category_id,
    is_detail_path,
    is_list_route,
    links_in,
    pick_chain_host,
    pick_link_for_host,
    pick_new_site_link,
    scheme_variants,
    templates_from_homepage,
    templates_from_url,
    text_of_html,
)
from app.toolkit.codec import decode_body
from app.toolkit.dns import cdn_alias
from app.toolkit.domain_notice import (
    clean_host,
    close_pages,
    find_new_url,
    page_notice_target,
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
        self.epoch = 0        # 旧 host -> 新 host；换域名后历史上已入队的绝对 URL 也能重写
        self._host_map: dict[str, str] = {}
        # 发布页里给出的完整地址（可能带新站点的列表路径）
        self._notice_url = ""

    def _remap(self, old_host: str, new_host: str) -> None:
        old = (old_host or "").lower()
        new = (new_host or "").lower()
        if not old or not new or old == new:
            return
        self._host_map[old] = new
        for key, value in list(self._host_map.items()):
            if value == old:
                self._host_map[key] = new

    def _looks_like_list(self, content: bytes) -> bool:
        """粗略判断页面是不是列表页：至少两个像播放页的链接。"""
        if not content:
            return False
        text = decode_body(content, "")[:200000]
        hint = self.rules.link.play_hint_re
        if hint is None:
            return len(text) > 2000
        # 真列表页通常有大量详情链接；目录页/单条页不能算列表页
        return len(hint.findall(text)) >= 5

    def _looks_like_publish(self, content: bytes) -> bool:
        """发布页/跳转页：带换域名提示、但几乎没有详情链接。"""
        if not content:
            return False
        if self._looks_like_list(content):
            return False
        text = decode_body(content, "")[:30000]
        return any(k in text for k in self.rules.pipeline.shell_hints)

    async def _list_ok(self, tpl: str, p1: int = 1) -> bool:
        try:
            r = await self._get_raw(auto_list_url(tpl, p1))
        except (StopJob, JobAborted):
            raise
        except Exception:
            return False
        return r.status < 400 and self._looks_like_list(r.content)

    async def _follow_notice_chain(self, tpl: str, cat: str) -> str:
        """跟随「换域名跳转页」链路：A 域名 → 跳转页 → 跳转页上写的新网址。

        很多站的旧域名不会 30x 到新站，而是先跳到一个“发布页/说明页”，
        上面才写着真正的新地址；只采纳第一跳域名会一直 404。
        """
        visited: set[str] = set()
        for _ in range(5):
            origin = _origin(tpl)
            host = urlparse(origin).netloc.lower() if origin else ""
            if not origin or not host or host in visited:
                return tpl
            visited.add(host)
            # 换过域名后先验证当前域名是否已经有可用列表页；能用就停止跟随，
            # 否则会跟着发布页里的广告/统计域名继续乱跳。
            if await self._list_ok(tpl, self.cfg.p1):
                self.log(f"当前域名列表页可用, 停止跟随跳转: {host}")
                return tpl
            try:
                r = await self._get_raw(origin + "/")
            except (StopJob, JobAborted):
                raise
            except Exception as e:
                self.log(f"跳转页探测失败({type(e).__name__}): {str(e)[:80]}")
                return tpl
            html = (decode_body(r.content, r.headers.get("Content-Type", ""))
                    if r.content else "")
            final_host = clean_host(str(r.url))

            # 1) HTTP 跳转到了另一个域名
            if final_host and final_host != host:
                self.log(f"跳转页指向新域名: {host} -> {final_host}")
                if self.adopt_host(final_host):
                    tpl = replace_host(tpl, {host}, final_host)
                    continue

            # 2) 页面上直接给出其它站点的列表链接（最可靠）
            links = links_in(html, origin)
            link = pick_new_site_link(links, visited | {host}, cat)
            if link:
                link_host = clean_host(link)
                self.log(f"跳转页给出新站点列表地址: {link}")
                tmpl_list = templates_from_url(link, cat) or [link]
                if link_host and link_host not in visited:
                    self.adopt_host(link_host)
                self._notice_url = link
                return tmpl_list[0]

            # 3) 页面文字里写的新域名（可能同时带完整列表地址）
            new_host, new_url = find_new_url(
                text_of_html(html), self.rules.pipeline.new_domain_re,
                visited | {host})
            if not new_host:
                # 4) 任意指向其它站点的链接（“点此进入新站”按钮）
                for cand_link in links:
                    cand_host = clean_host(cand_link)
                    if cand_host and cand_host not in visited:
                        new_host = cand_host
                        break
            if not new_host and not link:
                # 地址发布页常把真实域名写进 <script>：JS 跳转/加密配置/入口链接
                script_host = pick_chain_host(html, visited | {host})
                if script_host:
                    self.log(f"跳转页脚本里发现新域名: {host} -> {script_host}")
                    if self.adopt_host(script_host):
                        tpl = replace_host(tpl, {host}, script_host)
                        continue

            if not new_host and not link:
                # 跳转页可能是 JS 渲染的：用浏览器读一次，再按上面同样的规则找
                bhtml = await self._browser_html(origin + "/")
                if bhtml and bhtml != html:
                    blinks = links_in(bhtml, origin)
                    blink = pick_new_site_link(blinks, visited | {host}, cat)
                    if blink:
                        bhost = clean_host(blink)
                        self.log(f"跳转页(浏览器)给出新站点列表地址: {blink}")
                        if bhost:
                            self.adopt_host(bhost)
                        self._notice_url = blink
                        return (templates_from_url(blink, cat) or [blink])[0]
                    new_host, new_url = find_new_url(
                        text_of_html(bhtml), self.rules.pipeline.new_domain_re,
                        visited | {host})
                    if not new_host:
                        script_host = pick_chain_host(bhtml, visited | {host})
                        if script_host:
                            self.log(f"跳转页(浏览器)脚本里发现新域名: "
                                     f"{host} -> {script_host}")
                            if self.adopt_host(script_host):
                                tpl = replace_host(tpl, {host}, script_host)
                                continue

            if new_host:
                self.log(f"跳转页提示新域名: {host} -> {new_host}")
                adopted = self.adopt_host(new_host)
                if new_url and is_list_route(new_url) and not is_detail_path(new_url):
                    self.log(f"跳转页给出新站点列表地址: {new_url}")
                    self._notice_url = new_url
                    return (templates_from_url(new_url, cat) or [new_url])[0]
                if adopted:
                    tpl = replace_host(tpl, {host}, new_host)
                    continue
            return tpl
        return tpl

    async def _browser_html(self, url: str) -> str:
        """用浏览器读页面 HTML（应对 JS 渲染或 WAF 拦截）。"""
        try:
            async with self._new_page() as (page, _hits):
                await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                return await page.content() or ""
        except Exception as e:
            self.log(f"浏览器读取失败 {url}: {type(e).__name__}: {str(e)[:80]}")
            return ""

    async def _homepage_templates(self, tpl: str, cat: str) -> list[str]:
        """抓新站点首页，从它自己的链接里推导列表模板（比盲猜路由可靠）。"""
        origin = _origin(tpl)
        if not origin:
            return []
        html = ""
        try:
            r = await self._get_raw(origin + "/")
            if r.status < 400 and r.content:
                html = decode_body(r.content, r.headers.get("Content-Type", ""))
                found = templates_from_homepage(html, origin, cat)
                if found:
                    self.log(f"从新域名首页发现 {len(found)} 个候选列表路径")
                    return found
                self.log("首页没有可用的分类链接, 改用浏览器读取首页")
            else:
                self.log(f"新域名首页返回 HTTP {r.status}, 改用浏览器读取首页")
        except (StopJob, JobAborted):
            raise
        except Exception as e:
            self.log(f"新域名首页抓取失败({type(e).__name__}), 改用浏览器: "
                     f"{str(e)[:80]}")

        # 首页由 JS 渲染或被 WAF 挡住时，用浏览器再读一次
        html = await self._browser_html(origin + "/")
        if not html:
            return []
        found = templates_from_homepage(html, origin, cat)
        if found:
            self.log(f"从新域名首页(浏览器)发现 {len(found)} 个候选列表路径")
        return found

    async def ensure_list_url(self, tpl: str, p1: int = 1,
                              strict: bool = False) -> str:
        """校验列表页是否可访问；404/不是列表页时尝试修复路径。

        换域名时只替换 host 会保留旧路径，而站点经常连列表路由一起改版，
        所以这里在拿到新域名后再验证一次真实页面。
        """
        if not tpl:
            return tpl
        try:
            r0 = await self._get_raw(auto_list_url(tpl, p1))
        except (StopJob, JobAborted):
            raise
        except Exception as e:
            self.log(f"列表页校验失败({type(e).__name__}), 尝试探测新站点的列表路径: "
                     f"{str(e)[:80]}")
            r0 = None
        # 原链接只要不是 4xx/5xx、也不是“发布页/跳转页”，就认为它是可用列表页；
        # 小分类只有一两条视频，不能因为链接少就误判并去改路径。
        if (r0 is not None and r0.status < 400
                and not self._looks_like_publish(r0.content)):
            return tpl
        if r0 is not None:
            self.log(f"列表页不可用(HTTP {r0.status}), 尝试探测新站点的列表路径")

        # 先跟随「跳转页 → 跳转页上写的新网址」链路；只采纳第一跳域名会一直 404
        chained = await self._follow_notice_chain(tpl, cat=category_id(tpl))
        if chained and chained != tpl:
            tpl = chained
            try:
                r1 = await self._get_raw(auto_list_url(tpl, p1))
            except (StopJob, JobAborted):
                raise
            except Exception:
                r1 = None
            if r1 is not None and r1.status < 400 and self._looks_like_list(r1.content):
                self.remember_list_url(tpl)
                self.log(f"列表路径已修复: {tpl}")
                return tpl

        cat = category_id(tpl)
        cands: list[str] = []
        # 1) 发布页给出的完整地址（最可信）
        if self._notice_url:
            cands.extend(templates_from_url(self._notice_url, cat))
            cands.append(self._notice_url)
        # 2) 新站点首页自己的链接（分类 id 或路由前缀）
        cands.extend(await self._homepage_templates(tpl, cat))
        # 3) 常见路由模板
        patterns = self.rules.pipeline.list_url_patterns or DEFAULT_PATTERNS
        cands.extend(candidate_templates(tpl, patterns))
        # 4) https/http 互备（换域名后协议经常不一致）
        for cand in list(cands):
            cands.extend(scheme_variants(cand))

        tried = 0
        for cand in dict.fromkeys(cands):
            if tried >= MAX_CANDIDATES:
                break
            if not cand or cand.rstrip("/") == tpl.rstrip("/"):
                continue
            if is_detail_path(cand):
                continue
            tried += 1
            try:
                r = await self._get_raw(auto_list_url(cand, p1))
            except (StopJob, JobAborted):
                raise
            except Exception:
                continue
            if r.status < 400 and self._looks_like_list(r.content):
                self.log(f"列表路径已修复: {cand}")
                self.remember_list_url(cand)
                self._notice_url = ""
                return cand
        msg = (f"列表页不可用且无法自动修复（已尝试 {tried} 个候选路径）。"
               f"站点可能已换域名或改版、分类 id 也可能变了，"
               f"请把新站点的列表页链接填进任务，"
               f"例如：{_origin(tpl)}/vodshow/分类id--------1---.html")
        self.log(msg)
        if strict:
            # 立刻失败，别再去逐页解析/开浏览器白跑几分钟
            raise RuntimeError(msg)
        return tpl

    def remember_list_url(self, tpl: str) -> None:
        """把修复后的列表模板加入候选，后续重探/换域名都优先用可用路径。"""
        if tpl and tpl not in self.cfg.candidates:
            self.cfg.candidates.append(tpl)

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

    @staticmethod
    async def _page_links(page) -> list[str]:
        """发布页上的链接：常常直接给出新站点的列表地址，比页面文字更可靠。"""
        try:
            got = await page.evaluate(
                "() => [...document.querySelectorAll('a[href]')].map(a => a.href)")
        except Exception:
            return []
        if not isinstance(got, list):
            return []
        return [str(x) for x in got if x]

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
                host, notice_url = await page_notice_target(
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
                    host, notice_url = await page_notice_target(
                        popup, self.rules.pipeline.new_domain_re,
                        self.rules.pipeline.shell_hints, exclude)
                if host:
                    cat = category_id(tpl)
                    links: list[str] = []
                    for pg in [page, *popups]:
                        links.extend(await self._page_links(pg))
                    picked = pick_link_for_host(links, host, cat)
                    self._notice_url = notice_url or picked or ""
                    if self._notice_url:
                        self.log(f"发布页给出了新站点地址: {self._notice_url}")
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
