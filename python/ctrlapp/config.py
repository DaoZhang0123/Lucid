"""配置加载：读取 config.toml，命令行可覆盖。"""
from __future__ import annotations

import os
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
    # 任意附加 HTTP 头，会同时透传给 OpenAI SDK 和 smoke-test 客户端。
    # 典型用途：OpenClaw gateway 要求 model="openclaw" 时，靠
    # `x-openclaw-model = "github-copilot/claude-opus-4.7"` 选具体后端模型。
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class AnthropicConfig:
    """Anthropic 原生 API 接入。走 https://api.anthropic.com/v1/messages。"""
    api_key: str = ""             # 留空则读 ANTHROPIC_API_KEY 环境变量
    model: str = "claude-opus-4-5-20250929"
    base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"


@dataclass
class CopilotConfig:
    r"""GitHub Copilot 接入。OAuth device code 拿 GitHub token 后换
    Copilot 临时 token（~30 min），直接打 Copilot 的 chat completions 端点。
    api_key / cached token 保存在 state_file（默认 %LOCALAPPDATA%\dev.ctrlapp\copilot.json），
    不应写入 config.toml。这里只放公开选项。
    """
    model: str = "claude-opus-4-6"
    state_file: str = ""          # 留空则默认 user data dir / copilot.json


@dataclass
class LLMConfig:
    # provider 选择走哪个模型后端。取值：
    #   "proxy"     OpenAI 兼容代理 (LiteLLM / OpenClaw 等，默认)
    #   "anthropic" Anthropic 原生 API
    #   "copilot"   GitHub Copilot OAuth (内置 device-code 登录)
    provider: str = "anthropic"
    model: str = "claude-opus-4-5"
    max_tokens: int = 16384
    max_steps: int = 25
    enable_prompt_cache: bool = True
    # Sampling parameters. Sent to the provider on every request when not None;
    # leave None to use the provider's server-side default (usually
    # temperature=1.0, top_p=1.0). For GUI-agent / tool-use workloads a low
    # temperature is recommended; combining a low temperature with a tight
    # top_p can cause repetition loops, so prefer adjusting only one.
    temperature: float | None = 0.2
    top_p: float | None = 1.0
    # 发往模型的对话历史里最多保留多少张截图（含起始图）。
    # 超出后旧图被替换成一段占位文字，避免 413 Request Entity Too Large。
    keep_recent_screenshots: int = 0
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)


@dataclass
class ScreenshotConfig:
    l1_fullscreen_interval: float = 60.0
    l2_activewindow_interval: float = 8.0
    l3_cursor_interval: float = 0.0
    l3_radius_px: int = 100
    l1_max_long_edge: int = 1568
    l2_max_long_edge: int = 1568
    l3_max_long_edge: int = 0
    # 发送到 LLM 的图像编码：L1/L2 大图默认 JPEG（压缩 ~10x），L3 小图保留 PNG（无损）。
    # 这是为了避免单条请求 body 过大（>1MB）导致 Copilot/openrouter 代理读 body 超时（408）。
    send_jpeg_for_l1_l2: bool = True
    send_jpeg_quality: int = 80
    # 对话历史里每个 level 保留最近 K 张截图（装入发给 LLM 的 prompt 里）。
    # 超出部分会被替换为占位文本（携带本地文件名/路径，需要时可反查）。
    keep_recent_l1: int = 1
    keep_recent_l2: int = 1
    keep_recent_l3: int = 2
    skip_if_similarity_above: float = 0.985
    # ---- R2: launch_app diff → L2 (Docs/screenshot.md §13.3) ----
    # When launch_app actually starts a new instance (method != activate-existing),
    # snapshot L1 before/after and use the diff bbox to crop a tight L2 of the
    # newly-appeared window. Falls back to window_client_rect(hwnd) on failure.
    launch_diff_enabled: bool = True
    # Diff bbox area must cover at least this fraction of L1 to be trusted.
    launch_diff_min_area_ratio: float = 0.05
    # Max wait (ms) for the launched window to become visible+non-iconic.
    launch_wait_max_ms: int = 1500
    # Poll interval while waiting for the window.
    launch_wait_poll_ms: int = 80
    # ---- R3: click pre/post pixel-diff verify (Docs/screenshot.md §13.4) ----
    click_verify_enabled: bool = True
    # Below this fraction of pixels changed near the cursor → "didn't click".
    click_no_change_threshold: float = 0.005
    # How long to wait after the click before sampling post (UI reaction time).
    click_verify_post_sleep_ms: int = 150
    # Radius (in screen px) of the L3 tile sampled before/after the click.
    click_verify_radius_px: int = 100
    # ---- Initial L1: feed it to the LLM as the very first user-message image? ----
    # When False, the run still captures the L1 (used internally as
    # ``last_capture`` for coordinate reverse-mapping and as the pre-image for
    # R2 launch_app diff), and still saves ``step-000-init.png`` to disk, but
    # the LLM only receives the textual task instruction without an attached
    # screenshot. Useful when the user's task explicitly starts with
    # `launch_app(...)` etc., where the desktop snapshot is irrelevant noise
    # that wastes tokens. The model can always request one via
    # `screenshot(level='fullscreen')` when it needs orientation.
    feed_initial_l1_to_llm: bool = False


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
    # 点击前预检：在执行 click 之前到目标 (x,y) 抓一张 L3 小图，与模型决策所
    # 依据的“最近一次截图”同一区域做 dHash 对比；低于相似度阈值即取消本次
    # click，把实时小图回送给模型让它重新看再决定。
    # **2026-05 修订**：R3（点击后 pixel-diff 校验，见 §13.4）落地后默认关闭这条
    # 强制 preview，改为事后判定；保守用户可手动 = true 回到强制 preview。
    verify_click_target_before: bool = False
    verify_click_target_radius_px: int = 60


@dataclass
class InputConfig:
    chinese_input: str = "clipboard"
    action_delay: float = 0.15
    # If True, `\n`/`\t` inside `type` text are split out and sent as Enter/Tab
    # key presses (legacy behaviour). This is harmful for chat apps where Enter
    # sends the message immediately (e.g. WeChat, Telegram, Slack), turning a
    # single multi-line message into N separate messages. Default False:
    # paste the whole string verbatim via clipboard and let the target widget
    # interpret newlines as soft breaks.
    type_split_newlines: bool = False


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
class MemoryConfig:
    """长期记忆 memory.md 的配置。"""
    enabled: bool = True
    path: str = "memory.md"            # 相对路径会落到 LOCALAPPDATA/dev.ctrlapp/
    max_entries: int = 200             # 超过则丢最早
    max_entry_chars: int = 500         # 单条最大字符数
    max_chars: int = 8000              # 注入 prompt 时的总裁剪上限
    heartbeat_interval_sec: int = 0    # 0=禁用心跳反思（占位）


@dataclass
class ToolsConfig:
    """操作技巧的配置。
    - ``path``        全局 tools.md（每次任务起手注入到 system prompt 的 tips）。
    - ``apps_dir``    per-app 资源根目录 ``apps/<slug>/``，按需加载；目前用于
                       存 ``tips.md``，将来同一目录下可加 ``regions.json`` /
                       ``launcher.json`` 等。新增 App = 新建一个子文件夹。
    """
    enabled: bool = True
    path: str = "tools.md"
    apps_dir: str = "apps"
    max_entries: int = 300
    max_entry_chars: int = 500
    max_chars: int = 12000


@dataclass
class LaunchersConfig:
    """`launch_app` meta tool 的配置。"""
    enabled: bool = True
    # `<user data>/launchers.json` 持久化用户自定义 / 学习到的 launcher entries。
    path: str = "launchers.json"
    # 任务起手时把可用 launcher 的紧凑摘要注入 system prompt（每条 ~1 行）。
    inject_catalog_in_system_prompt: bool = True


@dataclass
class RegionsConfig:
    """App 区域化坐标库（initialization-time region calibration）配置。"""
    enabled: bool = True
    # `<user data>/regions/<app-slug>.json` 每个 App 一份。
    dir: str = "regions"


@dataclass
class IconMemoryConfig:
    """图标记忆 icons/ 的配置。每个图标 = PNG 文件 + 标签描述，
    任务起手时拼成一张合集图注入 prompt，让模型能识别小图标。"""
    enabled: bool = True
    path: str = "icons"           # 相对路径 → LOCALAPPDATA/dev.ctrlapp/icons/
    max_icons: int = 60           # 超过则丢最早的
    max_label_chars: int = 40
    max_desc_chars: int = 200
    tile_size: int = 96           # 单图标在合集图里的最大渲染边长（像素）
    atlas_cols: int = 4           # 合集图每行多少个图标


@dataclass
class ContextConfig:
    """Context Manager: image recompression + adaptive history summarisation."""
    # --- image recompression (no LLM, pure code) ---
    # Old screenshots (those outside the per-level / global keep window) are
    # recompressed to JPEG at this quality and downscaled to this max long edge,
    # instead of being dropped to a text placeholder. Set image_max_long_edge=0
    # to fall back to text-placeholder behaviour (legacy).
    image_recompress_enabled: bool = True
    image_recompress_quality: int = 35
    image_recompress_max_long_edge: int = 720
    # --- adaptive summarisation (uses LLM) ---
    auto_compress_enabled: bool = True
    # Target ratio of model context window before triggering summarisation.
    target_ratio: float = 0.7
    # Approximate model context window in tokens. Conservative default for Claude.
    model_context_tokens: int = 200_000
    # When summarising, keep this many most-recent non-prelude messages verbatim.
    keep_recent_messages: int = 12
    # Optional cheaper model for summarisation. Empty = reuse the main client.
    summary_model: str = ""
    # Max tokens the summary itself may consume.
    summary_max_tokens: int = 1500


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    input: InputConfig = field(default_factory=InputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    icons: IconMemoryConfig = field(default_factory=IconMemoryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    launchers: LaunchersConfig = field(default_factory=LaunchersConfig)
    regions: RegionsConfig = field(default_factory=RegionsConfig)


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
        env_path = os.environ.get("CTRLAPP_CONFIG")
        if env_path:
            path = env_path
    if path is None:
        # 默认在工作目录或包根目录找
        for candidate in (Path.cwd() / "config.toml", Path(__file__).resolve().parents[2] / "config.toml"):
            if candidate.is_file():
                path = candidate
                break
    if path is None:
        return cfg
    # 容忍 BOM（部分 PowerShell / 编辑器写入会带 UTF-8 BOM，tomllib 会拒绝）
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    raw = tomllib.loads(data.decode("utf-8"))
    llm_raw = dict(raw.get("llm") or {})
    proxy_raw = llm_raw.pop("proxy", None)
    anthropic_raw = llm_raw.pop("anthropic", None)
    copilot_raw = llm_raw.pop("copilot", None)
    _apply(cfg.llm, llm_raw)
    _apply(cfg.llm.proxy, proxy_raw)
    _apply(cfg.llm.anthropic, anthropic_raw)
    _apply(cfg.llm.copilot, copilot_raw)
    _apply(cfg.screenshot, raw.get("screenshot"))
    _apply(cfg.safety, raw.get("safety"))
    _apply(cfg.input, raw.get("input"))
    _apply(cfg.logging, raw.get("logging"))
    _apply(cfg.memory, raw.get("memory"))
    _apply(cfg.tools, raw.get("tools"))
    _apply(cfg.icons, raw.get("icons"))
    _apply(cfg.context, raw.get("context"))
    _apply(cfg.launchers, raw.get("launchers"))
    _apply(cfg.regions, raw.get("regions"))
    return cfg
