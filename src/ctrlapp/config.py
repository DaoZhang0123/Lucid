"""配置加载：读取 config.toml，命令行可覆盖。"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass
class ProxyConfig:
    base_url: str = "http://localhost:4000"
    model: str = "claude-opus-4.6"
    api_key: str = ""


@dataclass
class LLMConfig:
    model: str = "claude-opus-4-5"
    max_tokens: int = 4096
    max_steps: int = 40
    enable_prompt_cache: bool = True
    # 发往模型的对话历史里最多保留多少张截图（含起始图）。
    # 超出后旧图被替换成一段占位文字，避免 413 Request Entity Too Large。
    keep_recent_screenshots: int = 4
    proxy: ProxyConfig = field(default_factory=ProxyConfig)


@dataclass
class ScreenshotConfig:
    l1_fullscreen_interval: float = 60.0
    l2_activewindow_interval: float = 8.0
    l3_cursor_interval: float = 0.0
    l3_radius_px: int = 100
    l1_max_long_edge: int = 1568
    l2_max_long_edge: int = 1568
    l3_max_long_edge: int = 0
    keep_recent_l1: int = 2
    keep_recent_l2: int = 4
    keep_recent_l3: int = 6
    skip_if_similarity_above: float = 0.985


@dataclass
class SafetyConfig:
    hitl_keywords: list[str] = field(default_factory=list)
    emergency_hotkey: str = "ctrl+alt+esc"
    autonomy: str = "confirm_critical"
    # 安全兑底（架构层 + 行为层）
    # 架构层：点击类动作后额外拓 L3 鼠标周边高清取证图，让模型看清落点。
    # 代价：每次点击后 prompt 多一张小图（带宽上变慢）。
    verify_click_with_l3: bool = True
    # 行为层：在保存/打开对话框里检测到“在左侧 25% 区域点击”时，注入一条
    # “请用文件名框/地址栏 type 路径”的纠正提示，避免模型去左侧导航树逐层点。
    save_dialog_sidebar_guard: bool = True


@dataclass
class InputConfig:
    chinese_input: str = "clipboard"
    action_delay: float = 0.15


@dataclass
class LoggingConfig:
    enabled: bool = True
    dir: str = "logs"
    text_level: str = "INFO"     # DEBUG | INFO | WARNING | ERROR | OFF
    image_level: str = "INFO"    # DEBUG | INFO | WARNING | OFF
    image_format: str = "png"    # png | jpg
    jpg_quality: int = 85
    keep_runs: int = 20


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    input: InputConfig = field(default_factory=InputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _apply(dc: Any, raw: dict[str, Any] | None) -> Any:
    if not raw:
        return dc
    for k, v in raw.items():
        if hasattr(dc, k):
            setattr(dc, k, v)
    return dc


def load_config(path: str | Path | None = None) -> Config:
    """读取 toml；缺失时返回全默认。"""
    cfg = Config()
    if path is None:
        # 默认在工作目录或包根目录找
        for candidate in (Path.cwd() / "config.toml", Path(__file__).resolve().parents[2] / "config.toml"):
            if candidate.is_file():
                path = candidate
                break
    if path is None:
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    llm_raw = dict(raw.get("llm") or {})
    proxy_raw = llm_raw.pop("proxy", None)
    _apply(cfg.llm, llm_raw)
    _apply(cfg.llm.proxy, proxy_raw)
    _apply(cfg.screenshot, raw.get("screenshot"))
    _apply(cfg.safety, raw.get("safety"))
    _apply(cfg.input, raw.get("input"))
    _apply(cfg.logging, raw.get("logging"))
    return cfg
