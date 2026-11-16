"""纯函数单测：m3u8 解析、密钥计划、URL 与编解码工具（不依赖网络/第三方库）。"""
from __future__ import annotations

import unittest

from app.toolkit.codec import decode_body, decode_page, unpad_pkcs7
from app.toolkit.m3u8 import (
    abs_segment,
    best_variant,
    has_init_segment,
    key_info,
    key_schedule,
    media_lines,
    media_sequence,
)
from app.toolkit.url import abs_url, auto_list_url, split_urls


class M3u8Tests(unittest.TestCase):
    def test_media_sequence(self) -> None:
        self.assertEqual(media_sequence("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:42\n"), 42)
        self.assertEqual(media_sequence("#EXTM3U\n"), 0)

    def test_media_lines_ignore_tags(self) -> None:
        text = "#EXTM3U\n#EXTINF:4,\nseg0.ts\n\n#EXTINF:4,\nseg1.ts\n"
        self.assertEqual(media_lines(text), ["seg0.ts", "seg1.ts"])

    def test_key_schedule_derives_iv_from_sequence(self) -> None:
        text = (
            "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
            "#EXTINF:4,\na.ts\n#EXTINF:4,\nb.ts\n"
        )
        slots = key_schedule(text)
        self.assertEqual([s[0] for s in slots], ["key.bin", "key.bin"])
        # 没有显式 IV 时保留 None，由调用方用 seq 派生 128 位大端 IV
        self.assertIsNone(slots[0][1])
        self.assertEqual([s[3] for s in slots], [7, 8])

    def test_key_schedule_supports_rotation_and_clear(self) -> None:
        text = (
            "#EXTM3U\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"k1\",IV=0x00000000000000000000000000000001\n"
            "a.ts\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"k2\",IV=0x00000000000000000000000000000002\n"
            "b.ts\n"
            "#EXT-X-KEY:METHOD=NONE\n"
            "c.ts\n"
        )
        slots = key_schedule(text)
        self.assertEqual([s[0] for s in slots], ["k1", "k2", ""])
        self.assertEqual(slots[0][1], b"\x00" * 15 + b"\x01")
        self.assertEqual(slots[1][1], b"\x00" * 15 + b"\x02")
        self.assertIsNone(slots[2][1])

    def test_key_schedule_rejects_unsupported_method(self) -> None:
        with self.assertRaises(ValueError):
            key_schedule("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"k\"\nseg.ts\n")

    def test_key_schedule_rejects_bad_iv(self) -> None:
        with self.assertRaises(ValueError):
            key_schedule("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\",IV=0x12\nseg.ts\n")

    def test_key_schedule_rejects_drm_keyformat(self) -> None:
        with self.assertRaises(ValueError):
            key_schedule("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\","
                         "KEYFORMAT=\"com.apple.streamingkeydelivery\"\nseg.ts\n")

    def test_key_info_still_detects_true_rotation(self) -> None:
        text = (
            "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"a\"\nseg0.ts\n"
            "#EXT-X-KEY:METHOD=AES-128,URI=\"b\"\nseg1.ts\n"
        )
        with self.assertRaises(ValueError):
            key_info(text)

    def test_abs_segment_does_not_inherit_query_by_default(self) -> None:
        self.assertEqual(
            abs_segment("seg.ts", "https://cdn.example.com/a/index.m3u8?sign=x"),
            "https://cdn.example.com/a/seg.ts")
        self.assertEqual(
            abs_segment("//cdn.example.com/seg.ts",
                        "https://cdn.example.com/a/index.m3u8"),
            "https://cdn.example.com/seg.ts")

    def test_master_playlist(self) -> None:
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2400000\nhigh.m3u8\n"
        )
        self.assertEqual(best_variant(text), "high.m3u8")

    def test_fmp4_detection(self) -> None:
        self.assertTrue(has_init_segment(
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\nseg.m4s\n'))
        self.assertFalse(has_init_segment("#EXTM3U\nseg.ts\n"))


class UrlCodecTests(unittest.TestCase):
    def test_split_urls(self) -> None:
        self.assertEqual(
            split_urls("https://a.com/1, https://b.com/2；https://a.com/1"),
            ["https://a.com/1", "https://b.com/2"])

    def test_auto_list_url(self) -> None:
        self.assertEqual(auto_list_url("https://x.com/list/3.html", 9),
                         "https://x.com/list/9.html")
        self.assertEqual(auto_list_url("https://x.com/list/{}", 9),
                         "https://x.com/list/9")
        self.assertEqual(auto_list_url("https://x.com/list/", 2),
                         "https://x.com/list?page=2")

    def test_abs_url(self) -> None:
        self.assertEqual(abs_url("/a/b", "https://x.com/c/d"), "https://x.com/a/b")
        self.assertEqual(abs_url("https://y.com/z", "https://x.com"), "https://y.com/z")

    def test_decode_and_unpad(self) -> None:
        self.assertEqual(unpad_pkcs7(b"hello\x03\x03\x03"), b"hello")
        self.assertEqual(unpad_pkcs7(b"hello"), b"hello")
        self.assertEqual(decode_body("中文".encode("gb18030"), "text/html; charset=gb18030"),
                         "中文")
        page = 'document.write(decodeURIComponent("%3Cp%3Ehi%3C%2Fp%3E"))<script>x</script>'
        self.assertEqual(decode_page(page).strip(), "<p>hi</p>")


if __name__ == "__main__":
    unittest.main()
