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


# Localized defaults for visual_notify.auto_chat_instruction, keyed by the
# Lucid UI locale (cfg.ui.locale). The dataclass field below uses the zh-CN
# version as a static fallback (legacy: that was the single shipping default);
# sidecar.py picks the locale-appropriate one at runtime via
# ``default_auto_chat_instruction(cfg.ui.locale)`` whenever the stored value
# still matches ANY of these defaults (= user never customised it).
_DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE: dict[str, str] = {
    "zh-CN": (
        "请打开对应的聊天工具，逐条阅读所有最近未读消息：对每条都要结合上下文给出自然、完整的回复；"
        "若消息中包含可执行的任务，请先把任务实际完成（含必要的操作和结果），再把结果回复给对方。"
        "所有消息和任务都处理完毕后才算本轮结束（监听会由调度器自动继续，不需要你留在循环里）。"
    ),
    "en": (
        "Open the corresponding chat client and read every recent unread message one by one: "
        "for each one, write a natural, complete reply that fits the context; if a message contains "
        "an actionable task, actually carry the task out first (including the necessary steps and "
        "results) and then reply with the outcome. The turn only ends after every message and task "
        "has been handled (the scheduler will keep listening automatically — you don't need to stay "
        "in a loop yourself)."
    ),
    "fr-FR": (
        "Ouvrez le client de chat correspondant et lisez un par un tous les messages récents non lus : "
        "pour chacun, rédigez une réponse naturelle et complète adaptée au contexte ; si un message "
        "contient une tâche exécutable, effectuez d'abord réellement la tâche (étapes et résultats "
        "nécessaires inclus), puis répondez avec le résultat. Le tour ne se termine qu'une fois tous "
        "les messages et toutes les tâches traités (le planificateur continuera l'écoute "
        "automatiquement — vous n'avez pas besoin de rester dans une boucle)."
    ),
}
_DEFAULT_AUTO_CHAT_INSTRUCTION = _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE["zh-CN"]


def default_auto_chat_instruction(locale: str | None) -> str:
    """Return the auto-chat default instruction for ``locale`` (Lucid UI locale
    like ``en`` / ``zh-CN`` / ``fr-FR``). Unknown locales fall back to English."""
    tag = (locale or "").strip()
    if tag in _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE:
        return _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE[tag]
    primary = tag.split("-")[0].lower()
    alias = {"zh": "zh-CN", "fr": "fr-FR", "en": "en"}.get(primary)
    if alias:
        return _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE[alias]
    return _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE["en"]


# Any stored value equal to one of the current localized defaults counts as
# "user hasn't customised" — sidecar then swaps in the locale-appropriate one.
_CURRENT_DEFAULT_AUTO_CHAT_INSTRUCTIONS = frozenset(
    _DEFAULT_AUTO_CHAT_INSTRUCTION_BY_LOCALE.values()
)

# Historical default strings that older builds shipped with. If a user-level
# config.toml stored one of these verbatim (i.e. they never customised it),
# silently upgrade to the current default at load time so existing installs
# get prompt-policy fixes without forcing the user to re-edit config.toml.
_STALE_AUTO_CHAT_INSTRUCTIONS = frozenset({
    "请打开对应的聊天工具查看最近未读，结合上下文给出简短自然的回复后返回继续监听。",
})


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
    api_key / cached token 保存在 state_file（默认 ~/.lucid/copilot.json），
    不应写入 config.toml。这里只放公开选项。
    """
    model: str = "claude-opus-4.6"
    state_file: str = ""          # 留空则默认 user data dir / copilot.json


@dataclass
class OpenAIConfig:
    """OpenAI 直连。https://api.openai.com/v1 的 chat completions 端点。"""
    api_key: str = ""             # 留空则读 OPENAI_API_KEY 环境变量
    model: str = "gpt-5"
    base_url: str = "https://api.openai.com/v1"


@dataclass
class GeminiConfig:
    """Google Gemini 直连，走其 OpenAI 兼容端点
    （https://generativelanguage.googleapis.com/v1beta/openai/）。
    """
    api_key: str = ""             # 留空则读 GEMINI_API_KEY / GOOGLE_API_KEY 环境变量
    model: str = "gemini-2.5-pro"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass
class LLMConfig:
    # provider 选择走哪个模型后端。取值：
    #   "proxy"     OpenAI 兼容代理 (LiteLLM / OpenClaw 等，默认)
    #   "anthropic" Anthropic 原生 API
    #   "copilot"   GitHub Copilot OAuth (内置 device-code 登录)
    #   "openai"    OpenAI 直连 (api.openai.com/v1)
    #   "gemini"    Google Gemini 直连 (generativelanguage.googleapis.com/v1beta/openai)
    provider: str = "anthropic"
    model: str = "claude-opus-4-5"
    max_tokens: int = 16384
    enable_prompt_cache: bool = True
    # Sampling parameters. Sent to the provider on every request when not None;
    # leave None to use the provider's server-side default (usually
    # temperature=1.0, top_p=1.0). For GUI-agent / tool-use workloads a low
    # temperature is recommended; combining a low temperature with a tight
    # top_p can cause repetition loops, so prefer adjusting only one.
    temperature: float | None = 0.2
    top_p: float | None = 1.0
    # 发往模型的对话历史里最多保留多少张截图（不计 L0 icon_atlas，后者总是保留）。
    # 这是一个 **硬天花板**：即使按 level / 按 app 的保留规则会保留更多，超出
    # 这个数量的旧图也会被强制压缩/丢弃为占位文字。设为 0 关闭硬天花板（只按
    # per-level 规则）。默认 4 = 1 L1 + 2 L2 + 1 L3 的稳态，可吸收偶尔的瞬时
    # 峰值同时给 Copilot/Opus content filter 留出余量。见 thread-20260520-132241
    # O2 — content-filter 在累积 vision payload 上误判。
    keep_recent_screenshots: int = 4
    # 单次 chat() 调用的应用层 wall-clock 兜底。SDK 自己的 timeout 在连接 hang
    # 或 chunked 上游静默时不一定真触发，daemon 线程到点视为 connection-style
    # 错误进入下一轮退避重试。设大于 _CHAT_TIMEOUT_SEC (90s) 即可。
    chat_wall_timeout_sec: float = 180.0
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    gemini: GeminiConfig = field(default_factory=GeminiConfig)


@dataclass
class ScreenshotConfig:
    l1_max_long_edge: int = 1568
    l2_max_long_edge: int = 1568
    l3_max_long_edge: int = 0
    # 发送到 LLM 的图像编码：L1/L2 大图默认 JPEG（压缩 ~10x），L3 小图保留 PNG（无损）。
    # 这是为了避免单条请求 body 过大（>1MB）导致 Copilot/openrouter 代理读 body 超时（408）。
    send_jpeg_for_l1_l2: bool = True
    send_jpeg_quality: int = 88
    # 对话历史里每个 level 保留最近 K 张截图（装入发给 LLM 的 prompt 里）。
    # 超出部分会被替换为占位文本（携带本地文件名/路径，需要时可反查）。
    keep_recent_l1: int = 1
    keep_recent_l2: int = 2
    # L3 (cursor_local) 默认保留最近 1 张：模型主动调 screenshot(level="cursor_local") 后，
    # 后续步骤可能还要回看刚才那块细节；超出后替换为占位文本，可用 load_local_images 反查。
    keep_recent_l3: int = 1
    # 每个不同的 active app（按最近 launch_app/focus_window 的 slug 区分）
    # 期望保留多少张最近的 L2，用于跨 App 任务里旧 App 的 L2 不被新 App 一次
    # 性挤掉。**硬性约束：永远不会让总 L2 数超过 keep_recent_l2**——后者
    # 是 LLM provider payload 的硬上限（图片太多 Copilot/Opus 会直接返回
    # no choices），所以 min_per_l2_app 只在 keep_recent_l2 的额度内尽量
    # 公平分配，不能突破。
    min_per_l2_app: int = 1
    # ---- launch_app L2 capture (Docs/screenshot.md §13.3) ----
    # After launch_app, wait briefly for the new window to materialise then
    # crop an L2 of its client rect (via Win32 GetClientRect on the hwnd).
    # Max wait (ms) for the launched window to become visible+non-iconic.
    launch_wait_max_ms: int = 1500
    # Poll interval while waiting for the window.
    launch_wait_poll_ms: int = 80
    # ---- L3 fixed-tile sizing ----
    # L3 (cursor_local) is a fixed-size square tile centred on the cursor /
    # click target. Earlier versions used UIA "smart sizing" to crop to the
    # smallest element rect, but with gridlines now drawn on every L1/L2/L3
    # (see screen._draw_grid) the model can read screen coordinates straight
    # off the image — a fixed-pixel tile is simpler, works in non-UIA apps
    # (WeChat, Electron, custom-drawn UIs), and gives the model a predictable
    # context window around the click point.
    l3_tile_size_px: int = 200
    # ---- Deprecated UIA smart-sizing fields (kept so existing config.toml
    # files don't fail to parse). No code reads these any more. ----
    l3_smart_padding_px: int = 16
    l3_smart_min_w: int = 160
    l3_smart_min_h: int = 80


@dataclass
class SafetyConfig:
    emergency_hotkey: str = "ctrl+alt+esc"
    # 行为层：在保存/打开对话框里检测到“在左侧 25% 区域点击”时，注入一条
    # “请用文件名框/地址栏 type 路径”的纠正提示，避免模型去左侧导航树逐层点。
    save_dialog_sidebar_guard: bool = True
    # 点击前预检（two-phase click protocol）：在执行 click 之前先到目标 (x,y) 抓
    # 一张 L3 小图回送给模型，模型确认无误后再用同一 (action, coordinate)
    # + ``confirmed=true`` 触发真正点击。loop._maybe_pre_click_verify 在初次
    # 调用时**忽略**模型预先附带的 ``confirmed=true``——只有最近一次 preview
    # 的 (action, screen_xy) 与本次一致时才视为有效确认，防止模型绕过 L3 校
    # 验。
    # **2026-05-17 修订**：用户反馈“所有点击都要确认”——重新默认开启。R3
    # 事后 pixel-diff 校验依旧保留，作为冗余兜底。
    verify_click_target_before: bool = True
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
    # 蟹钳鼠标：Lucid 跑任务期间把系统光标换成一只橙色蟹钳，
    # 提醒用户这是由 Lucid 操纵的。任务结束 / 取消 / 崩溃都会还原。
    crab_cursor: bool = True


@dataclass
class LoggingConfig:
    enabled: bool = True
    dir: str = "logs"
    text_level: str = "INFO"     # DEBUG | INFO | WARNING | ERROR | OFF
    image_level: str = "INFO"    # DEBUG | INFO | WARNING | OFF
    image_format: str = "png"    # png | jpg
    jpg_quality: int = 85
    # 历史轮转：最多保留多少个 thread 目录；超过会从最旧的开始删除。
    # 设为 0 / 负数 表示**不轮转**（永不自动删除任何 thread）。
    keep_runs: int = 0


@dataclass
class MemoryConfig:
    """长期记忆 memory.md 的配置。"""
    enabled: bool = True
    path: str = "memory.md"            # 相对路径会落到 ~/.lucid/
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
    # 任务起手时做一次轻量 LLM 调用，根据 instruction 从已知 app 列表里挑出相关的，
    # 一次性把这些 app 的 tips 注入。这样 agent 启动 wechat 之前就已经看到过 wechat tips，
    # 不会先在 file explorer 里乱点。设为 False 则完全按需（仅在 launch_app/load_app_tips 时加载）。
    plan_app_tips: bool = True
    # 上限：再相关也最多预加载这么多个 app（防止 LLM 把"全选了"）。
    plan_app_tips_max: int = 5


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
class WebReadConfig:
    """`read_webpage` meta 工具配置。两个后端：headless dump（任意 URL，无登录态）
    和 CDP 读取用户现有活动 tab（需要浏览器以 ``--remote-debugging-port=N`` 启动）。"""
    enabled: bool = True
    cdp_port: int = 9222
    # Headless 超时（秒），设多了是为了等重 JS 页面渲染完。
    headless_timeout_s: float = 25.0
    # html_to_text 后的输出上限。默认 8000 字符 ≈ 2k tokens，比一张 L2 截图便宜。
    default_max_chars: int = 8000



@dataclass
class FileIOConfig:
    """`read_file` / `write_file` meta tool config. Bypasses the cmd-screenshot
    round-trip when the agent just needs to read or append small text files.
    The agent already has full keyboard/mouse control — direct file IO doesn't
    expand the attack surface, but we still cap sizes to protect the request body.
    """
    enabled: bool = True
    # Read: hard cap on bytes returned in a single read_file call. Anything
    # larger returns the head + tail with a "truncated" notice.
    read_max_bytes: int = 65_536
    # Read: refuse outright if the file is bigger than this (bytes). Prevents
    # accidentally tailing a 2 GB log.
    read_refuse_bytes: int = 5 * 1024 * 1024
    # Write: hard cap on bytes per write_file call. CJK text is ~3 bytes/char.
    write_max_bytes: int = 256 * 1024


@dataclass
class ShellConfig:
    """`run_shell` meta tool config. Spawns cmd.exe / powershell.exe via
    subprocess (NO console window), captures stdout+stderr, returns text +
    exit code. Beats `launch_app('cmd') → type → screenshot → OCR` for any
    task whose output is plain text (read a file, list a dir, query a process)."""
    enabled: bool = True
    # Default shell when the model omits the `shell` parameter.
    # 'cmd' = cmd.exe /c, 'powershell' = powershell.exe -NoProfile -Command, 'pwsh' = PowerShell 7
    default_shell: str = "powershell"
    # Hard timeout per command (seconds). Long-running stuff (servers, builds)
    # should NOT use this tool — open a real terminal instead.
    timeout_s: float = 20.0
    # Cap on captured stdout+stderr returned to the model (chars). Larger
    # output is truncated head+tail with a notice.
    max_output_chars: int = 16_000


@dataclass
class VisualNotifyConfig:
    """任务栏视觉通知两步法配置：先 diff，再按需调用 LLM 确认。"""
    enabled: bool = False
    poll_interval_sec: float = 2.0
    strip_height_px: int = 120
    auto_detect_taskbar_height: bool = True  # 自动检测系统任务栏高度，覆盖 strip_height_px
    strip_center_width_ratio: float = 0.50
    # 如果 strip_left_skip_px / strip_right_skip_px 任一为正，则改用
    # 跳过像素方式裁剪 strip（screen[left_skip : screen_w - right_skip]），
    # 适合宽屏：避免居中比例把右侧系统托盘 / 通知中心切掉。
    # 左侧默认跳过 100 px，覆盖 Win11 小组件（天气，频繁变化会触发恒定 diff）；
    # 右侧默认跳过 120 px，覆盖时钟（每分钟跳秒同样恒定 diff）。
    strip_left_skip_px: int = 100
    strip_right_skip_px: int = 100
    diff_method: str = "dhash"         # dhash | pixel
    diff_threshold: float = 8.0
    # Step1 横向分段：基于像素差分在 x 轴投影自动切分候选区域，
    # 再对每段计算 diff 分数并聚合（默认取最大），避免整条任务栏被时钟/托盘噪声干扰。
    x_projection_enabled: bool = True
    x_projection_pixel_threshold: int = 20
    x_projection_active_ratio: float = 0.08
    x_projection_min_segment_width_px: int = 20
    x_projection_pad_px: int = 4
    x_projection_merge_gap_px: int = 4
    x_projection_max_segments: int = 10
    x_projection_score_agg: str = "max"  # max | sum_top2
    llm_confirm_enabled: bool = True
    llm_confirm_on_diff_only: bool = True
    llm_confirm_cooldown_sec: float = 5.0
    llm_confirm_max_tokens: int = 300
    # LLM 拒绝后，记住被拒绝的 x_projection 段落多久（秒）。
    # 在此窗口内，若新的 diff 段全部落在最近被拒区域里（±20 px），
    # 直接跳过 LLM 调用（避免 hover/聚焦/运行下划线等同一图标反复噪声触发）。
    rejected_segment_cooldown_sec: float = 30.0
    rejected_segment_overlap_pad_px: int = 20
    save_screenshots: bool = True
    recent_screenshot_keep: int = 100
    key_screenshot_keep: int = 200
    # 检测到新消息后，是否自动发起一轮任务（查看消息并回复）。
    auto_chat_enabled: bool = False
    auto_chat_instruction: str = _DEFAULT_AUTO_CHAT_INSTRUCTION


@dataclass
class TaskbarUiaConfig:
    """任务栏通知 UIA 通道配置。

    UIA 通道是事件驱动 + 零 LLM 成本，覆盖了大部分应用（Teams / Outlook /
    Slack / 微信 等）。它和 VisualNotifyConfig 是并行通道：UIA 优先；UIA 命中
    会自动给视觉通道一个抑制窗口，避免重复触发昂贵的 step-2 确认。

    哪些 App 走 UIA / 走视觉 / 都走，可由 doze 写到
    ``~/.lucid/taskbar_sources.json``（见 :mod:`lucid.taskbar_sources`），这里
    的配置只控制 UIA 通道本身是否启用 + 全局节流参数。
    """
    enabled: bool = True
    # 单个 App 在多少秒内的重复 UIA 触发只算一次（仅影响 emit，不影响 raw 日志）
    per_app_emit_cooldown_sec: float = 8.0
    # UIA 命中后，让视觉通道抑制 step-2 LLM 调用多少秒
    visual_suppress_after_uia_sec: float = 20.0
    # v2: 周期性 UIA 全树快照间隔（秒）。这是 Win11 22H2+ XAML 任务栏上
    # **实际能拿到通知的主路径**——UIA PropertyChanged 事件在该平台上对
    # XAML 任务栏按钮不 dispatch（COM handler 挂上但永不回调），但属性
    # 值本身是正确的，所以走轮询。500ms 与 packaging/poc_taskbar_uia_wechat.py
    # 一致：walk ~十几个按钮 + 读两个属性是毫秒级 COM 调用，单核占用<1%；
    # 间隔再短意义不大（auto-reply 不需要 100ms 级响应），再长会漏掉
    # WeChat 的 ~1s flash 或 Teams 的更短 flash。sweep 内做边沿检测 + 复用
    # per_app_emit_cooldown_sec 去重；0 = 关闭轮询（仅靠不可靠的事件流）。
    snapshot_sweep_interval_sec: float = 0.5


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
class DozeConfig:
    """Doze (idle-time) reflection learning. See ``Docs/doze.md``.

    When the sidecar has been idle for ``idle_threshold_sec`` (no running task,
    empty queue, no recent user RPC), a low-priority background worker scans
    ``threads/`` for unprocessed runs and asks the LLM to extract reusable
    ``learn_tip`` / ``remember`` calls. Default off — opt in via /settings.
    """
    enabled: bool = False
    # When to wake.
    idle_threshold_sec: int = 300
    tick_interval_sec: int = 60
    # Per-pass limits.
    max_threads_per_pass: int = 1
    max_rounds_per_thread: int = 2
    max_tool_calls_per_pass: int = 6
    max_event_text_chars: int = 600
    max_tips_digest_lines: int = 30
    max_memory_digest_lines: int = 30
    # LLM reply ceiling (the reflector replies one short sentence + tool_calls).
    max_tokens: int = 1500
    # Persistence (relative to <user data> dir).
    processed_path: str = "logs/doze_processed.json"
    log_path: str = "logs/doze.log"
    # Bump when prompt format changes — past threads will be re-learned.
    prompt_version: int = 1
    # Taskbar visual learning: each doze pass also drains a small batch of
    # confirmed/rejected taskbar_notify events (with their focus_crop images)
    # and asks the LLM to write per-app `[taskbar_visual]` tips. The Step-2
    # confirm prompt later injects these tips so the same false positive
    # doesn't keep happening.
    taskbar_learn_enabled: bool = True
    taskbar_learn_max_events_per_pass: int = 6
    taskbar_learn_max_tokens: int = 500


@dataclass
class VoiceConfig:
    """Voice input (push-to-talk) — local ASR + overlay window. See ``Docs/voice-input.md``."""
    enabled: bool = False
    # ---- engine ----
    engine: str = "faster-whisper"   # faster-whisper | sherpa-onnx | vosk
    model_size: str = "tiny"         # tiny | base | small | medium | large-v3 | distil-small.en | distil-large-v3
    language: str = "auto"           # ISO 639-1 or "auto"
    compute_type: str = "int8"       # int8 | int8_float16 | float16 | float32
    device: str = "cpu"              # cpu | cuda
    cpu_threads: int = 0             # 0 = let CTranslate2 pick (== os.cpu_count)
    vad_filter: bool = True
    beam_size: int = 5
    max_seconds: int = 30            # recording hard cap (frontend enforces)
    # ---- trigger (a11y-first single-key long-press) ----
    hotkey: str = "Space"            # any single key or combo
    hold_threshold_ms: int = 5000    # 0 = classic PTT (record on press)
    stop_mode: str = "release"      # release | tap_again | auto_silence
    start_feedback: str = "beep"     # beep | vibrate-tray | silent
    focus_aware: bool = True         # don't steal Space when an editable is focused
    # ---- text routing ----
    mode: str = "auto"               # auto (LLM intent dispatch) | thread_new (always start a task) | dictation_append (always insert into focused input)
    auto_send: bool = False          # true = commit immediately after the dwell window; false = wait for the user to tap ✓ in the overlay
    # ---- overlay window ----
    overlay_position: str = "top-center"  # only top-center supported for now
    overlay_y_offset_px: int = 48
    overlay_screen: str = "cursor"   # cursor | primary | active-window
    # ---- misc ----
    keep_audio: bool = False         # keep ~/.lucid/voice/recording-*.webm for debugging
    hf_endpoint: str = ""            # e.g. https://hf-mirror.com for users in CN
    # ---- model lifecycle ----
    idle_unload_sec: int = 1800      # drop model from RAM after this many seconds idle


@dataclass
class SkillsConfig:
    """Reusable parameterised playbooks. See ``Docs/skills.md``."""
    enabled: bool = True
    # If True, also fetch skill JSON from arbitrary http(s) URLs via
    # ``install_skill_url``. Default False because the downloaded steps run
    # through the regular Agent loop and inherit user-level permissions.
    allow_online_install: bool = False
    # Per-skill caps (defence against prompt-bombing / supply-chain abuse).
    max_steps: int = 32
    max_params: int = 16
    max_bytes: int = 32768
    # Inject "## Available skills" block at the end of the system prompt.
    inject_in_system_prompt: bool = True


@dataclass
class UIConfig:
    """User-interface preferences shared between the Tauri app and the sidecar."""
    # ISO locale tag chosen in /settings (e.g. "en", "zh-CN", "fr-FR").
    # "" or "auto" = detect from the OS user locale at runtime.
    # Used by the system prompt so the LLM defaults to replying in the same
    # language the user reads the app in (still adapts when the query itself
    # is clearly in a different language).
    locale: str = "auto"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    input: InputConfig = field(default_factory=InputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    launchers: LaunchersConfig = field(default_factory=LaunchersConfig)
    regions: RegionsConfig = field(default_factory=RegionsConfig)
    webread: WebReadConfig = field(default_factory=WebReadConfig)
    fileio: FileIOConfig = field(default_factory=FileIOConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    visual_notify: VisualNotifyConfig = field(default_factory=VisualNotifyConfig)
    taskbar_uia: TaskbarUiaConfig = field(default_factory=TaskbarUiaConfig)
    doze: DozeConfig = field(default_factory=DozeConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    ui: UIConfig = field(default_factory=UIConfig)


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
        env_path = os.environ.get("LUCID_CONFIG")
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
    openai_raw = llm_raw.pop("openai", None)
    gemini_raw = llm_raw.pop("gemini", None)
    _apply(cfg.llm, llm_raw)
    _apply(cfg.llm.proxy, proxy_raw)
    _apply(cfg.llm.anthropic, anthropic_raw)
    _apply(cfg.llm.copilot, copilot_raw)
    _apply(cfg.llm.openai, openai_raw)
    _apply(cfg.llm.gemini, gemini_raw)
    _apply(cfg.screenshot, raw.get("screenshot"))
    _apply(cfg.safety, raw.get("safety"))
    _apply(cfg.input, raw.get("input"))
    _apply(cfg.logging, raw.get("logging"))
    _apply(cfg.memory, raw.get("memory"))
    _apply(cfg.tools, raw.get("tools"))
    _apply(cfg.context, raw.get("context"))
    _apply(cfg.launchers, raw.get("launchers"))
    _apply(cfg.regions, raw.get("regions"))
    _apply(cfg.webread, raw.get("webread"))
    _apply(cfg.fileio, raw.get("fileio"))
    _apply(cfg.shell, raw.get("shell"))
    _apply(cfg.visual_notify, raw.get("visual_notify"))
    # Auto-upgrade: if the saved auto_chat_instruction is a known-stale
    # historical default, refresh it to the current default so older installs
    # pick up the new prompt policy without manual config editing.
    if cfg.visual_notify.auto_chat_instruction.strip() in _STALE_AUTO_CHAT_INSTRUCTIONS:
        cfg.visual_notify.auto_chat_instruction = _DEFAULT_AUTO_CHAT_INSTRUCTION
    _apply(cfg.taskbar_uia, raw.get("taskbar_uia"))
    _apply(cfg.doze, raw.get("doze"))
    _apply(cfg.voice, raw.get("voice"))
    _apply(cfg.skills, raw.get("skills"))
    _apply(cfg.ui, raw.get("ui"))
    return cfg
