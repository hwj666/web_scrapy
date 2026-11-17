"""分片落盘 / 解密合并（纯 IO + 加密，不依赖 Job）。"""

from __future__ import annotations

import os
import threading

from Crypto.Cipher import AES

from app.toolkit.codec import unpad_pkcs7


def save_segment(path: str, data: bytes) -> None:
    """分片先写临时文件再原子替换，避免中断后留下半截 .enc。"""
    tmp = f"{path}.part{threading.get_ident()}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def concat_segments(ts: str, parts: str, count: int, keys: list[bytes | None], ivs: list[bytes]) -> None:
    """按顺序解密并合并分片（阻塞 IO，调用方用 asyncio.to_thread 跑）。

    每个分片各自一条 CBC 链：key 可以逐片不同（密钥轮换），IV 也可以逐片不同
    （未显式给出 IV 时按 RFC 8216 用媒体序列号派生）。明文分片的 key 为 None。
    """
    with open(ts, "wb") as out:
        for i in range(count):
            with open(os.path.join(parts, f"{i:05d}.enc"), "rb") as f:
                data = f.read()
            key = keys[i] if keys and i < len(keys) else None
            if key:
                iv = ivs[i] if i < len(ivs) else b"\x00" * 16
                data = unpad_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(data))
            out.write(data)
