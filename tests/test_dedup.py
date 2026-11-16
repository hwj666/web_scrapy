"""VDF 查重服务的纯逻辑测试（不依赖外部 vdf 可执行文件）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from app.services.dedup_service import DedupService, _list_videos, _stage_kind


class DedupParsingTests(unittest.TestCase):
    def test_stage_kind(self) -> None:
        self.assertEqual(_stage_kind("AI matching"), "ai")
        self.assertEqual(_stage_kind("Comparing 3/10"), "compare")
        self.assertEqual(_stage_kind("Hashing 5/20"), "hash")
        self.assertEqual(_stage_kind("Sampling frames"), "sample")
        self.assertEqual(_stage_kind("Scanning file"), "scan")

    def test_list_videos_skips_db_and_non_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "_vdf_db"))
            open(os.path.join(tmp, "a.mp4"), "wb").close()
            open(os.path.join(tmp, "b.txt"), "w", encoding="utf-8").close()
            open(os.path.join(tmp, "_vdf_db", "cached.mp4"), "wb").close()
            files = _list_videos(tmp)
            self.assertEqual([f["n"] for f in files], ["a.mp4"])

    def test_load_groups_marks_keep_and_wasted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "_vdf_results.json")
            raw = [{
                "GroupId": "g1",
                "Items": [
                    {"Path": "/v/a.mp4", "SizeLong": 100, "Similarity": 99.5,
                     "FrameSize": "1920x1080", "BitRateKbs": 3000,
                     "Duration": "00:01:00", "IsBestBitRateKbs": True},
                    {"Path": "/v/b.mp4", "SizeLong": 400, "Similarity": 98.0,
                     "FrameSize": "1280x720", "BitRateKbs": 1000,
                     "Duration": "00:01:10"},
                ],
            }]
            with open(result, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            svc = DedupService()
            groups = svc._load_groups(result)
            self.assertEqual(len(groups), 1)
            items = groups[0]["items"]
            self.assertTrue(items[0]["keep"])
            self.assertFalse(items[1]["keep"])
            self.assertEqual(groups[0]["wasted"], 400)

    def test_match_file_by_basename(self) -> None:
        svc = DedupService()
        files = [
            {"p": "/v/a.mp4", "n": "a.mp4", "sz": 1, "s": 0, "st": "",
             "c": 0, "fc": 0, "fm": 0},
            {"p": "/v/b.mp4", "n": "b.mp4", "sz": 1, "s": 0, "st": "",
             "c": 0, "fc": 0, "fm": 0},
        ]
        svc._by_base = {"a.mp4": 0, "b.mp4": 1}
        self.assertEqual(svc._match_file(files, ".../a.mp4"), 0)
        self.assertEqual(svc._match_file(files, "b.mp4"), 1)
        self.assertIsNone(svc._match_file(files, "missing.mp4"))


if __name__ == "__main__":
    unittest.main()
