"""全链路集成：本地列表页 → 播放页 → AES-128 HLS → ffmpeg，跑完整 Job.run()。"""

from __future__ import annotations

import functools
import http.server
import os
import subprocess
import tempfile
import threading
import unittest

from app.core.models import JobConfig
from app.services.job import Job
from app.toolkit.files import ffmpeg_exe, no_window

try:
    EXE = ffmpeg_exe()
except Exception:  # pragma: no cover - 没装 ffmpeg 时跳过
    EXE = ""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _make_hls(root: str, key: bytes) -> None:
    key_file = os.path.join(root, "key.bin")
    with open(key_file, "wb") as f:
        f.write(key)
    key_info = os.path.join(root, "key_info.txt")
    with open(key_info, "w", encoding="utf-8") as f:
        f.write("key.bin\n")
        f.write(key_file + "\n")
    common = [
        EXE,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x120:rate=10",
        "-t",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_key_info_file",
        key_info,
        "-hls_segment_filename",
        os.path.join(root, "seg%03d.ts"),
    ]
    playlist = os.path.join(root, "index.m3u8")
    for codec in (["-c:v", "libx264"], ["-c:v", "mpeg2video"]):
        result = subprocess.run(
            common + codec + [playlist], capture_output=True, text=True, timeout=90, **no_window()
        )
        if result.returncode == 0:
            return
    raise unittest.SkipTest(f"ffmpeg 无法生成 HLS: {result.stderr[-400:]}")


@unittest.skipUnless(EXE, "需要 ffmpeg（PATH 或 imageio-ffmpeg）")
class FullPipelineTests(unittest.TestCase):
    def test_list_to_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_hls(tmp, os.urandom(16))
            os.makedirs(os.path.join(tmp, "list"))
            os.makedirs(os.path.join(tmp, "play"))
            with open(os.path.join(tmp, "list", "1.html"), "w", encoding="utf-8") as f:
                f.write('<html><body><a href="/play/1.html">测试视频一</a></body></html>')

            handler = functools.partial(_QuietHandler, directory=tmp)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with open(os.path.join(tmp, "play", "1.html"), "w", encoding="utf-8") as f:
                    f.write(
                        "<html><head><title>测试视频一 - 影院</title></head><body>"
                        "<h1>测试视频一</h1><script>"
                        f'var url = "http://127.0.0.1:{port}/index.m3u8";'
                        "</script></body></html>"
                    )
                outdir = os.path.join(tmp, "out")
                os.makedirs(outdir)
                cfg = JobConfig(
                    list_url=f"http://127.0.0.1:{port}/list/{{}}.html",
                    p1=1,
                    p2=1,
                    outdir=outdir,
                    workers=1,
                    seg_concurrency=4,
                )
                job = Job(cfg)
                job.rules.browser.use_api_capture = False
                thread = threading.Thread(target=job.run, daemon=True)
                thread.start()
                thread.join(90)
                if thread.is_alive():
                    job.shutdown()
                    self.fail("Job.run 超时")
                try:
                    self.assertEqual(job.st["error"], "")
                    self.assertFalse(job.st["running"])
                    self.assertEqual(job.st["stages"]["dec"]["ok"], 1)
                    self.assertEqual(job.st["stages"]["out"]["count"], 1)
                    mp4s = [n for n in os.listdir(outdir) if n.endswith(".mp4")]
                    self.assertEqual(len(mp4s), 1, os.listdir(outdir))
                    self.assertEqual([n for n in os.listdir(outdir) if n.endswith(".tmp")], [])
                    self.assertGreater(os.path.getsize(os.path.join(outdir, mp4s[0])), 0)
                finally:
                    job.dispose()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
