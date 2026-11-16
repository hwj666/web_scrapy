"""列表页 URL 修复：换域名/改版后旧路径 404 时，尝试同站其它常见列表路径。

策略（按可信度排序）：
1. 发布页里给出的完整 URL；
2. 新站点首页里带分类 id 的链接；
3. 首页里出现过的列表路由前缀（`/vodtype/`、`/vodshow/` …）配原分类 id；
4. 常见 maccms 路径模板（YAML `list_url_patterns`）。

候选数量有上限，避免 404 时把站点扫一遍。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

PAGE_TOKEN = "{}"
_NUM_RE = re.compile(r"\d+")
_HREF_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
# 常见的分类列表路由前缀（出现过的前缀优先于盲猜模板）
_ROUTE_RE = re.compile(
    r"/(?:vodtype|vodshow|vodlist|vod|type|list|show|category|index\.php/vod/type)/",
    re.I)
# 详情/播放路由绝不能被当成列表路径
_DETAIL_RE = re.compile(r"/(?:play|vodplay|detail|voddetail|watch)/", re.I)
MAX_CANDIDATES = 12

# 换域名后常见的列表路径（{cat}=分类 id，{page}=页码；没有 {page} 的会用 ?page= 兜底）
DEFAULT_PATTERNS = (
    "/vodtype/{cat}-{page}.html",
    "/vodtype/{cat}.html",
    "/vodshow/{cat}--------{page}---.html",
    "/vodtype/{cat}/page/{page}.html",
    "/list/{cat}-{page}.html",
    "/type/{cat}-{page}.html",
    "/vod/{cat}/page/{page}.html",
    "/index.php/vod/type/id/{cat}.html",
)


def category_id(template: str) -> str:
    """从列表模板里取分类 id：页码占位符之前最后一个数字。"""
    head = (template or "").split(PAGE_TOKEN, 1)[0]
    nums = _NUM_RE.findall(head)
    return nums[-1] if nums else ""


def _origin(url: str) -> str:
    up = urlsplit(url or "")
    if up.scheme not in ("http", "https") or not up.netloc:
        return ""
    return f"{up.scheme}://{up.netloc}"


def _with_page(pattern: str) -> str:
    if "{page}" in pattern:
        return pattern.replace("{page}", PAGE_TOKEN)
    sep = "&" if "?" in pattern else "?"
    return f"{pattern}{sep}page={PAGE_TOKEN}"


def candidate_templates(tpl: str,
                        patterns: tuple[str, ...] = DEFAULT_PATTERNS) -> list[str]:
    """按分类 id 生成候选列表模板（绝对地址，按原顺序去重）。"""
    origin = _origin(tpl)
    cat = category_id(tpl)
    if not origin or not cat:
        return []
    out = [f"{origin}{_with_page(p.replace('{cat}', cat))}" for p in patterns]
    return list(dict.fromkeys(out))


def templates_from_url(url: str, cat: str) -> list[str]:
    """把具体 URL 变成可分页模板。

    `/vodtype/144-1.html` -> `/vodtype/144-{}.html`；
    只有分类号没有页码时（`/vodtype/144.html`）退回 `?page={}`，避免把分类号当页码替换。
    """
    origin = _origin(url)
    up = urlsplit(url or "")
    if not origin:
        return []
    path = up.path or "/"
    out: list[str] = []
    nums = list(_NUM_RE.finditer(path))
    if nums:
        last = nums[-1]
        # 只有「最后一个数字不是分类号」时才能安全地把它换成分页占位符
        if last.group(0) != cat or len(nums) > 1:
            out.append(f"{origin}{path[:last.start()]}{PAGE_TOKEN}{path[last.end():]}")
    if up.query:
        out.append(f"{origin}{path}?{up.query}&page={PAGE_TOKEN}")
    else:
        out.append(f"{origin}{path}?page={PAGE_TOKEN}")
    return list(dict.fromkeys(out))


def text_of_html(html: str) -> str:
    """粗剥 HTML 标签取可见文字（用于在跳转页/发布页里找新网址）。"""
    import html as _html

    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _html.unescape(re.sub(r"[ \t\r\f\v]+", " ", text))


def is_list_route(url: str) -> bool:
    return bool(_ROUTE_RE.search(urlsplit(url or "").path))


def links_in(html: str, base_url: str) -> list[str]:
    return _hrefs(html, _origin(base_url) or base_url)


def _hrefs(html: str, origin: str) -> list[str]:
    out: list[str] = []
    for href in _HREF_RE.findall(html or ""):
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        out.append(urljoin(origin + "/", href))
    return out


def templates_from_homepage(html: str, base_url: str, cat: str) -> list[str]:
    """从站点首页 HTML 推导列表模板：先找带分类 id 的链接，再用出现过的路由前缀。"""
    origin = _origin(base_url)
    if not origin:
        return []
    links = _hrefs(html, origin)
    out: list[str] = []

    # 只认「像分类列表」的链接：详情/播放路由一律排除
    list_links = [ln for ln in links
                  if urlsplit(ln).netloc == urlsplit(origin).netloc
                  and _ROUTE_RE.search(urlsplit(ln).path)
                  and not _DETAIL_RE.search(urlsplit(ln).path)]

    # 1) 直接带分类 id 的链接（最可靠：站点自己写出来的列表地址）
    if cat:
        for link in list_links:
            if cat in link:
                out.extend(templates_from_url(link, cat))

    # 2) 首页出现过的列表路由前缀，配上原分类 id（分类 id 没变、只是路由换样式的情况）
    prefixes = {m.group(0) for m in _ROUTE_RE.finditer(" ".join(list_links))}
    for prefix in sorted(prefixes):
        if not cat:
            break
        base = f"{origin}{prefix}"
        out.append(f"{base}{cat}.html?page={PAGE_TOKEN}")
        out.append(f"{base}{cat}-{PAGE_TOKEN}.html")
        out.append(f"{base}{cat}{'-' * 8}{PAGE_TOKEN}---.html")
        out.append(f"{base}{cat}/page/{PAGE_TOKEN}.html")
    return list(dict.fromkeys(out))


_URL_RE = re.compile(r"https?://[^\s\"'<>()\\]+", re.I)


def urls_in_html(html: str) -> list[str]:
    """页面里出现的所有绝对 URL（包括 <script> 里的），用于跟随 JS 跳转/发布页。"""
    out: list[str] = []
    for raw in _URL_RE.findall(html or ""):
        url = raw.rstrip(".,;:!?，。；：、）】")
        if url and url not in out:
            out.append(url)
    return out


_ASSET_RE = re.compile(
    r"\.(?:js|css|png|jpe?g|gif|ico|svg|woff2?|ttf|mp4|m3u8|map)$", re.I)
_ASSET_PATH_RE = re.compile(r"/(?:logo|assets?|static|gg-|ads?)/", re.I)


def _is_asset_url(url: str) -> bool:
    path = urlsplit(url).path
    return bool(_ASSET_RE.search(path) or _ASSET_PATH_RE.search(path))


def pick_chain_host(html: str, exclude_hosts: set[str]) -> str:
    """从页面（含脚本）里挑一个「其它域名」作为下一跳。

    地址发布页常常把真实域名写进 `window.SITE.domains`、`location.href = "..."`、
    或 `https://xxx/home.html` 这类脚本/链接里，只读可见文字会漏掉。
    """
    blocked = {h.lower() for h in exclude_hosts if h}
    urls = [u for u in urls_in_html(html) if not _is_asset_url(u)]
    # 优先「入口页」：根路径或 /home.html 之类
    entry = [u for u in urls
             if not urlsplit(u).path.strip("/") or
             urlsplit(u).path.lower().endswith(("/home.html", "/index.html"))]
    for group in (entry, urls):
        for url in group:
            host = urlsplit(url).netloc.lower().split(":")[0]
            if host and host not in blocked:
                return host
    return ""


def pick_new_site_link(links: list[str], exclude_hosts: set[str], cat: str) -> str:
    """从跳转页/发布页里挑一个指向「其它域名」的列表链接。"""
    blocked = {h.lower() for h in exclude_hosts if h}
    pool = []
    for link in links:
        host = urlsplit(link).netloc.lower().split(":")[0]
        if not host or host in blocked:
            continue
        if is_detail_path(link) or not is_list_route(link):
            continue
        pool.append(link)
    if not pool:
        return ""
    if cat:
        with_cat = [ln for ln in pool if cat in ln]
        pool = with_cat or pool
    return pool[0]


def pick_link_for_host(links: list[str], host: str, cat: str) -> str:
    """从发布页/弹窗的链接里挑一个最像「新站点列表页」的地址。

    优先：同 host + 带原分类 id + 列表路由；其次同 host + 列表路由。
    详情/播放链接一律排除。
    """
    target = (host or "").lower()
    pool = [ln for ln in links
            if urlsplit(ln).netloc.lower().split(":")[0] == target]
    pool = [ln for ln in pool if not is_detail_path(ln)]
    if not pool:
        return ""
    listish = [ln for ln in pool if _ROUTE_RE.search(urlsplit(ln).path)]
    pool = listish or pool
    if cat:
        with_cat = [ln for ln in pool if cat in ln]
        pool = with_cat or pool
    return pool[0]


def is_detail_path(url: str) -> bool:
    """详情/播放页不能当列表页用。"""
    return bool(_DETAIL_RE.search(urlsplit(url or "").path))


def scheme_variants(tpl: str) -> list[str]:
    """https 不通时再试 http（换域名后常见 http/https 不一致）。"""
    if not tpl:
        return []
    if tpl.startswith("https://"):
        return ["http://" + tpl[len("https://"):]]
    if tpl.startswith("http://"):
        return ["https://" + tpl[len("http://"):]]
    return []
