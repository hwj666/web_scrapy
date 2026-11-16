"""URL 处理工具。"""
import re
from urllib.parse import urljoin

_URL_SPLIT = re.compile(r"[,，;；\s]+")


def split_urls(s: str | None) -> list[str]:
    """把多个候选链接拆成去重后的列表。"""
    return list(dict.fromkeys(x for x in _URL_SPLIT.split(str(s or "")) if x))


def auto_list_url(base: str, p: int) -> str:
    """把列表页链接模板里的页码占位符替换成页码 p。"""
    raw = base.strip()
    for ph in ("{}", "{p}", "{page}", "%d"):
        if ph in raw:
            return raw.replace(ph, str(p))
    tail = "/" if raw.endswith("/") else ""
    u = raw.rstrip("/")
    slash = u.find("/", u.find("://") + 3)
    if slash == -1:
        slash = len(u)
    nums = [m for m in re.finditer(r"\d+", u) if m.start() > slash]
    if nums:
        last = nums[-1]
        return u[:last.start()] + str(p) + u[last.end():] + tail
    return f"{u}?page={p}"


def abs_url(u: str, base: str) -> str:
    """相对地址 -> 绝对地址。"""
    if u.startswith("http"):
        return u
    return urljoin(base, u)
