"""从发布页/弹窗里识别新域名，以及把旧域名 URL 重写到新域名。"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# 只接受看起来像站点的 host（拒绝 localhost、裸 IP、javascript 等）
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$", re.I)


def clean_host(value: str | None) -> str:
    """把 'https://New.com/path' / 'new.com:8080' / 'new.com' 统一成 'new.com'。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    if not host or not _HOST_RE.match(host) or host in ("localhost",):
        return ""
    return host


def host_from_url(url: str | None) -> str:
    return clean_host(url)


def find_new_host(text: str | None, pattern: re.Pattern[str] | None,
                  exclude: Iterable[str] = ()) -> str | None:
    """从页面文字中找新域名；pattern 的每个分组都可能是 host。"""
    if pattern is None:
        return None
    blocked = {str(x).lower() for x in exclude if x}
    for match in pattern.finditer(text or ""):
        for idx in range(1, (match.lastindex or 0) + 1):
            host = clean_host(match.group(idx))
            if host and host not in blocked:
                return host
    return None


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>）)】\u3000]+", re.I)


def find_new_url(text: str | None, pattern: re.Pattern[str] | None,
                 exclude: Iterable[str] = ()) -> tuple[str, str | None]:
    """识别新域名，并尽量把发布页里给出的完整 URL 一起带回来。

    只换 host 会丢掉路径：站点换域名时常连列表路径一起改版，
    所以能拿到完整地址时优先用它（返回的 url 可能是列表页地址）。
    """
    host = find_new_host(text, pattern, exclude)
    if not host:
        return "", None
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:，。；：")
        if clean_host(url) == host:
            return host, url
    return host, None


def replace_host(url: str, old_hosts: Iterable[str], new_host: str) -> str:
    """保留路径/查询串，只替换 host。"""
    up = urlsplit(url)
    old = {str(x).lower() for x in old_hosts if x}
    if up.netloc.lower() not in old:
        return url
    return urlunsplit(up._replace(netloc=new_host))


async def page_notice_target(page, pattern: re.Pattern[str] | None,
                             shell_hints: Iterable[str],
                             exclude: Iterable[str]) -> tuple[str, str | None]:
    """检查一个 Playwright 页面：返回 (新 host, 发布页里给出的完整 URL 或 None)。"""
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    text = f"{title}\n{body}"[:50000]
    host, url = find_new_url(text, pattern, exclude)
    if host:
        return host, url
    # 没有明确“新域名: xxx”字样时，只有带发布页提示的页面才用当前 URL 的 host
    if any(k in text for k in shell_hints):
        host = host_from_url(page.url)
        if host and host not in {str(x).lower() for x in exclude if x}:
            return host, page.url
    return "", None


def page_opener_matches(page, opener) -> bool:
    """判断新页面是不是当前页打开的（Playwright 的 opener 同步返回）。"""
    try:
        return page.opener() is opener
    except Exception:
        return False


async def close_pages(pages: Iterable[Any]) -> None:
    for page in pages:
        try:
            await page.close()
        except Exception:
            pass
