"""端到端 HLS 集成测试：现场生成 AES-128 加密 HLS，走完整分片下载→解密→合成。

缺 ffmpeg/依赖时自动跳过；不访问外网（本地起一个只读 HTTP 服务）。
"""

from __future__ import annotations

import asyncio
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
    def log_message(self, *_args) -> None:  # noqa: D401 - 测试里不需要访问日志
        pass


def _make_hls(root: str, key: bytes) -> str:
    """用 ffmpeg 生成 2 秒、2 个分片的 AES-128 HLS（KEY 不带 IV）。"""
    key_file = os.path.join(root, "key.bin")
    with open(key_file, "wb") as f:
        f.write(key)
    key_info = os.path.join(root, "key_info.txt")
    with open(key_info, "w", encoding="utf-8") as f:
        f.write("key.bin\n")
        f.write(key_file + "\n")

    playlist = os.path.join(root, "index.m3u8")
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
    for codec in (["-c:v", "libx264"], ["-c:v", "mpeg2video"]):
        r = subprocess.run(
            common + codec + [playlist], capture_output=True, text=True, timeout=90, **no_window()
        )
        if r.returncode == 0:
            return playlist
    raise unittest.SkipTest(f"ffmpeg 无法生成 HLS: {r.stderr[-400:]}")


@unittest.skipUnless(EXE, "需要 ffmpeg（PATH 或 imageio-ffmpeg）")
class EncryptedHlsTests(unittest.TestCase):
    def test_download_decrypt_and_remux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = os.urandom(16)
            infile = _make_hls(tmp, key)
            handler = functools.partial(_QuietHandler, directory=tmp)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{port}/{os.path.basename(infile)}"
                outdir = os.path.join(tmp, "out")
                os.makedirs(outdir)
                mp4 = os.path.join(outdir, "video.mp4")
                job = Job(JobConfig(list_url=url, outdir=outdir))
                try:
                    mb = asyncio.run(job._hls_to_mp4(url, "video", mp4))
                finally:
                    job.dispose()
                self.assertGreater(mb, 0)
                self.assertTrue(os.path.exists(mp4))
                self.assertGreater(os.path.getsize(mp4), 0)
                # 原子落盘：成功后不应留下 .mp4.tmp / .tmp.ts 中间文件
                self.assertFalse(os.path.exists(mp4 + ".tmp"))
                self.assertEqual([n for n in os.listdir(outdir) if n.endswith(".tmp.ts")], [])
                # 加密没解对时 ffmpeg 合成通常会失败或产出 0 字节；成功即说明
                # 「按媒体序列号派生 IV」与逐片解密链路是对的。
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
