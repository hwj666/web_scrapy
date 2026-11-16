"""流水线关键逻辑回归：分片解密合并、重试进度、列表页并发有序性与异步日志。"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

try:
    from Crypto.Cipher import AES
except Exception:  # pragma: no cover - 依赖缺失时跳过
    AES = None

from app.core.models import JobConfig, ListPage, VideoItem
from app.services.job import Job
from app.toolkit.files import AsyncLogWriter
from app.toolkit.segments import concat_segments


def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


@unittest.skipIf(AES is None, "需要 pycryptodome")
class ConcatSegmentsTests(unittest.TestCase):
    def test_rotating_keys_and_per_segment_iv(self) -> None:
        key1, key2 = b"0123456789abcdef", b"fedcba9876543210"
        iv1, iv2 = bytes(range(16)), bytes(range(16, 32))
        pieces = [b"first-segment", b"second-segment"]
        keys = [key1, key2]
        ivs = [iv1, iv2]
        with tempfile.TemporaryDirectory() as tmp:
            parts = os.path.join(tmp, "parts")
            os.makedirs(parts)
            for i, (piece, key, iv) in enumerate(zip(pieces, keys, ivs, strict=True)):
                enc = AES.new(key, AES.MODE_CBC, iv).encrypt(_pad(piece))
                with open(os.path.join(parts, f"{i:05d}.enc"), "wb") as f:
                    f.write(enc)
            ts = os.path.join(tmp, "out.ts")
            concat_segments(ts, parts, len(pieces), keys, ivs)
            with open(ts, "rb") as f:
                self.assertEqual(f.read(), b"".join(pieces))

    def test_clear_segment_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parts = os.path.join(tmp, "parts")
            os.makedirs(parts)
            with open(os.path.join(parts, "00000.enc"), "wb") as f:
                f.write(b"clear-bytes")
            ts = os.path.join(tmp, "out.ts")
            concat_segments(ts, parts, 1, [None], [b"\x00" * 16])
            with open(ts, "rb") as f:
                self.assertEqual(f.read(), b"clear-bytes")


class ProgressTests(unittest.TestCase):
    def test_progress_is_state_based_and_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Job(JobConfig(list_url="https://example.com/v/1", outdir=tmp))
            try:
                job.total = 4
                job.st["videos"] = [{"s": 2}, {"s": 3}, {"s": 1}, {"s": 0}]
                job._progress()
                self.assertAlmostEqual(job.st["percent"], 10 + 88 * 2 / 4)

                # 模拟一键重试：视频状态复位、累计成功/失败计数器仍很大，
                # 进度必须按状态重算，不能被旧计数器顶到 100% 以上。
                job.ok = job.fail = 99
                job.st["videos"] = [{"s": 0}, {"s": 0}, {"s": 0}, {"s": 0}]
                job._progress()
                self.assertAlmostEqual(job.st["percent"], 10.0)

                job.st["videos"] = [{"s": 2}, {"s": 2}, {"s": 3}, {"s": 4}]
                job._progress()
                self.assertLessEqual(job.st["percent"], 100.0)
            finally:
                job.dispose()


class _FakeListParser:
    """按页码返回视频；页 2/3/4 的延迟故意打乱，用来验证乱序完成也按页号入队。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def parse(self, html: str, page: int, base: str) -> ListPage:
        self.calls.append(page)
        await asyncio.sleep({1: 0.03, 2: 0.06, 3: 0.0, 4: 0.01}.get(page, 0.0))
        if page in (5, 6):
            return ListPage(page=page, items=[])
        return ListPage(page=page, items=[
            VideoItem(id=f"https://example.com/v/{page}-{i}",
                      title=f"p{page}-{i}", page=page)
            for i in range(2)
        ])


class ListConcurrencyTests(unittest.TestCase):
    def test_pages_are_dispatched_in_order_and_stop_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = JobConfig(list_url="https://example.com/list/1-{}.html",
                            p1=1, p2=10, outdir=tmp)
            job = Job(cfg)
            try:
                fake = _FakeListParser()
                job._ensure_parsers()
                job.list_runner.static_list = fake       # type: ignore[union-attr]
                job.list_runner.browser_list = None      # type: ignore[union-attr]
                job.rules.pipeline.list_concurrency = 3
                job.rules.browser.use_api_capture = False
                seen: list[tuple[int, int]] = []

                async def fake_fetch(url: str, **kw):
                    return "<html></html>"

                async def fake_resolve_base(cand: str, p1: int = 1) -> str:
                    return cand

                async def fake_work(n: int, item: VideoItem) -> None:
                    seen.append((n, item.page))
                    job._vset(n - 1, s=2)
                    job._progress()

                job.list_runner._fetch = fake_fetch         # type: ignore[union-attr]
                job.domain.resolve_base = fake_resolve_base  # type: ignore[assignment]
                job._work = fake_work                        # type: ignore[assignment]
                job._ensure_parsers = lambda: None            # type: ignore[assignment]

                # 走完整 _async_run：初始化事件循环、列表解析、等任务、_finish 收尾
                asyncio.run(job._async_run())

                # 5、6 连续两页为空 -> 提前结束；7~10 不应进入队列
                self.assertEqual([p for _, p in seen], [1, 1, 2, 2, 3, 3, 4, 4])
                self.assertEqual([n for n, _ in seen], list(range(1, 9)))
                self.assertEqual([it.page for it in job.items], [1, 1, 2, 2, 3, 3, 4, 4])
                self.assertEqual(job.total, 8)
                self.assertEqual(job.st["percent"], 100)
                self.assertFalse(job.st["running"])
                self.assertLessEqual(max(fake.calls), 8)
                with open(os.path.join(tmp, "_web_log.txt"), encoding="utf-8") as f:
                    self.assertIn("任务完成", f.read())
            finally:
                job.dispose()


class LogWriterTests(unittest.TestCase):
    def test_batches_are_flushed_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.txt")
            writer = AsyncLogWriter(path)
            try:
                for i in range(300):
                    writer.write(f"line{i}")
                writer.flush()
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(len(lines), 300)
                self.assertEqual(lines[0], "line0")
                self.assertEqual(lines[-1], "line299")
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
