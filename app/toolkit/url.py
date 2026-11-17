"""URL 处理工具。"""

import re
from urllib.parse import urljoin

_URL_SPLIT = re.compile(r"[,，;；\s]+")
# 常见分页参数：query 里已经有这些字段时优先改值，而不是再拼一个 page。
# 分页参数可能出现在 query 开头（无前导 ?），所以把起始位置也算作分隔符
_PAGE_PARAM_RE = re.compile(r"(^|[?&])(page|paged|pageNum|page_no|p)=(\d+)", re.I)
# 显式页码占位符：界面要求用户用这些标记页码位置（新站点适配只改 YAML/链接）。
_PAGE_PLACEHOLDERS = ("{}", "{p}", "{page}", "%d")


def split_urls(s: str | None) -> list[str]:
    """把多个候选链接拆成去重后的列表。"""
    return list(dict.fromkeys(x for x in _URL_SPLIT.split(str(s or "")) if x))


def has_page_placeholder(url: str) -> bool:
    """链接里是否用 {} / {p} / {page} / %d 显式标了页码位置。"""
    raw = url or ""
    return any(ph in raw for ph in _PAGE_PLACEHOLDERS)


def has_page_param(url: str) -> bool:
    """query 里是否已经带 page= 这类分页参数（可以直接改值翻页）。"""
    _head, _sep, query = (url or "").partition("?")
    return bool(_PAGE_PARAM_RE.search(query))


def page_number_mode(url: str) -> str:
    """返回翻页方式：placeholder / query / path / none。

    只有 placeholder 和 query 能无歧义地翻页；`path` 表示“路径最后一个数字当页码”
    的旧启发式（对 `/vodtype/144.html` 这种分类 ID 链接并不可靠）。
    """
    if has_page_placeholder(url):
        return "placeholder"
    if has_page_param(url):
        return "query"
    _head, sep, _query = (url or "").partition("?")
    path, _sep2, _query2 = _head.partition("#")[0].partition("?")
    u = path.rstrip("/")
    slash = u.find("/", u.find("://") + 3)
    if slash == -1:
        slash = len(u)
    return "path" if any(m.start() > slash for m in re.finditer(r"\d+", u)) else "none"


def page_url(tpl: str, p: int, p1: int, p2: int) -> str:
    """按任务页码范围取第 p 页的链接。

    单页任务（p1 == p2）且链接没有显式翻页标记时，原样使用用户粘贴的地址：
    对 `/vodtype/144.html` 这种「路径数字是分类 ID」的常见链接，第 1 页必须仍是
    144，而不是被当成页码改写成 `/vodtype/1.html`。
    """
    raw = (tpl or "").strip()
    if not raw:
        return raw
    if p1 == p2 and page_number_mode(raw) == "path":
        return raw
    return auto_list_url(raw, p)


def page_mode_warning(url: str, p1: int, p2: int) -> str:
    """多页任务但链接没有可靠页码标记时，返回给用户看的提示（空串表示无需提示）。"""
    if p2 <= p1:
        return ""
    mode = page_number_mode(url)
    if mode in ("placeholder", "query"):
        return ""
    return (
        "链接里没有页码占位符 {} 也没有 page= 参数，将按「路径最后一个数字是页码」"
        "推断翻页；如果这个数字其实是分类/栏目 ID（如 /vodtype/144.html），"
        "会翻到别的分类。建议改成 /vodtype/144.html?page={} 或 /vodtype/144-{}.html"
    )


def auto_list_url(base: str, p: int) -> str:
    """把列表页链接模板里的页码占位符替换成页码 p。

    优先级：
    1. 显式占位符 ``{}`` / ``{p}`` / ``{page}`` / ``%d``；
    2. 路径里的最后一个数字（``/list/3.html`` → ``/list/9.html``）；
    3. query 里已有的分页参数（``?page=1`` → ``?page=9``）；
    4. 都没有时追加 ``page=``，已有 query 用 ``&`` 连接，并保留 ``#fragment``。

    query 里的普通数字（如 ``?id=123``）不再被误当成页码。
    """
    raw = (base or "").strip()
    if not raw:
        return raw
    for ph in _PAGE_PLACEHOLDERS:
        if ph in raw:
            # 站点的第 1 页通常就是不带分页参数的地址：
            # `/vodtype/144.html?page={}` 在 p=1 时应该是 `/vodtype/144.html`
            if p == 1 and raw.endswith("?page=" + ph):
                return raw[: -len("?page=" + ph)]
            return raw.replace(ph, str(p))

    head, hash_sep, fragment = raw.partition("#")
    path, query_sep, query = head.partition("?")

    tail = "/" if path.endswith("/") else ""
    u = path.rstrip("/")
    slash = u.find("/", u.find("://") + 3)
    if slash == -1:
        slash = len(u)
    nums = [m for m in re.finditer(r"\d+", u) if m.start() > slash]
    if query_sep and _PAGE_PARAM_RE.search(query):
        # query 里的分页参数是明确的页码：优先改它，别去动路径里的数字。
        # 否则 /vodtype/144.html?page=2 会把分类 ID 144 当成页码改成 1/2/3。
        query = _PAGE_PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}={p}", query, count=1)
        result = f"{path}?{query}"
    elif nums:
        last = nums[-1]
        result = u[: last.start()] + str(p) + u[last.end() :] + tail
        if query_sep:
            result += "?" + query
    elif query_sep:
        result = f"{u}?{query}&page={p}"
    else:
        result = f"{u}?page={p}"
    if hash_sep:
        result += "#" + fragment
    return result


def abs_url(u: str, base: str) -> str:
    """相对地址 -> 绝对地址。"""
    if u.startswith("http"):
        return u
    return urljoin(base, u)
