"""域名兜底：主域名被 DNS 污染/阻断时，沿 CNAME 链试同站其他主机名。

注意这不是「重定向」：CNAME 只是同一台服务器 / 同一套 CDN 的另一个名字，所以它
只能救「域名被针对、服务器还在」的情况；站点搬到全新域名时 CNAME 链里不会有新域名，
必须靠 HTTP 30x / 发布页 / JS 跳转去发现。
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx

_CNAME_TTL = 600
_ALIAS_TTL = 3600
_DOH_SERVERS = (
    "https://223.5.5.5/resolve",
    "https://1.1.1.1/dns-query",
    "https://dns.alidns.com/resolve",
)

# host -> [(cname...), 查询时间]
_CNAME_CACHE: dict[str, tuple[list[str], float]] = {}
# host -> [(主机名, 验证时间)]（按最近成功排序，带过期）
_ALIAS_CACHE: dict[str, list[tuple[str, float]]] = {}


def reset_caches() -> None:
    _CNAME_CACHE.clear()
    _ALIAS_CACHE.clear()


async def _doh(host: str, rtype: str) -> list[dict]:
    async with httpx.AsyncClient(verify=False, timeout=8) as c:
        for url in _DOH_SERVERS:
            for attempt in (0, 1):
                try:
                    r = await c.get(url, params={"name": host, "type": rtype},
                                    headers={"Accept": "application/dns-json"})
                    ans = (r.json() or {}).get("Answer") or []
                    if ans:
                        return ans
                    break
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(0.8)
    return []


async def _dns_cnames(host: str) -> list[str]:
    now = time.time()
    hit = _CNAME_CACHE.get(host)
    if hit and now - hit[1] < _CNAME_TTL:
        return hit[0]
    out: list[str] = []
    for rtype in ("A", "CNAME"):
        for a in await _doh(host, rtype):
            if a.get("type") == 5:  # CNAME 记录
                n = (a.get("data") or "").rstrip(".")
                if n and n != host and n not in out:
                    out.append(n)
        if out:
            break
    if out:
        _CNAME_CACHE[host] = (out, now)
    return out


def _cached_aliases(host: str, now: float) -> list[tuple[str, float]]:
    return [(a, t) for a, t in (_ALIAS_CACHE.get(host) or []) if now - t < _ALIAS_TTL]


async def cdn_alias(url: str, ua: str) -> str | None:
    """域名连不上时沿 CNAME 链试同站主机名，返回第一个能响应的完整 URL。"""
    up = urlparse(url)
    cur = f"{up.scheme}://{up.netloc}"
    now = time.time()
    cached = _cached_aliases(up.netloc, now)
    seen = [a for a, _ in cached] + await _dns_cnames(up.netloc)
    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10,
                                 headers={"User-Agent": ua}) as c:
        for alt in dict.fromkeys(seen):
            if alt == up.netloc:
                continue
            origin = f"{up.scheme}://{alt}"
            try:
                r = await c.get(origin + "/")
                if r.status_code < 400:
                    _ALIAS_CACHE[up.netloc] = [(alt, now)] + [
                        (a, t) for a, t in cached if a != alt]
                    return url.replace(cur, origin, 1)
            except Exception:
                continue
    _ALIAS_CACHE.pop(up.netloc, None)
    return None
