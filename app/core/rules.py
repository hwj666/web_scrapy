"""规则引擎：把散落在各模块里的正则/关键词/阈值统一收敛到 rules/*.yaml。

运行期由 `RuleManager` 一次性加载并编译，解析器与 Job 通过它取得已编译规则，
不再各自硬编码。新增/调整规则只改 YAML，不改代码；新站点则写一个新解析器文件。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml


def _rx(fragments: Iterable[str] | None, flags: int = 0) -> re.Pattern[str] | None:
    """把一组正则片段拼成一个组合正则；空列表返回 None。"""
    frags = [f for f in (fragments or []) if f]
    return re.compile("|".join(frags), flags) if frags else None


def _one(pattern: str | None, flags: int = 0) -> re.Pattern[str] | None:
    """编译单个正则，空串/None 返回 None。"""
    return re.compile(pattern, flags) if pattern else None


@dataclass
class TitleRules:
    """标题清洗规则（title_rules.yaml → title）。"""

    separators: list[str] = field(default_factory=list)
    prefix_patterns: list[str] = field(default_factory=list)
    suffix_patterns: list[str] = field(default_factory=list)
    wrap_chars: str = ""
    site_words: list[str] = field(default_factory=list)
    episode_patterns: list[str] = field(default_factory=list)
    generic_titles: list[str] = field(default_factory=list)

    def compiled(self) -> dict[str, Any]:
        return {
            "sep_re": _rx(self.separators),
            "prefix_re": _rx(self.prefix_patterns),
            # 旧实现对后缀/站名做了忽略大小写（后缀里的 "HD"、站名里的 "app/vip"）
            "suffix_re": _rx(self.suffix_patterns, re.I),
            "wrap": self.wrap_chars,
            "site_re": _rx([re.escape(w) for w in self.site_words], re.I),
            "episode_re": _rx(self.episode_patterns, re.I),
            "generic": set(self.generic_titles),
        }


@dataclass
class LinkRules:
    """列表页链接规则（link_rules.yaml → link）。"""

    exclude_re: re.Pattern[str] | None = None
    ext_re: re.Pattern[str] | None = None
    ad_re: re.Pattern[str] | None = None
    play_hint_re: re.Pattern[str] | None = None
    id_re: re.Pattern[str] | None = None
    div_to_re: re.Pattern[str] | None = None
    title_block_keys: list[str] = field(default_factory=list)


@dataclass
class ResolverRules:
    """播放页解析规则（pipeline_rules.yaml → resolver）。"""

    m3u8_re: re.Pattern[str] | None = None
    crypt_key_re: re.Pattern[str] | None = None
    aes_call_re: re.Pattern[str] | None = None
    blob_re: re.Pattern[str] | None = None
    vod_name_re: re.Pattern[str] | None = None


@dataclass
class PipelineRules:
    """流水线阈值与非重试/发布页/跳转信号（pipeline_rules.yaml → pipeline）。"""

    streak_limit: int = 5
    reprobe_cooldown: int = 300
    disk_min_gb: int = 5
    # 列表页同时抓取/解析的页数（1-6）。顺序处理结果，不会打乱视频编号。
    list_concurrency: int = 3
    non_retryable: tuple[str, ...] = ()
    shell_hints: tuple[str, ...] = ()
    redirect_hint_re: re.Pattern[str] | None = None
    # 发布页/换域名提示页上的按钮文字（按顺序尝试）
    publish_click_texts: tuple[str, ...] = ()
    # 从页面文字里提取新域名（正则里必须有一个分组是 host）
    new_domain_re: re.Pattern[str] | None = None
    # 换域名后列表路径可能一起改版：这些模板用于 404 时探测可用路径
    list_url_patterns: tuple[str, ...] = ()


@dataclass
class BrowserRules:
    """浏览器兜底规则（pipeline_rules.yaml → browser）。"""

    trigger_selectors: list[str] = field(default_factory=list)
    title_selectors: list[str] = field(default_factory=list)
    card_title_selector: str = "[class*=title]"
    card_selectors: list[str] = field(default_factory=list)
    card_click_max: int = 40
    # SPA 翻页按钮选择器（站点会剥掉 URL 里的 page 参数时用）
    next_page_selector: str = ""
    # SPA 翻页按钮文本候选（按顺序尝试，命中即点）：
    # zd3xy 用「下一页」, enw 用「加载更多」
    next_page_texts: list[str] = field(default_factory=lambda: ["下一页", "加载更多", "更多", "Load More"])
    # 加密 SPA（如 zd3xy / enw）：列表数据来自加密 API，DOM 拿不到，
    # 开启后用 TextDecoder 钩子拦截浏览器解密后的 JSON，再点翻页按钮加载更多。
    use_api_capture: bool = False
    # 详情页 URL 模板，用视频 id 填充，如 "/video/play?id={id}"
    detail_url_template: str = ""


class RuleManager:
    """加载并编译 rules/*.yaml，解析器/Job 唯一取数入口。"""

    def __init__(self, rule_dir: str | None = None) -> None:
        self.rule_dir = rule_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules"
        )
        self.title = TitleRules()
        self.link = LinkRules()
        self.resolver = ResolverRules()
        self.pipeline = PipelineRules()
        self.browser = BrowserRules()
        self._load()

    # ---- 加载 ----
    def _load(self) -> None:
        data = self._load_yaml("title_rules.yaml")
        if data and data.get("title"):
            self.title = TitleRules(**data["title"])

        lk = self._load_yaml("link_rules.yaml") or {}
        self.link = self._build_link(lk.get("link") or {})

        pl = self._load_yaml("pipeline_rules.yaml") or {}
        self._build_pipeline(pl)

    def _load_yaml(self, name: str) -> dict:
        path = os.path.join(self.rule_dir, name)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _build_link(cfg: dict) -> LinkRules:
        exts = cfg.get("exclude_exts") or []
        ext_re = None
        if exts:
            ext_re = re.compile(r"\.(" + "|".join(exts) + r")([?#]|$)", re.I)
        return LinkRules(
            exclude_re=_rx(cfg.get("exclude_keywords"), re.I),
            ext_re=ext_re,
            ad_re=_rx(cfg.get("ad_patterns"), re.I),
            play_hint_re=_rx(cfg.get("play_hint_patterns"), re.I),
            id_re=_rx(cfg.get("id_patterns"), re.I),
            div_to_re=_one(cfg.get("div_to_pattern"), re.S | re.I),
            title_block_keys=cfg.get("title_block_keys") or [],
        )

    def _build_pipeline(self, data: dict) -> None:
        p = data.get("pipeline") or {}
        self.pipeline = PipelineRules(
            streak_limit=int(p.get("streak_limit", 5)),
            reprobe_cooldown=int(p.get("reprobe_cooldown", 300)),
            disk_min_gb=int(p.get("disk_min_gb", 5)),
            list_concurrency=max(1, min(6, int(p.get("list_concurrency", 3)))),
            non_retryable=tuple(p.get("non_retryable") or ()),
            shell_hints=tuple(p.get("shell_hints") or ()),
            redirect_hint_re=_one(p.get("redirect_hint"), re.I),
            publish_click_texts=tuple(
                p.get("publish_click_texts") or ["進入", "进入", "继续访问", "点击进入", "新域名"]
            ),
            new_domain_re=_rx(p.get("new_domain_patterns") or [], re.I),
            list_url_patterns=tuple(p.get("list_url_patterns") or ()),
        )

        r = data.get("resolver") or {}
        names = r.get("crypt_key_names") or ["crypt_key", "encryption_key"]
        crypt_key_re = re.compile(
            r'<meta[^>]+name="(?:' + "|".join(re.escape(n) for n in names) + r')"[^>]+content="([^"]+)"', re.I
        )
        self.resolver = ResolverRules(
            m3u8_re=_one(r.get("m3u8_pattern"), re.I),
            crypt_key_re=crypt_key_re,
            aes_call_re=_one(r.get("aes_call_pattern"), re.I),
            blob_re=_one(r.get("blob_pattern")),
            vod_name_re=_one(r.get("vod_name_pattern")),
        )

        b = data.get("browser") or {}
        self.browser = BrowserRules(
            trigger_selectors=b.get("trigger_selectors") or [],
            title_selectors=b.get("title_selectors") or [],
            card_title_selector=b.get("card_title_selector") or "[class*=title]",
            card_selectors=b.get("card_selectors") or [],
            card_click_max=int(b.get("card_click_max") or 40),
            next_page_selector=b.get("next_page_selector") or "",
            next_page_texts=b.get("next_page_texts") or ["下一页", "加载更多", "更多", "Load More"],
            use_api_capture=bool(b.get("use_api_capture") or False),
            detail_url_template=b.get("detail_url_template") or "",
        )
