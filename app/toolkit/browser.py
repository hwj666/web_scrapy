"""共享的浏览器跳转/等待 helper：可被停止信号打断的 page.goto 与定时等待。

`stop` 是同步回调，检测到停止信号时抛出 StopJob/JobAborted；在 45 秒的 page.goto
或长等待期间每 250ms 轮询一次，避免用户点完「停止」还要干等到超时。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def goto_with_stop(page, url: str, *, stop: Callable[[], None], timeout_ms: int = 45000) -> None:
    """page.goto 期间也响应停止：分片轮询等待，而不是一直等到超时。"""
    task = asyncio.ensure_future(page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded"))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if done:
                task.result()
                return
            stop()
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


async def wait_with_stop(page, ms: int, *, stop: Callable[[], None]) -> None:
    """可被停止信号打断的定时等待（替代 page.wait_for_timeout 的整段阻塞）。"""
    left = max(0, ms)
    while left > 0:
        stop()
        step = min(200, left)
        await page.wait_for_timeout(step)
        left -= step
