"""URL 处理工具。"""
import re
from urllib.parse import urljoin

_URL_SPLIT = re.compile(r"[,，;；\s]+")
# 常见分页参数：query 里已经有这些字段时优先改值，而不是再拼一个 page。
# 分页参数可能出现在 query 开头（无前导 ?），所以把起始位置也算作分隔符
_PAGE_PARAM_RE = re.compile(r"(^|[?&])(page|paged|pageNum|page_no|p)=(\d+)", re.I)


def split_urls(s: str | None) -> list[str]:
    """把多个候选链接拆成去重后的列表。"""
    return list(dict.fromkeys(x for x in _URL_SPLIT.split(str(s or "")) if x))


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
    for ph in ("{}", "{p}", "{page}", "%d"):
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
    if nums:
        last = nums[-1]
        result = u[:last.start()] + str(p) + u[last.end():] + tail
        if query_sep:
            result += "?" + query
    elif query_sep and _PAGE_PARAM_RE.search(query):
        query = _PAGE_PARAM_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}={p}", query, count=1)
        result = f"{path}?{query}"
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
