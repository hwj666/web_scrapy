"""统一 HTTP 客户端（基于 httpx 的异步客户端 + 重试退避）。

整条下载链路都是网络 IO，共用一个 `AsyncClient` 复用连接池，靠协程并发，
不再为每个线程建 Session / 线程池。
"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.models import AppConfig
from app.toolkit.codec import decode_body

# 这些 4xx 是"可能过一会儿就好了"的，值得重试；其余 4xx（404/403…）重试纯属浪费。
_RETRY_STATUS = frozenset({408, 425, 429})


def _tls_verify_default() -> bool:
    """默认校验证书；需兼容自签/证书异常站点时设 YS_TLS_VERIFY=0 关闭。"""
    raw = (os.environ.get("YS_TLS_VERIFY") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


class FetchError(RuntimeError):
    """网络取数失败：保留原始异常用于区分「连不上」和「站点返回 4xx/5xx」。"""

    def __init__(self, url: str, cause: BaseException | None):
        self.url = url
        self.cause = cause
        super().__init__(f"fetch failed {url}: {cause}")


def is_conn_error(e: BaseException) -> bool:
    """是否属于「连不上」类错误（域名失效/被阻断）。4xx/5xx 不算。

    httpx 里连接失败/读写超时/协议被重置都归在 TransportError 下。
    """
    cause = e.cause if isinstance(e, FetchError) else e
    return isinstance(cause, httpx.TransportError)


def _is_retryable(e: httpx.HTTPError) -> bool:
    """HTTP 层是否值得重试：5xx / 少数 4xx / 连接类错误重试，其余 4xx 直接放弃。"""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500 or e.response.status_code in _RETRY_STATUS
    return isinstance(e, (httpx.TransportError, httpx.TimeoutException))


async def _sleep(seconds: float, stop=None) -> None:
    """可被 stop 回调打断的退避（stop 抛异常即中止，通常是 StopJob）。"""
    left = seconds
    while left > 0:
        if stop is not None:
            stop()
        step = min(0.2, left)
        await asyncio.sleep(step)
        left -= step


@dataclass
class FetchedResponse:
    """已读完的原始响应（status/url/headers/content 一次拷出来，调用方随便用）。"""
    url: str
    status: int
    headers: dict[str, str]
    content: bytes


class AsyncHttpClient:
    """整个任务共用一个 AsyncClient 的异步 HTTP 客户端（连接池自动复用）。"""

    def __init__(self, app_config: AppConfig | None = None, verify: bool | None = None):
        self.app_config = app_config or AppConfig()
        self.verify = _tls_verify_default() if verify is None else verify
        self._client: httpx.AsyncClient | None = None
        self._referer = ""

    def set_referer(self, referer: str) -> None:
        self._referer = referer

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": self.app_config.ua}
        if self._referer:
            h["Referer"] = self._referer
        return h

    def _get(self) -> httpx.AsyncClient:
        c = self._client
        if c is None or c.is_closed:
            c = httpx.AsyncClient(
                verify=self.verify, follow_redirects=True,
                timeout=httpx.Timeout(30.0, connect=15.0),
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
            self._client = c
        return c

    @staticmethod
    def _timeout(timeout: float) -> httpx.Timeout:
        return httpx.Timeout(timeout, connect=min(15.0, timeout))

    async def aclose(self) -> None:
        c, self._client = self._client, None
        if c is not None and not c.is_closed:
            try:
                await c.aclose()
            except Exception:
                pass

    async def get(self, url: str, *, binary: bool = False, retries: int = 3,
                  timeout: float = 30, stop=None, **kw) -> Any:
        """带重试退避的 GET；stop 为可选回调，用于在重试间隙/退避中响应停止信号。"""
        last: BaseException | None = None
        attempts = max(1, retries)
        for i in range(attempts):
            if stop is not None:
                stop()
            try:
                r = await self._get().get(url, timeout=self._timeout(timeout),
                                          headers=self._headers(), **kw)
                r.raise_for_status()
                return r.content if binary else decode_body(
                    r.content, r.headers.get("Content-Type", ""))
            except httpx.HTTPError as e:
                last = e
                if not _is_retryable(e) or i >= attempts - 1:
                    break
                await _sleep(min(2 * (i + 1), 6) + random.random(), stop)
        raise FetchError(url, last)

    async def get_raw(self, url: str, *, retries: int = 1, timeout: float = 20,
                      stop=None) -> FetchedResponse:
        """原始响应（不因 4xx/5xx 抛错），用于域名探测时要看最终 URL 和状态码。"""
        last: BaseException | None = None
        attempts = max(1, retries)
        for i in range(attempts):
            if stop is not None:
                stop()
            try:
                r = await self._get().get(url, timeout=self._timeout(timeout),
                                          headers=self._headers())
                return FetchedResponse(url=str(r.url), status=r.status_code,
                                       headers=dict(r.headers), content=r.content)
            except httpx.HTTPError as e:
                last = e
                if not _is_retryable(e) or i >= attempts - 1:
                    break
                await _sleep(min(2 * (i + 1), 6) + random.random(), stop)
        raise FetchError(url, last)
