"""拆分后各模块的边界测试：JobState / DomainProbe / VideoWorker。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from app.core.exceptions import DomainChanged, NewDomainError
from app.core.models import JobConfig, ListPage, VideoItem
from app.core.parsing import ParserContext
from app.core.rules import RuleManager
from app.parsers.list.static import StaticListParser
from app.parsers.play.static import StaticResolver
from app.services.dedup_service import DedupService
from app.services.domain import DomainProbe
from app.services.hls import HlsDownloader
from app.services.job_state import JobState
from app.services.list_runner import ListRunner
from app.services.list_url import (
    candidate_templates,
    category_id,
    templates_from_homepage,
    templates_from_url,
)
from app.services.video_worker import VideoWorker
from app.toolkit.domain_notice import (
    clean_host,
    find_new_host,
    page_notice_target,
    replace_host,
)
from app.toolkit.http import FetchedResponse, _tls_verify_default, should_retry_with_browser
from app.toolkit.probe import probe_media


def _cfg(tmp: str, **kw) -> JobConfig:
    return JobConfig(list_url="https://example.com/list/1-{}.html", outdir=tmp, **kw)


class JobStateTests(unittest.TestCase):
    def test_add_dedup_progress_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            items = [
                VideoItem(id="u1", title="a"),
                VideoItem(id="u2", title="b"),
                VideoItem(id="u1", title="a-dup"),
            ]
            added = state.add_items(items, 1, "https://example.com")
            self.assertEqual([n for n, _ in added], [1, 2])
            self.assertEqual(state.total, 2)
            self.assertEqual([v["i"] for v in state.st["videos"]], [1, 2])
            self.assertEqual(state.st["videos"][0]["u"], "https://example.com/u1")

            state.record_failure("x", 5)
            state.vset(0, s=3)
            state.vset(1, s=2)
            state.progress()
            self.assertAlmostEqual(state.st["percent"], 10 + 88 * 2 / 2)

            state.finish()
            self.assertFalse(state.st["running"])
            pairs, err = state.prepare_retry()
            self.assertEqual(err, "")
            self.assertEqual([n for n, _ in pairs], [1])
            self.assertEqual(state.fail, 0)
            self.assertTrue(state.st["running"])

    def test_record_failure_trips_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            self.assertEqual(state.record_failure("boom", 2), "")
            trip = state.record_failure("boom", 2)
            self.assertIn("连续 2 个视频下载失败", trip)
            self.assertEqual(state.fail, 2)
            self.assertEqual(state.abort_msg, trip)

    def test_log_seq_survives_trimming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            line = state.log_line("hello")
            self.assertEqual(state.log_seq, 1)
            snap = state.snapshot()
            self.assertEqual(snap.log_seq, 1)
            self.assertEqual(snap.log, [line])
            for i in range(500):
                state.log_line(f"line{i}")
            self.assertEqual(state.log_seq, 501)
            self.assertEqual(len(state.snapshot().log), 400)


class StaticListTitleTests(unittest.TestCase):
    def test_list_title_is_cleaned_and_same_domain_link_kept(self) -> None:
        parser = StaticListParser()
        page = asyncio.run(parser.parse('<a href="/play/1.html">测试视频一-HD</a>', 1, "https://example.com"))
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].title, "测试视频一")


class MainConfigTests(unittest.TestCase):
    def test_localhost_is_a_valid_list_url(self) -> None:
        from main import _build_job_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _build_job_config(
                {
                    "list_url": "http://localhost:8000/list",
                    "p1": 1,
                    "p2": 1,
                    "outdir": tmp,
                },
                default_outdir=tmp,
                safe_dir=lambda raw: raw,
            )
            self.assertEqual(cfg.base, "http://localhost:8000")


class _FakeDomain:
    async def resolve_base(self, cand: str, p1: int = 1) -> str:
        return cand

    def set_base(self, list_url: str) -> None:
        pass

    async def reprobe(self, why: str = "", force: bool = False) -> bool:
        return False

    async def ensure_list_url(self, tpl: str, p1: int = 1, strict: bool = False) -> str:
        return tpl


class _EmptyStaticList:
    async def parse(self, html: str, page: int, base: str) -> ListPage:
        return ListPage(page=page, items=[])


class _ApiOnlyBrowserList:
    async def parse_with_page(self, page, url: str, page_no: int) -> ListPage:
        return ListPage(page=page_no, items=[])

    async def collect_via_api_capture(self, page, list_url: str, p1: int, p2: int) -> list[VideoItem]:
        return [VideoItem(id="https://example.com/v/1", title="API视频", page=p1)]


@asynccontextmanager
async def _fake_new_page():
    yield object(), []


class ListRunnerFallbackTests(unittest.TestCase):
    def test_api_capture_is_only_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, p2=1)
            state = JobState(cfg)
            rules = RuleManager()
            self.assertTrue(rules.browser.use_api_capture)
            self.assertTrue(rules.browser.detail_url_template)
            dispatched: list[tuple[int, VideoItem]] = []

            async def fake_fetch(url: str, **kw):
                return "<html></html>"

            runner = ListRunner(
                cfg,
                rules,
                state,
                fetch=fake_fetch,
                static_list=_EmptyStaticList(),
                browser_list=_ApiOnlyBrowserList(),
                new_page=_fake_new_page,
                domain=_FakeDomain(),  # type: ignore[arg-type]
                log=lambda msg: None,
                is_stopped=lambda: False,
                dispatch=lambda n, item: dispatched.append((n, item)),
            )
            asyncio.run(runner.run())

            self.assertEqual([item.title for item in state.items], ["API视频"])
            self.assertEqual([n for n, _ in dispatched], [1])


class BrowserTitleFallbackTests(unittest.TestCase):
    def test_untitled_static_items_use_browser_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, p2=1)
            state = JobState(cfg)
            rules = RuleManager()
            rules.browser.use_api_capture = False
            html = "<html><body><a href='/play/1.html'></a><a href='/play/2.html'></a></body></html>"

            async def fetch(url: str, **kw):
                return html

            class _Page:
                async def goto(self, *a, **k):
                    return None

                async def wait_for_timeout(self, *a, **k):
                    return None

                async def content(self):
                    return html

            @asynccontextmanager
            async def new_page():
                yield _Page(), []

            class _BrowserList:
                async def parse_with_page(self, page, url, p):
                    return ListPage(
                        page=p,
                        items=[
                            VideoItem(id="https://e.com/play/1.html", title="标题一", page=p),
                            VideoItem(id="https://e.com/play/2.html", title="标题二", page=p),
                        ],
                    )

            runner = ListRunner(
                cfg,
                rules,
                state,
                fetch=fetch,
                static_list=StaticListParser(),
                browser_list=_BrowserList(),
                new_page=new_page,
                domain=_FakeDomain(),  # type: ignore[arg-type]
                log=lambda msg: None,
                is_stopped=lambda: False,
                dispatch=lambda n, item: None,
            )
            asyncio.run(runner.run())

            self.assertEqual([it.title for it in state.items], ["标题一", "标题二"])


class BrowserRetryPolicyTests(unittest.TestCase):
    def test_404_does_not_trigger_browser(self) -> None:
        req = httpx.Request("GET", "https://x.com/list")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        self.assertFalse(should_retry_with_browser(err))

    def test_403_triggers_browser(self) -> None:
        req = httpx.Request("GET", "https://x.com/list")
        err = httpx.HTTPStatusError("403", request=req, response=httpx.Response(403, request=req))
        self.assertTrue(should_retry_with_browser(err))

    def test_strict_repair_raises_instead_of_continuing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)

            async def get_raw(url: str):
                return FetchedResponse(url=url, status=404, headers={}, content=b"nf")

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(probe.ensure_list_url("https://new.com/vodtype/144-{}.html", 1, strict=True))
            self.assertIn("无法自动修复", str(ctx.exception))


class BrowserListFetchFallbackTests(unittest.TestCase):
    def test_http_failure_falls_back_to_browser_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, p2=1)
            state = JobState(cfg)
            rules = RuleManager()
            rules.browser.use_api_capture = False
            dispatched: list[int] = []
            html = "<html><body><a href='/play/1.html'>测试视频一</a></body></html>"

            async def failing_fetch(url: str, **kw):
                # 模拟被 WAF 挡住的 403：这类错误才值得换浏览器
                req = httpx.Request("GET", url)
                resp = httpx.Response(403, request=req)
                raise httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)

            class _Page:
                async def goto(self, *a, **k):
                    return None

                async def wait_for_timeout(self, *a, **k):
                    return None

                async def content(self):
                    return html

            @asynccontextmanager
            async def new_page():
                yield _Page(), []

            class _BrowserList:
                async def parse_with_page(self, page, url, p):
                    return ListPage(page=p, items=[])

            runner = ListRunner(
                cfg,
                rules,
                state,
                fetch=failing_fetch,
                static_list=StaticListParser(),
                browser_list=_BrowserList(),
                new_page=new_page,
                domain=_FakeDomain(),  # type: ignore[arg-type]
                log=lambda msg: None,
                is_stopped=lambda: False,
                dispatch=lambda n, item: dispatched.append(n),
            )
            asyncio.run(runner.run())

            self.assertEqual([it.title for it in state.items], ["测试视频一"])
            self.assertEqual(dispatched, [1])


class HlsHelperTests(unittest.TestCase):
    def test_ts_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.ts")
            with open(good, "wb") as f:
                f.write(b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 187)
            bad = os.path.join(tmp, "bad.ts")
            with open(bad, "wb") as f:
                f.write(b"\x00" * 400)
            self.assertTrue(HlsDownloader._looks_like_ts(good))
            self.assertFalse(HlsDownloader._looks_like_ts(bad))
            self.assertFalse(HlsDownloader._looks_like_ts(os.path.join(tmp, "none")))

    def test_byterange_playlist_is_rejected_before_any_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            playlist = "#EXTM3U\n#EXTINF:4,\n#EXT-X-BYTERANGE:100@0\na.ts\n"
            requested: list[str] = []

            async def fetch(url: str, **kw):
                return playlist

            async def fetch_media(url: str, query: str, **kw):
                requested.append(url)
                return b""

            hls = HlsDownloader(
                tmp,
                2,
                state=state,
                fetch=fetch,
                fetch_media=fetch_media,
                log=lambda msg: None,
                is_stopped=lambda: False,
                abort_msg=lambda: "",
            )
            with patch("app.services.hls.ffmpeg_exe", lambda: "ffmpeg"):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(
                        hls.download("https://example.com/index.m3u8", "v", os.path.join(tmp, "v.mp4"))
                    )
            self.assertIn("EXT-X-BYTERANGE", str(ctx.exception))
            self.assertEqual(requested, [])
            self.assertFalse(os.path.exists(os.path.join(tmp, "v.mp4")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "v.mp4.tmp")))

    def test_consecutive_segment_failures_trigger_domain_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXTINF:1,\nc.ts\n#EXT-X-ENDLIST\n"
            calls: list[str] = []
            epoch = [0]

            async def fetch(url: str, **kw):
                return playlist

            async def fetch_media(url: str, query: str, **kw):
                raise RuntimeError("connection reset")

            async def recover(reason: str) -> bool:
                calls.append(reason)
                epoch[0] = 1
                return True

            hls = HlsDownloader(
                tmp,
                2,
                state=state,
                fetch=fetch,
                fetch_media=fetch_media,
                log=lambda msg: None,
                is_stopped=lambda: False,
                abort_msg=lambda: "",
                domain_epoch=lambda: epoch[0],
                recover=recover,
            )
            with self.assertRaises(DomainChanged):
                asyncio.run(hls.download("https://example.com/index.m3u8", "v", os.path.join(tmp, "v.mp4")))
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].startswith("分片"))

    def test_epoch_change_aborts_active_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXTINF:1,\nc.ts\n#EXT-X-ENDLIST\n"
            epoch = [0]

            async def fetch(url: str, **kw):
                return playlist

            async def fetch_media(url: str, query: str, **kw):
                epoch[0] = 1
                return b"\x47" + b"\x00" * 187

            hls = HlsDownloader(
                tmp,
                1,
                state=state,
                fetch=fetch,
                fetch_media=fetch_media,
                log=lambda msg: None,
                is_stopped=lambda: False,
                abort_msg=lambda: "",
                domain_epoch=lambda: epoch[0],
            )
            with self.assertRaises(DomainChanged):
                asyncio.run(hls.download("https://example.com/index.m3u8", "v2", os.path.join(tmp, "v2.mp4")))

    def test_fingerprint_and_parts_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)

            async def noop(*a, **k):
                return b""

            hls = HlsDownloader(
                tmp,
                2,
                state=state,
                fetch=noop,
                fetch_media=noop,
                log=lambda msg: None,
                is_stopped=lambda: False,
                abort_msg=lambda: "",
            )
            fp1 = hls._plan_fingerprint(["https://e/a.ts?sign=1"], [b"k" * 16], [b"\x01" * 16])
            fp2 = hls._plan_fingerprint(["https://e/a.ts?sign=2"], [b"k" * 16], [b"\x01" * 16])
            self.assertEqual(fp1, fp2)  # 查询串变化不应让断点续传失效

            parts = os.path.join(tmp, "parts")
            os.makedirs(parts)
            with open(os.path.join(parts, "00000.enc"), "wb") as f:
                f.write(b"stale")
            hls._prepare_parts(parts, "video", fp1)
            self.assertFalse(os.path.exists(os.path.join(parts, "00000.enc")))
            with open(os.path.join(parts, "00000.enc"), "wb") as f:
                f.write(b"keep")
            hls._prepare_parts(parts, "video", fp1)
            self.assertTrue(os.path.exists(os.path.join(parts, "00000.enc")))


class TlsDefaultTests(unittest.TestCase):
    def test_verify_is_on_by_default(self) -> None:
        with patch.dict(os.environ, {"YS_TLS_VERIFY": ""}):
            self.assertTrue(_tls_verify_default())
        with patch.dict(os.environ, {"YS_TLS_VERIFY": "0"}):
            self.assertFalse(_tls_verify_default())
        with patch.dict(os.environ, {"YS_TLS_VERIFY": "1"}):
            self.assertTrue(_tls_verify_default())


class ListUrlRepairTests(unittest.TestCase):
    def test_category_id_and_templates_from_concrete_url(self) -> None:
        self.assertEqual(category_id("https://a.com/vodtype/144-{}.html"), "144")
        self.assertEqual(category_id("https://a.com/vodtype/{}.html"), "")
        cands = templates_from_url("https://new.com/vodtype/144-1.html", "144")
        self.assertIn("https://new.com/vodtype/144-{}.html", cands)

    def test_category_only_url_is_not_turned_into_page_template(self) -> None:
        cands = templates_from_url("https://new.com/vodtype/144.html", "144")
        self.assertNotIn("https://new.com/vodtype/{}.html", cands)
        self.assertIn("https://new.com/vodtype/144.html?page={}", cands)

    def test_candidate_templates_are_absolute(self) -> None:
        cands = candidate_templates(
            "https://new.com/vodtype/144-{}.html",
            ("/vodtype/{cat}-{page}.html", "/vodshow/{cat}--------{page}---.html"),
        )
        self.assertEqual(cands[0], "https://new.com/vodtype/144-{}.html")
        self.assertEqual(cands[1], "https://new.com/vodshow/144--------{}---.html")


class HomepageDiscoveryTests(unittest.TestCase):
    def test_homepage_links_derive_templates(self) -> None:
        html = (
            "<a href='/vodtype/144.html'>分类144</a>"
            "<a href='/vodtype/145.html'>分类145</a>"
            "<a href='/voddetail/999.html'>详情</a>"
        )
        found = templates_from_homepage(html, "https://new.com/vodtype/144-{}.html", "144")
        self.assertTrue(any("144" in t for t in found))
        self.assertFalse(any("/voddetail/" in t for t in found))

    def test_homepage_route_prefix_with_changed_category_id(self) -> None:
        html = "<a href='/vodshow/777.html'>新分类</a>"
        found = templates_from_homepage(html, "https://new.com/vodtype/144-{}.html", "144")
        self.assertTrue(any("vodshow/144" in t for t in found))

    def test_directory_listing_is_not_accepted_as_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            listing = b"<html><body><a href='1.html'>1.html</a></body></html>"

            async def get_raw(url: str):
                if url.endswith("/") or "?page=" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=listing)
                return FetchedResponse(url=url, status=404, headers={}, content=b"nf")

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            tpl = "https://new.com/vodtype/144-{}.html"
            self.assertEqual(asyncio.run(probe.ensure_list_url(tpl, 1)), tpl)


class NoticeChainTests(unittest.TestCase):
    def test_redirect_then_notice_then_real_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            list_html = (
                "<html><body>"
                + "".join(f"<a href='/play/{i}.html'>v{i}</a>" for i in range(1, 8))
                + "</body></html>"
            ).encode()

            def resp(url, status, content=b"", final=None):
                return FetchedResponse(url=final or url, status=status, headers={}, content=content)

            async def get_raw(url: str):
                if url.rstrip("/") == "https://qing9wa.com":
                    # 第一跳：旧域名 302 到说明页
                    return resp(url, 200, b"redirect", final="https://notice.example.net/")
                if url.rstrip("/") == "https://notice.example.net":
                    body = (
                        "<html><body>本站新地址：https://real-cinema.com/vodtype/144.html</body></html>"
                    ).encode()
                    return resp(url, 200, body)
                if "/vodtype/144" in url and "real-cinema.com" in url:
                    return resp(url, 200, list_html)
                return resp(url, 404, b"nf")

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            got = asyncio.run(probe.ensure_list_url("https://qing9wa.com/vodtype/144-{}.html", 1))
            self.assertEqual(got, "https://real-cinema.com/vodtype/144.html?page={}")
            self.assertEqual(cfg.base, "https://real-cinema.com")


class ListUrlEnsureTests(unittest.TestCase):
    def test_404_old_path_is_repaired_from_notice_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            list_html = (
                "<html><body>"
                + "".join(f"<a href='/play/{i}.html'>v{i}</a>" for i in range(1, 8))
                + "</body></html>"
            ).encode()

            async def get_raw(url: str):
                if "/vodtype/144-1.html" in url:
                    return FetchedResponse(url=url, status=404, headers={}, content=b"not found")
                if "/vodshow/144" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=list_html)
                return FetchedResponse(url=url, status=404, headers={}, content=b"")

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            probe._notice_url = "https://new.com/vodshow/144--------1---.html"
            got = asyncio.run(probe.ensure_list_url("https://new.com/vodtype/144-{}.html", 1))
            self.assertEqual(got, "https://new.com/vodshow/144--------{}---.html")

    def test_working_list_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            list_html = (
                "<html><body>"
                + "".join(f"<a href='/play/{i}.html'>v{i}</a>" for i in range(1, 8))
                + "</body></html>"
            ).encode()

            async def get_raw(url: str):
                return FetchedResponse(url=url, status=200, headers={}, content=list_html)

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            tpl = "https://new.com/vodtype/144-{}.html"
            self.assertEqual(asyncio.run(probe.ensure_list_url(tpl, 1)), tpl)


class DomainProbeTests(unittest.TestCase):
    def test_redirect_adoption_and_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            probe = DomainProbe(
                cfg,
                RuleManager(),
                get_raw=lambda url: None,
                new_page=lambda: None,
                log=lambda msg: None,
                ua="test-ua",
            )
            self.assertEqual(
                probe._adopt_redirect("https://old.com/a/1.html", "https://new.com/b/1.html"),
                "https://new.com/a/1.html",
            )
            probe.set_base("https://new.com/b/1.html")
            self.assertEqual(cfg.base, "https://new.com")

    def test_adopt_host_rebases_pending_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            # 候选域名里已经有新域名时，也应该允许从候选切过去
            cfg.candidates = [
                "https://example.com/list/1-{}.html",
                "https://new.example.com/list/1-{}.html",
            ]
            probe = DomainProbe(
                cfg,
                RuleManager(),
                get_raw=lambda url: None,
                new_page=lambda: None,
                log=lambda msg: None,
                ua="test-ua",
            )
            self.assertTrue(probe.adopt_host("https://new.example.com/x"))
            self.assertEqual(cfg.base, "https://new.example.com")
            self.assertEqual(cfg.list_url, "https://new.example.com/list/1-{}.html")
            self.assertEqual(
                probe.rebase_url("https://example.com/v/1?q=2"), "https://new.example.com/v/1?q=2"
            )

    def test_resolve_base_keeps_reachable_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)

            async def get_raw(url: str):
                return FetchedResponse(
                    url=url, status=200, headers={}, content=b"<html><body>ok</body></html>"
                )

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test-ua"
            )
            got = asyncio.run(probe.resolve_base("https://example.com/list/1-{}.html"))
            self.assertEqual(got, "https://example.com/list/1-{}.html")


class _FakeNoticePage:
    def __init__(self, url: str, title: str, body: str):
        self.url = url
        self._title = title
        self._body = body

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return self._body


class DomainNoticeTests(unittest.TestCase):
    def test_extract_and_replace_host(self) -> None:
        rules = RuleManager()
        text = "本站新域名：https://new-site.com/path/a"
        self.assertEqual(find_new_host(text, rules.pipeline.new_domain_re, {"old.com"}), "new-site.com")
        self.assertEqual(clean_host("https://New-Site.com:8443/x"), "new-site.com")
        self.assertEqual(
            replace_host("https://old.com/v/1?q=2", {"old.com"}, "new.com"), "https://new.com/v/1?q=2"
        )

    def test_page_notice_host_from_text_or_popup_url(self) -> None:
        rules = RuleManager()

        async def run():
            text_page = _FakeNoticePage("https://old.com/x", "发布页", "最新地址：https://new-site.com/x")
            url_page = _FakeNoticePage("https://new-site.com/x", "发布页", "点击进入")
            return (
                (
                    await page_notice_target(
                        text_page, rules.pipeline.new_domain_re, rules.pipeline.shell_hints, {"old.com"}
                    )
                )[0],
                (
                    await page_notice_target(
                        url_page, rules.pipeline.new_domain_re, rules.pipeline.shell_hints, {"old.com"}
                    )
                )[0],
            )

        self.assertEqual(asyncio.run(run()), ("new-site.com", "new-site.com"))

    def test_static_resolver_raises_new_domain(self) -> None:
        async def fetch(url: str) -> str:
            return "<html><body>最新地址：new-site.com</body></html>"

        resolver = StaticResolver(ParserContext(fetch=fetch, state={"rules": RuleManager()}))
        with self.assertRaises(NewDomainError) as ctx:
            asyncio.run(
                resolver.resolve(VideoItem(id="https://old.com/play/1", title="x"), "https://old.com")
            )
        self.assertEqual(ctx.exception.host, "new-site.com")


class _FakeHls:
    def __init__(self, mb: float = 2.5):
        self.mb = mb
        self.calls: list[str] = []

    async def download(self, m3u8_url: str, name: str, mp4: str, vi: int | None = None) -> float:
        self.calls.append(name)
        return self.mb


class _DomainChangedOnceHls(_FakeHls):
    async def download(self, m3u8_url: str, name: str, mp4: str, vi: int | None = None) -> float:
        self.calls.append(name)
        if len(self.calls) == 1:
            raise DomainChanged()
        return self.mb


class VideoWorkerTests(unittest.TestCase):
    def test_success_path_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)
            state.add_items([VideoItem(id="https://example.com/v/1", title="T")], 1, "https://example.com")
            hls = _FakeHls()

            async def resolve(item: VideoItem) -> tuple[str, str]:
                return "https://example.com/m.m3u8", item.title

            async def reprobe(why: str) -> bool:
                return False

            worker = VideoWorker(
                cfg,
                RuleManager(),
                state,
                hls,  # type: ignore[arg-type]
                resolve=resolve,
                reprobe=reprobe,
                log=lambda msg: None,
                stop_check=lambda: None,
                is_stopped=lambda: False,
            )
            asyncio.run(worker.run(1, state.items[0]))

            self.assertEqual(len(hls.calls), 1)
            self.assertEqual(state.ok, 1)
            self.assertEqual(state.st["videos"][0]["s"], 2)
            self.assertEqual(state.st["stages"]["out"]["count"], 1)
            self.assertAlmostEqual(state.st["percent"], 98.0)

    def test_existing_mp4_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)
            state.add_items([VideoItem(id="https://example.com/v/1", title="T")], 1, "https://example.com")
            name = "001_T"
            with open(os.path.join(tmp, name + ".mp4"), "wb") as f:
                f.write(b"x")
            hls = _FakeHls()
            worker = VideoWorker(
                cfg,
                RuleManager(),
                state,
                hls,  # type: ignore[arg-type]
                resolve=lambda item: None,
                reprobe=lambda why: None,
                log=lambda msg: None,
                stop_check=lambda: None,
                is_stopped=lambda: False,
            )
            asyncio.run(worker.run(1, state.items[0]))
            self.assertEqual(hls.calls, [])
            self.assertEqual(state.st["videos"][0]["s"], 4)
            self.assertEqual(state.skip, 1)

    def test_skip_marks_the_correct_item_as_seen(self) -> None:
        # 回归：跳过第 N 个视频时，_seen_web.json 只能记第 N 个，不能记成第 N-1 个。
        # 否则增量模式下会把「前一个」视频误记为已下载，导致它被永久跳过。
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)
            state.add_items(
                [
                    VideoItem(id="https://example.com/v/1", title="A"),
                    VideoItem(id="https://example.com/v/2", title="B"),
                ],
                1,
                "https://example.com",
            )
            name_b = "002_B"
            with open(os.path.join(tmp, name_b + ".mp4"), "wb") as f:
                f.write(b"x")
            hls = _FakeHls()
            worker = VideoWorker(
                cfg,
                RuleManager(),
                state,
                hls,  # type: ignore[arg-type]
                resolve=lambda item: None,
                reprobe=lambda why: None,
                log=lambda msg: None,
                stop_check=lambda: None,
                is_stopped=lambda: False,
            )
            asyncio.run(worker.run(2, state.items[1]))
            self.assertIn("https://example.com/v/2", state.seen)
            self.assertNotIn("https://example.com/v/1", state.seen)
            self.assertEqual(state.st["videos"][0]["s"], 0)
            self.assertEqual(state.st["videos"][1]["s"], 4)

    def test_domain_changed_re_resolves_without_consuming_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)
            state.add_items([VideoItem(id="https://example.com/v/1", title="T")], 1, "https://example.com")
            hls = _DomainChangedOnceHls()
            resolves: list[int] = []

            async def resolve(item: VideoItem) -> tuple[str, str]:
                resolves.append(1)
                return f"https://example.com/{len(resolves)}.m3u8", item.title

            async def reprobe(why: str) -> bool:
                return False

            worker = VideoWorker(
                cfg,
                RuleManager(),
                state,
                hls,  # type: ignore[arg-type]
                resolve=resolve,
                reprobe=reprobe,
                log=lambda msg: None,
                stop_check=lambda: None,
                is_stopped=lambda: False,
            )
            asyncio.run(worker.run(1, state.items[0]))
            self.assertEqual(len(resolves), 2)
            self.assertEqual(len(hls.calls), 2)
            self.assertEqual(state.st["videos"][0]["s"], 2)
            self.assertEqual(state.ok, 1)


class ListPageFailureTests(unittest.TestCase):
    """列表页抓取失败不能被当成“空页”，否则连续两页失败就会误判分类末尾。"""

    def _runner(self, cfg, state, fetch):
        rules = RuleManager()
        rules.browser.use_api_capture = False
        return ListRunner(
            cfg,
            rules,
            state,
            fetch=fetch,
            static_list=StaticListParser(),
            browser_list=None,
            new_page=_fake_new_page,
            domain=_FakeDomain(),  # type: ignore[arg-type]
            log=lambda msg: None,
            is_stopped=lambda: False,
            dispatch=lambda n, item: None,
        )

    def test_all_pages_failed_raises_with_page_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp, p2=3)
            state = JobState(cfg)

            async def fetch(url: str, **kw):
                raise RuntimeError("static parse broken")

            runner = self._runner(cfg, state, fetch)
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(runner.run())
            self.assertIn("抓取失败", str(ctx.exception))
            self.assertEqual(state.st["stages"]["list"]["failed"], 3)
            self.assertTrue(state.st["warnings"])

    def test_partial_failure_keeps_going_and_records_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = JobConfig(list_url="https://example.com/list/{}.html", p1=1, p2=3, outdir=tmp, workers=1)
            state = JobState(cfg)
            html = "<html><body><a href='/play/1.html'>唯一视频</a></body></html>"

            async def fetch(url: str, **kw):
                if url.endswith("/list/1.html"):
                    return html
                raise RuntimeError("blocked by waf")

            runner = self._runner(cfg, state, fetch)
            asyncio.run(runner.run())
            self.assertEqual(len(state.items), 1)
            self.assertEqual(state.st["stages"]["list"]["failed"], 2)
            self.assertTrue(any("抓取失败" in w for w in state.st["warnings"]))


class HlsIntegrityTests(unittest.TestCase):
    def _hls(
        self,
        tmp: str,
        state: JobState,
        playlist: str,
        media: list[str] | None = None,
        fetched: list[str] | None = None,
    ) -> HlsDownloader:
        async def fetch(url: str, **kw):
            return playlist

        async def fetch_media(url: str, query: str, **kw):
            if fetched is not None:
                fetched.append(url)
            return (media or [b"\x47" + b"\x00" * 187] * 3)[0]

        return HlsDownloader(
            tmp,
            2,
            state=state,
            fetch=fetch,
            fetch_media=fetch_media,
            log=lambda msg: None,
            is_stopped=lambda: False,
            abort_msg=lambda: "",
        )

    def test_live_playlist_without_endlist_is_rejected_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            fetched: list[str] = []
            playlist = "#EXTM3U\n#EXTINF:4,\na.ts\n#EXTINF:4,\nb.ts\n"
            hls = self._hls(tmp, state, playlist, fetched=fetched)
            with patch("app.services.hls.ffmpeg_exe", lambda: "ffmpeg"):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(
                        hls.download("https://example.com/index.m3u8", "live", os.path.join(tmp, "live.mp4"))
                    )
            self.assertIn("EXT-X-ENDLIST", str(ctx.exception))
            self.assertEqual(fetched, [])

    def test_fingerprint_changes_with_host_and_media_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = JobState(_cfg(tmp))
            hls = self._hls(tmp, state, "#EXTM3U\n#EXT-X-ENDLIST\n")
            segs = ["https://a.com/x/0.ts"]
            args = ([b"k" * 16], [b"\x01" * 16])
            fp_a = hls._plan_fingerprint(segs, *args, host="a.com", media_seq=0, endlist=True)
            fp_b = hls._plan_fingerprint(segs, *args, host="b.com", media_seq=0, endlist=True)
            fp_c = hls._plan_fingerprint(segs, *args, host="a.com", media_seq=5, endlist=True)
            self.assertNotEqual(fp_a, fp_b)  # 换域名后不能复用旧分片
            self.assertNotEqual(fp_a, fp_c)  # 换一轮直播后也不能复用

    def test_bad_segment_detection(self) -> None:
        self.assertIn("HTML", HlsDownloader._bad_segment(b"<!DOCTYPE html><html>", True))
        self.assertIn("AES", HlsDownloader._bad_segment(b"x" * 17, True))
        self.assertIn("TS", HlsDownloader._bad_segment(b"\x00" * 200, False))
        self.assertEqual(HlsDownloader._bad_segment(b"\x47" + b"\x00" * 187, False), "")

    def test_ts_validation_rejects_trailing_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.ts")
            with open(good, "wb") as f:
                f.write((b"\x47" + b"\x00" * 187) * 3)
            bad = os.path.join(tmp, "bad.ts")
            with open(bad, "wb") as f:
                f.write((b"\x47" + b"\x00" * 187) * 3 + b"tail")
            self.assertTrue(HlsDownloader._looks_like_ts(good))
            self.assertFalse(HlsDownloader._looks_like_ts(bad))


class DedupSafetyTests(unittest.TestCase):
    def _service(self, tmp: str) -> tuple[DedupService, str, str, str]:
        a = os.path.join(tmp, "a.mp4")
        b = os.path.join(tmp, "b.mp4")
        with open(a, "wb") as f:
            f.write(b"a" * 200)
        with open(b, "wb") as f:
            f.write(b"b" * 200)
        result = os.path.join(tmp, "_vdf_results.json")
        with open(result, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "GroupId": "g1",
                        "Items": [
                            {"Path": a, "SizeLong": 200},
                            {"Path": b, "SizeLong": 200},
                        ],
                    }
                ],
                f,
            )
        svc = DedupService()
        svc.st["result_file"] = result
        svc._scan_root = os.path.realpath(tmp)
        svc._scan_t0 = time.time() + 5  # 文件都“早于扫描开始”，视为未变化
        svc._scan_files = {os.path.realpath(a): 200, os.path.realpath(b): 200}
        return svc, a, b, result

    def test_permanent_delete_requires_recent_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, _a, _b, _result = self._service(tmp)
            with patch("app.services.dedup_service._vdf_exe", return_value="vdf"):
                r = svc.apply("lowest-quality", "permanent")
            self.assertFalse(r["ok"])
            self.assertIn("预览", r["error"])

    def test_filter_drops_files_changed_since_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, a, b, result = self._service(tmp)
            with open(b, "wb") as f:  # 扫描后被替换成不同大小
                f.write(b"changed" * 100)
            kept, dropped = svc._filter_result(result, svc._scan_root, svc._scan_t0)
            self.assertEqual(kept, [])
            self.assertGreaterEqual(dropped, 1)
            self.assertTrue(os.path.isfile(a))

    def test_apply_rejects_concurrent_and_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, _a, _b, _result = self._service(tmp)
            svc._applying = True
            self.assertFalse(svc.apply("lowest-quality", "dry-run")["ok"])
            svc._applying = False
            svc.st["running"] = True
            self.assertFalse(svc.apply("lowest-quality", "dry-run")["ok"])

    def test_dry_run_then_permanent_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, _a, _b, result = self._service(tmp)
            with (
                patch("app.services.dedup_service._vdf_exe", return_value="vdf"),
                patch.object(svc, "_capture", return_value=(0, "would delete: a")),
            ):
                r1 = svc.apply("lowest-quality", "dry-run")
                self.assertTrue(r1["ok"], r1.get("error"))
                r2 = svc.apply("lowest-quality", "permanent")
                self.assertTrue(r2["ok"], r2.get("error"))
            self.assertTrue(os.path.isfile(result))


class PersistenceTests(unittest.TestCase):
    def test_state_dump_restore_and_single_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            state = JobState(cfg)
            state.add_items(
                [VideoItem(id="u1", title="a"), VideoItem(id="u2", title="b")], 1, "https://example.com"
            )
            state.vset(0, s=3, e="boom")
            state.vset(1, s=2)
            state.finish()
            path = os.path.join(tmp, "_job_state.json")
            state.dump_state(path)
            restored = JobState(cfg)
            with open(path, encoding="utf-8") as f:
                self.assertTrue(restored.restore_state(json.load(f)))
            self.assertEqual([it.id for it in restored.items], ["u1", "u2"])
            pairs, err = restored.prepare_retry(only=[1])
            self.assertEqual(err, "")
            self.assertEqual([n for n, _ in pairs], [1])

    def test_probe_rejects_tiny_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "tiny.mp4")
            with open(p, "wb") as f:
                f.write(b"x" * 10)
            ok, why = probe_media(p)
            self.assertFalse(ok)
            self.assertIn("过小", why)


class VerifiedAdoptTests(unittest.TestCase):
    """播放页/弹窗提示的新域名必须能打开列表页才采纳，避免广告域名把任务带跑。"""

    def test_rejects_domain_without_usable_list_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            old_base = cfg.base

            async def get_raw(url: str):
                return FetchedResponse(url=url, status=404, headers={}, content=b"not found")

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            self.assertFalse(asyncio.run(probe.adopt_host_verified("ads.example.net")))
            self.assertEqual(cfg.base, old_base)

    def test_accepts_domain_with_real_list_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            html = (
                "<html><body>"
                + "".join(f"<a href='/play/{i}.html'>v{i}</a>" for i in range(6))
                + "</body></html>"
            ).encode()

            async def get_raw(url: str):
                return FetchedResponse(url=url, status=200, headers={}, content=html)

            probe = DomainProbe(
                cfg, RuleManager(), get_raw=get_raw, new_page=lambda: None, log=lambda msg: None, ua="test"
            )
            self.assertTrue(asyncio.run(probe.adopt_host_verified("new.example.com")))
            self.assertEqual(cfg.base, "https://new.example.com")


class NextClickPaginationTests(unittest.TestCase):
    """URL 翻页无效（page 参数被忽略 / 纯 JS 换内容）时用浏览器点「下一页」。"""

    def test_collect_via_next_click_walks_js_pages(self) -> None:
        from app.parsers.list.browser import BrowserListParser

        class _Loc:
            async def count(self) -> int:
                return 0

            async def first(self):
                return self

        class _Page:
            def __init__(self) -> None:
                self.page = 0
                self.url = "https://example.com/list"

            async def goto(self, *a, **k):
                return None

            async def wait_for_load_state(self, *a, **k):
                return None

            async def wait_for_timeout(self, *a, **k):
                return None

            def locator(self, *a, **k):
                return _Loc()

            async def evaluate(self, js, *args):
                if "words =" in js:  # _NEXT_JS：点一次“下一页”
                    self.page += 1
                    return True
                return [{"h": f"/play/{self.page}-{i}.html", "t": f"第{self.page}页{i}"} for i in range(2)]

        rules = RuleManager()

        async def _fetch(*a, **k):
            return ""

        parser = BrowserListParser(ParserContext(fetch=_fetch, state={"rules": rules}))
        items = asyncio.run(parser.collect_via_next_click(_Page(), "https://example.com/list", 1, 3))
        self.assertEqual([it.page for it in items], [1, 1, 2, 2, 3, 3])
        self.assertEqual(len({it.id for it in items}), 6)
        # 起始页 > 1：前 p1-1 页只点不收集
        later = asyncio.run(parser.collect_via_next_click(_Page(), "https://example.com/list", 2, 3))
        self.assertEqual([it.page for it in later], [2, 2, 3, 3])

    def test_collect_falls_back_to_infinite_scroll(self) -> None:
        from app.parsers.list.browser import BrowserListParser

        class _Loc:
            async def count(self) -> int:
                return 0

            async def first(self):
                return self

        class _Page:
            def __init__(self) -> None:
                self.page = 0
                self.url = "https://example.com/list"

            async def goto(self, *a, **k):
                return None

            async def wait_for_load_state(self, *a, **k):
                return None

            async def wait_for_timeout(self, *a, **k):
                return None

            def locator(self, *a, **k):
                return _Loc()

            async def evaluate(self, js, *args):
                if "words =" in js:
                    return False  # 没有“下一页”按钮
                if "scrollTo" in js:
                    self.page += 1  # 滚到底加载下一页
                    return None
                return [{"h": f"/play/{self.page}-{i}.html", "t": f"第{self.page}页{i}"} for i in range(2)]

        rules = RuleManager()

        async def _fetch(*a, **k):
            return ""

        parser = BrowserListParser(ParserContext(fetch=_fetch, state={"rules": rules}))
        items = asyncio.run(parser.collect_via_next_click(_Page(), "https://example.com/list", 1, 2))
        self.assertEqual([it.page for it in items], [1, 1, 2, 2])

    def test_list_runner_falls_back_when_url_pages_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = JobConfig(list_url="https://example.com/list/{}.html", p1=1, p2=3, outdir=tmp, workers=1)
            state = JobState(cfg)
            rules = RuleManager()
            rules.browser.use_api_capture = False
            html = "<html><body><a href='/play/1.html'>同一视频</a></body></html>"

            async def fetch(url: str, **kw):
                return html  # 所有页码都返回第 1 页内容

            class _BrowserList:
                async def parse_with_page(self, page, url, p):
                    return ListPage(page=p, items=[])

                async def collect_via_next_click(self, page, list_url, p1, p2):
                    return [
                        VideoItem(id=f"https://example.com/v/{pg}", title=f"第{pg}页视频", page=pg)
                        for pg in range(p1, p2 + 1)
                    ]

            @asynccontextmanager
            async def new_page():
                yield object(), []

            runner = ListRunner(
                cfg,
                rules,
                state,
                fetch=fetch,
                static_list=StaticListParser(),
                browser_list=_BrowserList(),
                new_page=new_page,
                domain=_FakeDomain(),  # type: ignore[arg-type]
                log=lambda msg: None,
                is_stopped=lambda: False,
                dispatch=lambda n, item: None,
            )
            asyncio.run(runner.run())
            titles = [it.title for it in state.items]
            self.assertIn("第2页视频", titles)
            self.assertIn("第3页视频", titles)
            self.assertTrue(any("翻页无效" in w for w in state.st["warnings"]))


class NoticePlainTextTests(unittest.TestCase):
    """旧域名失效 → 跳转/发布页（只写新地址）→ 新域名，整条链路要能自动跟随。"""

    LIST_HTML = (
        "<html><body>" + "".join(f"<a href='/play/{i}.html'>v{i}</a>" for i in range(6)) + "</body></html>"
    ).encode()

    def _cfg(self, tmp: str) -> JobConfig:
        return JobConfig(list_url="https://old.example/list/1-{}.html", p1=1, p2=2, outdir=tmp)

    def _probe(self, tmp: str, get_raw) -> DomainProbe:
        return DomainProbe(
            self._cfg(tmp),
            RuleManager(),
            get_raw=get_raw,
            new_page=lambda: None,
            log=lambda msg: None,
            ua="test",
        )

    def test_plain_text_new_address_on_homepage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:

            async def get_raw(url: str):
                if "old.example/list" in url:
                    return FetchedResponse(url=url, status=404, headers={}, content=b"")
                if "old.example/" in url:
                    return FetchedResponse(
                        url=url, status=200, headers={}, content="本站已迁移 zzzz。com".encode()
                    )
                if "zzzz.com/list/1-1.html" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=self.LIST_HTML)
                return FetchedResponse(url=url, status=404, headers={}, content=b"")

            probe = self._probe(tmp, get_raw)
            got = asyncio.run(probe.ensure_list_url("https://old.example/list/1-{}.html", 1))
            self.assertIn("zzzz.com", got)

    def test_old_home_redirect_then_plain_text_second_hop(self) -> None:
        """xxxx → yyyy(HTTP 跳转) → yyyy 正文只写 zzzz → zzzz 列表可用。"""
        with tempfile.TemporaryDirectory() as tmp:

            async def get_raw(url: str):
                if "old.example/list" in url:
                    return FetchedResponse(url=url, status=404, headers={}, content=b"")
                if "old.example/" in url:
                    return FetchedResponse(
                        url="https://yyyy.example/", status=200, headers={}, content=b"<html></html>"
                    )
                if "yyyy.example/list" in url:
                    return FetchedResponse(url=url, status=404, headers={}, content=b"")
                if "yyyy.example/" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=b"zzzz.example")
                if "zzzz.example/list/1-1.html" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=self.LIST_HTML)
                return FetchedResponse(url=url, status=404, headers={}, content=b"")

            probe = self._probe(tmp, get_raw)
            got = asyncio.run(probe.ensure_list_url("https://old.example/list/1-{}.html", 1))
            self.assertIn("zzzz.example", got)

    def test_blank_shell_list_page_triggers_notice_chain(self) -> None:
        """旧列表页返回 200 但只有一行新地址（不是 404）也要能跟随。"""
        with tempfile.TemporaryDirectory() as tmp:

            async def get_raw(url: str):
                if "old.example/list" in url:
                    return FetchedResponse(
                        url=url, status=200, headers={}, content=b"<html><body>zzzz.example</body></html>"
                    )
                if "old.example/" in url:
                    return FetchedResponse(url=url, status=404, headers={}, content=b"")
                if "zzzz.example/list/1-1.html" in url:
                    return FetchedResponse(url=url, status=200, headers={}, content=self.LIST_HTML)
                return FetchedResponse(url=url, status=404, headers={}, content=b"")

            probe = self._probe(tmp, get_raw)
            got = asyncio.run(probe.ensure_list_url("https://old.example/list/1-{}.html", 1))
            self.assertIn("zzzz.example", got)


class MainSafetyTests(unittest.TestCase):
    def test_allowed_roots_guard(self) -> None:
        from main import _safe_dir

        with tempfile.TemporaryDirectory() as tmp:
            inside = os.path.join(tmp, "sub")
            os.makedirs(inside)
            roots = [os.path.realpath(tmp)]
            self.assertEqual(_safe_dir(inside, roots), os.path.realpath(inside))
            with self.assertRaises(ValueError):
                _safe_dir(os.path.dirname(os.path.realpath(tmp)), roots)


if __name__ == "__main__":
    unittest.main()
