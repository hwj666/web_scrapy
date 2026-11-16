"""HLS/m3u8 工具：纯函数，不依赖网络或文件。"""
import re
from urllib.parse import urljoin, urlparse

_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)\s*=\s*("[^"]*"|[^,]*)')
_KEY_TAG_RE = re.compile(r"#EXT-X-KEY:([^\r\n]*)", re.I)
_MEDIA_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", re.I)
_STREAM_RE = re.compile(r"^#EXT-X-STREAM-INF", re.I)
_BANDWIDTH_RE = re.compile(r"BANDWIDTH\s*=\s*(\d+)", re.I)
_INIT_MAP_RE = re.compile(r"^#EXT-X-MAP:", re.I | re.M)

# 一个媒体分片对应的密钥槽位: (key URI, 显式 IV, 显式 IV 十六进制, 媒体序列号)
KeySlot = tuple[str, bytes | None, str, int]


def is_master(text: str | None) -> bool:
    return "#EXT-X-STREAM-INF" in (text or "")


def has_init_segment(text: str | None) -> bool:
    """是否 fMP4（#EXT-X-MAP 初始化段）。当前合成链路只支持 TS 分片。"""
    return bool(_INIT_MAP_RE.search(text or ""))


def media_lines(text: str | None) -> list[str]:
    return [s for s in (ln.strip() for ln in (text or "").splitlines())
            if s and not s.startswith("#")]


def parse_attrs(tag: str) -> dict[str, str]:
    out = {}
    for k, v in _ATTR_RE.findall(tag or ""):
        out[k.upper()] = v.strip().strip('"').strip()
    return out


def variants(text: str | None) -> list[tuple[int, str]]:
    lines = (text or "").splitlines()
    out, i = [], 0
    while i < len(lines):
        if _STREAM_RE.match(lines[i].strip()):
            m = _BANDWIDTH_RE.search(lines[i])
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                out.append((int(m.group(1)) if m else 0, lines[j].strip()))
                i = j
        i += 1
    return out


def best_variant(text: str | None) -> str | None:
    vs = variants(text)
    if not vs:
        return None
    return max(vs, key=lambda x: x[0])[1]


def key_tags(text: str | None) -> list[dict[str, str]]:
    return [parse_attrs(m.group(1)) for m in _KEY_TAG_RE.finditer(text or "")]


def _iv_bytes(iv_hex: str) -> bytes | None:
    s = (iv_hex or "").strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        b = bytes.fromhex(s)
    except ValueError:
        return None
    return b if len(b) == 16 else None


def key_info(text: str | None) -> tuple[str, bytes | None, str] | None:
    tags = [t for t in key_tags(text) if t.get("METHOD", "").upper() == "AES-128"]
    if not tags:
        return None
    uris = {t.get("URI", "") for t in tags if t.get("URI")}
    ivs = {t.get("IV", "") for t in tags}
    if len(uris) > 1 or len(ivs) > 1:
        raise ValueError("播放列表使用密钥轮换，暂不支持")
    uri = next(iter(uris), "")
    if not uri:
        return None
    iv_hex = next(iter(ivs), "")
    if iv_hex.lower().startswith("0x"):
        iv_hex = iv_hex[2:]
    iv = _iv_bytes(iv_hex)
    if iv_hex and iv is None:
        raise ValueError(f"IV 长度异常: {iv_hex[:20]}")
    return uri, iv, iv_hex


def media_sequence(text: str | None) -> int:
    """播放列表起始媒体序列号（没有该标签时为 0）。"""
    m = _MEDIA_SEQ_RE.search(text or "")
    return int(m.group(1)) if m else 0


def key_schedule(text: str | None) -> list[KeySlot]:
    """按媒体分片顺序返回每个分片的 (key URI, IV, IV 十六进制, 媒体序列号)。

    支持 HLS 的三种常见形态：
    - 整列表一个 KEY（IV 显式给出，或按媒体序列号派生）；
    - 密钥轮换（多个不同 URI 的 KEY，逐片生效）；
    - 混合明文/加密（`#EXT-X-KEY:METHOD=NONE` 之后的分片 key URI 为空）。

    没有显式 IV 时按 RFC 8216 用「媒体序列号」作为 IV，由调用方用返回的 seq 派生。
    遇到 SAMPLE-AES 等当前链路不支持的加密方式时直接报错，避免下完才失败。
    """
    seq = media_sequence(text)
    cur_uri = ""
    cur_iv: bytes | None = None
    cur_iv_hex = ""
    out: list[KeySlot] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-KEY:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "").upper()
            if method in ("", "NONE"):
                cur_uri, cur_iv, cur_iv_hex = "", None, ""
            elif method == "AES-128":
                uri = attrs.get("URI", "")
                if not uri:
                    raise ValueError("AES-128 密钥标签缺少 URI")
                keyformat = (attrs.get("KEYFORMAT") or "identity").strip().lower()
                if keyformat not in ("", "identity"):
                    raise ValueError(f"不支持的密钥格式: {keyformat}")
                iv_hex = attrs.get("IV", "")
                iv = _iv_bytes(iv_hex)
                if iv_hex and iv is None:
                    raise ValueError(f"IV 长度异常: {iv_hex[:20]}")
                cur_uri, cur_iv, cur_iv_hex = uri, iv, iv_hex
            else:
                raise ValueError(f"不支持的加密方式: {method}")
            continue
        if line.startswith("#"):
            continue
        out.append((cur_uri, cur_iv, cur_iv_hex, seq))
        seq += 1
    return out


def abs_segment(u: str, m3u8_url: str, inherit_query: bool = False) -> str:
    """分片/密钥/子播放列表地址绝对化。

    用 urljoin 统一处理相对地址、根相对地址和协议相对地址（`//cdn/x`）。
    默认**不**继承播放列表的查询串：HLS 标准就是按 RFC 3986 解析，真实播放器
    （hls.js / ffmpeg）也不会继承；很多 CDN 的 `?sign=` 是绑定路径的签名，
    拼到分片上会被直接断开连接（表现为 ConnectionReset / fetch failed）。
    只有确认站点需要 token 透传时才传 inherit_query=True，或在取数失败后兜底重试。
    """
    u = (u or "").strip()
    if not u:
        return u
    if u.startswith(("http://", "https://")):
        return u
    url = urljoin(m3u8_url, u)
    if inherit_query and "?" not in u and "?" not in url:
        q = urlparse(m3u8_url).query
        if q:
            url = f"{url}?{q}"
    return url


async def resolve_playlist(fetch, m3u8_url: str) -> tuple[str, str]:
    """多码率主列表自动下钻，返回 (最终媒体列表文本, 最终URL)。"""
    for _ in range(3):
        text = await fetch(m3u8_url)
        if not is_master(text):
            return text, m3u8_url
        nxt = best_variant(text)
        if not nxt:
            raise RuntimeError("empty master m3u8")
        m3u8_url = abs_segment(nxt, m3u8_url)
    raise RuntimeError("m3u8 多码率嵌套过深")
