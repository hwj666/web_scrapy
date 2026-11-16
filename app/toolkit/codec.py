"""编解码工具（纯标准库）。"""
import re


def unpad_pkcs7(b: bytes) -> bytes:
    if b and 1 <= b[-1] <= 16 and b[-b[-1]:] == bytes([b[-1]]) * b[-1]:
        return b[:-b[-1]]
    return b


# Content-Type 里的 charset
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)


def decode_page(text: str) -> str:
    """还原 document.write(decodeURIComponent(...)) 型页面并剥掉 <script>。"""
    m = re.search(r'document\.write\(decodeURIComponent\("([^"]+)"\)\)', text or "", re.S)
    if m:
        from urllib.parse import unquote
        text = unquote(m.group(1))
    return re.sub(r"<script>.*?</script>", "", text, flags=re.S)


def decode_body(content: bytes, content_type: str = "", fallback: tuple[str, ...] = ()) -> str:
    """把 HTTP 响应体解码成文本。
    优先按响应头 charset → UTF-8 → GB18030 顺序尝试。"""
    raw = content or b""
    cands: list[str] = []
    m = _CHARSET_RE.search(content_type or "")
    if m and m.group(1).lower() not in ("iso-8859-1", "latin-1", "us-ascii", "ascii"):
        cands.append(m.group(1))
    cands.extend(["utf-8", "gb18030", *fallback])
    for enc in cands:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")
