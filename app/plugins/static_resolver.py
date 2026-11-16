"""通用静态播放页解析插件：多形态解码 → m3u8 → maccms 加密 blob 试解 → iframe 递归。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import unquote, urljoin, urlparse

from Crypto.Cipher import AES

from app.core.exceptions import NewDomainError
from app.core.models import ResolvedVideo, VideoItem
from app.core.plugins import PluginContext, ResolverPlugin
from app.core.rules import RuleManager
from app.toolkit.codec import unpad_pkcs7
from app.toolkit.domain_notice import find_new_host
from app.toolkit.title import TitleCleaner, page_title
from app.toolkit.url import abs_url


class StaticResolver(ResolverPlugin):
    name = "static_resolver"

    def __init__(self, ctx: PluginContext | None = None):
        super().__init__(ctx)
        self.manager: RuleManager = (self.ctx.state or {}).get("rules") or RuleManager()
        self.cleaner = TitleCleaner(self.manager)

    async def _fetch(self, url: str) -> str:
        return await self.ctx.fetch(url)

    async def resolve(self, item: VideoItem, base: str) -> ResolvedVideo:
        url = abs_url(item.id, base)
        text = await self._fetch(url)
        return await self._resolve_text(text, url, base)

    async def _resolve_text(self, text: str, url: str, base: str) -> ResolvedVideo:
        exclude = {urlparse(url).netloc.lower(), urlparse(base).netloc.lower()}
        exclude.discard("")
        d2: str | None = None
        for cand in self._text_variants(text):
            mu = self._pick_m3u8(cand)
            if mu:
                # 标题必须从「命中 m3u8 的那份文本」里取：document.write(decodeURIComponent)
                # 型页面只有解码后的正文才有 .video-title，原始 HTML 里只有站名 <title>
                title = self._title(cand) or self._title(text)
                return ResolvedVideo(m3u8_url=self._abs(mu, url), title=title)
        t = self._try_decrypt_blob(text)
        if t:
            mu = self._pick_m3u8(t)
            if mu:
                name = self._extract_vod_name(t)
                return ResolvedVideo(m3u8_url=self._abs(mu, url),
                                     title=name or self._title(text))
        m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.I)
        if m:
            src = self._abs(m.group(1), url)
            d2 = await self._fetch(src)
            for cand in self._text_variants(d2):
                mu = self._pick_m3u8(cand)
                if mu:
                    title = self._title(cand) or self._title(d2)
                    return ResolvedVideo(m3u8_url=self._abs(mu, src), title=title)
            t2 = self._try_decrypt_blob(d2)
            if t2:
                mu = self._pick_m3u8(t2)
                if mu:
                    return ResolvedVideo(m3u8_url=self._abs(mu, src), title=self._title(d2))
        # 播放页可能只是「站点已换域名」的发布页：识别出来后让上层切域名重试
        for page_text in (text, d2):
            if not page_text:
                continue
            if not any(k in page_text for k in self.manager.pipeline.shell_hints):
                continue
            host = find_new_host(page_text, self.manager.pipeline.new_domain_re, exclude)
            if host:
                raise NewDomainError(host)
        raise RuntimeError("未能自动提取 m3u8")

    def _pick_m3u8(self, txt: str) -> str | None:
        rules = self.manager.resolver
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("//"):
                continue
            m = re.search(r'["\']([^"\']*\.m3u8[^"\']*)["\']', s)
            if m:
                u = self._fix(m.group(1))
                if u:
                    return u
        if rules.m3u8_re:
            m = rules.m3u8_re.search(txt)
            return self._fix(m.group(0)) if m else None
        return None

    @staticmethod
    def _fix(u: str) -> str | None:
        u = re.split(r"%(?:0D|0A|09|3B|20)|[<>]", u)[0]
        if ".m3u8" not in u:
            return None
        if u.startswith("//"):
            u = "https:" + u
        return u.replace("\\/", "/")

    def _text_variants(self, txt: str):
        yield txt
        m = re.search(r'decodeURIComponent\("([^"]+)"\)', txt)
        if m:
            try:
                yield unquote(m.group(1))
            except Exception:
                pass
        try:
            u2 = unquote(txt)
            if u2 != txt:
                yield u2
        except Exception:
            pass
        for b in re.findall(r'["\']([A-Za-z0-9+/=_-]{60,})["\']', txt):
            s2 = b.replace("-", "+").replace("_", "/")
            s2 += "=" * (-len(s2) % 4)
            try:
                dec = base64.b64decode(s2).decode("utf-8", "ignore")
            except Exception:
                continue
            if dec and ("m3u8" in dec or "{" in dec):
                yield dec

    def _try_decrypt_blob(self, txt: str) -> str | None:
        rules = self.manager.resolver
        if not rules.crypt_key_re:
            return None
        keys = rules.crypt_key_re.findall(txt)
        if not keys:
            return None
        blobs = []
        if rules.aes_call_re:
            blobs = rules.aes_call_re.findall(txt)
        if not blobs and rules.blob_re:
            blobs = rules.blob_re.findall(txt)[:4]
        for blob in blobs:
            s2 = blob.replace("-", "+").replace("_", "/")
            s2 += "=" * (-len(s2) % 4)
            try:
                raw = base64.b64decode(s2)
            except Exception:
                continue
            for ks in keys:
                for key in (hashlib.sha256(ks.encode()).digest(), ks.encode(),
                            hashlib.md5(ks.encode()).digest()):
                    if len(key) not in (16, 24, 32):
                        continue
                    for mode, kw in ((AES.MODE_ECB, {}), (AES.MODE_CBC, {"iv": b"\0" * 16})):
                        try:
                            pt = unpad_pkcs7(AES.new(key, mode, **kw).decrypt(raw))
                            t = pt.decode("utf-8", "ignore")
                            if "m3u8" in t or "vod_name" in t:
                                return t
                        except Exception:
                            continue
        return None

    def _extract_vod_name(self, t: str) -> str:
        rules = self.manager.resolver
        if not rules.vod_name_re:
            return ""
        m = rules.vod_name_re.search(t)
        if not m:
            return ""
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)

    def _title(self, html: str) -> str:
        return page_title(html, self.cleaner, self.manager.link.title_block_keys)

    @staticmethod
    def _abs(u: str, page_url: str) -> str:
        if u.startswith(("http://", "https://")):
            return u
        return urljoin(page_url, u)
