"""标题清洗与选择 + 通用播放页标题提取（规则外置，与站点无关）。

静态解析与浏览器兜底共用同一套规则：把 HTML/文本整理成候选，再按可信度挑最像
视频名的那一个，期间用站名/页面套话/纯数字过滤。所有「规则」都来自
`app/rules/title_rules.yaml` 与 `link_rules.yaml`（title_block_keys）。
"""

from __future__ import annotations

import html as html_mod
import re
from collections.abc import Iterable

from app.core.rules import RuleManager

_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', re.S)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_TITLE_BLOCK_RE = re.compile(
    r"<(?P<tag>div|span|h1|h2|h3|h4|p|a|strong|em|li)\b(?P<attrs>[^>]*)>"
    r"(?P<inner>.*?)</(?P=tag)\s*>",
    re.S | re.I,
)
_LD_JSON_RE = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)


class TitleCleaner:
    """可配置的标题清洗器，规则全部来自 RuleManager。"""

    def __init__(self, rules: RuleManager | None = None):
        self.rules = rules or RuleManager()
        self._cache = self.rules.title.compiled()

    def clean(self, raw: str, limit: int = 60) -> str:
        t = str(raw or "")[:500]
        t = html_mod.unescape(t)
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"[\s\u3000]+", " ", t).strip(self._cache.get("wrap", "")).strip()
        if self._cache.get("prefix_re"):
            t = self._cache["prefix_re"].sub("", t)
        if self._cache.get("suffix_re"):
            t = self._cache["suffix_re"].sub("", t)
        t = re.sub(r"[\s\u3000]+", " ", t).strip(self._cache.get("wrap", "")).strip()
        return t[:limit].strip()

    def is_generic(self, text: str) -> bool:
        core = re.sub(r"[\W_]+", "", str(text or ""))
        if len(core) < 2:
            return True
        if core.lower() in self._cache.get("generic", set()):
            return True
        return bool(re.fullmatch(r"[\d.\-:/]+", core))

    def has_separator(self, text: str) -> bool:
        sep_re = self._cache.get("sep_re")
        return bool(sep_re and sep_re.search(html_mod.unescape(str(text or ""))))

    def looks_like_site(self, text: str) -> bool:
        site_re = self._cache.get("site_re")
        return bool(site_re and site_re.search(self.clean(text)))

    def pick(self, candidates: Iterable[tuple[int, str]], site_names: Iterable[str] = ()) -> str:
        best, best_score = "", -1
        site_names = [self.clean(x) for x in site_names if x]
        for score, raw in candidates:
            t = self._strip_known_site(self.clean(raw), site_names)
            if not t or self.is_generic(t):
                continue
            if score > best_score:
                best, best_score = t, score
        return best

    def from_title_tag(self, title_tag: str, site_names: Iterable[str] = ()) -> str:
        raw = html_mod.unescape(str(title_tag or ""))
        site_names = [self.clean(x) for x in site_names if x]
        sep_re = self._cache.get("sep_re")
        cleaned: list[str] = []
        for seg in sep_re.split(raw) if sep_re else [raw]:
            seg = self._strip_known_site(self.clean(seg), site_names)
            if seg and not self.is_generic(seg):
                cleaned.append(seg)
        if not cleaned:
            return ""
        ep_re = self._cache.get("episode_re")
        named = [s for s in cleaned if not (ep_re and ep_re.match(s))] or cleaned
        if len(named) > 1 and self.looks_like_site(named[0]) and not self.looks_like_site(named[-1]):
            return max(named[1:], key=len)
        return named[0]

    def _strip_known_site(self, text: str, site_names: list[str]) -> str:
        for sn in site_names:
            if len(sn) < 2:
                continue
            if self._norm(text) == self._norm(sn):
                return ""
            sep = r"\s*[_|｜·:：/\-–—]+\s*"
            for pat in (rf"^{re.escape(sn)}{sep}(.+)$", rf"^(.+?){sep}{re.escape(sn)}$"):
                m = re.match(pat, text)
                if m:
                    rest = self.clean(m.group(1))
                    return rest if rest and not self.is_generic(rest) else ""
        return text

    def _norm(self, text: str) -> str:
        return re.sub(r"[\W_]+", "", self.clean(text)).lower()


def _tag_attrs(tag: str) -> dict[str, str]:
    return {k.lower(): v for k, _, v in _ATTR_RE.findall(tag or "")}


def meta_values(html: str, *keys: str) -> list[str]:
    """按 property/name/itemprop 取 <meta content>，属性顺序无关。"""
    out: list[str] = []
    for m in _META_TAG_RE.finditer(html or ""):
        a = _tag_attrs(m.group(0))
        ident = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
        if ident in keys and a.get("content"):
            out.append(a["content"])
    return out


def page_title(html: str, cleaner: TitleCleaner, title_block_keys: Iterable[str]) -> str:
    """通用播放页标题提取：多来源候选 + 站名/套话过滤，不绑定任何站点 class。"""
    h = html or ""
    m = _TITLE_TAG_RE.search(h)
    title_tag = m.group(1) if m else ""
    site: list[str] = meta_values(h, "og:site_name", "application-name", "apple-mobile-web-app-title")
    t_clean = cleaner.clean(title_tag)
    # 没有分隔符的 <title> 可能是站名，也可能是不带站名后缀的视频名：
    # 只有像站名（含影院/影视/在线…）或与 og:site_name 相同时才当站名挡掉。
    if (
        t_clean
        and not cleaner.has_separator(title_tag)
        and (cleaner.looks_like_site(t_clean) or any(t_clean == str(x).strip() for x in site))
    ):
        site.append(title_tag)

    keys = tuple(k.lower() for k in title_block_keys)
    cands: list[tuple[int, str]] = [(100, v) for v in meta_values(h, "og:title", "twitter:title")]
    cands += [(95, v) for v in meta_values(h, "title")]
    for mm in _TITLE_BLOCK_RE.finditer(h):
        attrs = _tag_attrs(mm.group("attrs"))
        ident = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
        if any(k in ident for k in keys):
            cands.append((90, mm.group("inner")))
    for mm in _H1_RE.finditer(h):
        if not re.search(r"logo|header|nav", mm.group(0), re.I):
            cands.append((85, mm.group(1)))
    for mm in _LD_JSON_RE.finditer(h):
        for v in re.findall(r'"name"\s*:\s*"([^"]{2,120})"', mm.group(1)):
            cands.append((80, v))
    cands += [(60, v) for v in meta_values(h, "name")]

    best = cleaner.pick(cands, site)
    if best:
        return best
    return cleaner.from_title_tag(title_tag, site)
