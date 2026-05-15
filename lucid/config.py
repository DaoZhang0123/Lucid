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
    api_key / cached token 保存在 state_file（默认 ~/.lucid/copilot.json），
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
    # 单次 chat() 调用的应用层 wall-clock 兜底。SDK 自己的 timeout 在连接 hang
    # 或 chunked 上游静默时不一定真触发，daemon 线程到点视为 connection-style
    # 错误进入下一轮退避重试。设大于 _CHAT_TIMEOUT_SEC (90s) 即可。
    chat_wall_timeout_sec: float = 180.0
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    copilot: CopilotConfig = field(default_factory=CopilotConfig)


@dataclass
class ScreenshotConfig:
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
    # L3 (cursor_local) 默认保留最近 2 张：模型主动调 screenshot(level="cursor_local") 后，
    # 后续步骤可能还要回看刚才那块细节；超出后替换为占位文本，可用 load_local_images 反查。
    keep_recent_l3: int = 1
    # 每个不同的 active app（按最近 launch_app/focus_window 的 slug 区分）至少保留多少张
    # 最近的 L2，避免跨 App 任务里旧 App 的 L2 被新 App 的 L2 一次性挤掉。
    min_per_l2_app: int = 1
    # ---- launch_app L2 capture (Docs/screenshot.md §13.3) ----
    # After launch_app, wait briefly for the new window to materialise then
    # crop an L2 of its client rect (via Win32 GetClientRect on the hwnd).
    # Max wait (ms) for the launched window to become visible+non-iconic.
    launch_wait_max_ms: int = 1500
    # Poll interval while waiting for the window.
    launch_wait_poll_ms: int = 80
    # ---- L3 smart sizing via UIA (Snipaste-style) ----
    # L3 cursor-local asks Windows UI Automation for the bounding rect of the
    # smallest UI element under the cursor and crops to that. When UIA can't
    # give a usable rect, L3 falls back to L2 (full active window) — see
    # Docs/screenshot.md v2 §2.2.
    l3_smart_padding_px: int = 16          # outset around the UIA rect
    l3_smart_min_w: int = 160              # auto-grow rect to at least this size
    l3_smart_min_h: int = 80


@dataclass
class SafetyConfig:
    emergency_hotkey: str = "ctrl+alt+esc"
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
    save_screenshots: bool = True
    recent_screenshot_keep: int = 100
    key_screenshot_keep: int = 200
    # 检测到新消息后，是否自动发起一轮任务（查看消息并回复）。
    auto_chat_enabled: bool = False
    auto_chat_instruction: str = (
        "请打开对应的聊天工具查看最近未读，结合上下文给出简短自然的回复后返回继续监听。"
    )


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
    stop_mode: str = "tap_again"     # release | tap_again | auto_silence
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
    _apply(cfg.context, raw.get("context"))
    _apply(cfg.launchers, raw.get("launchers"))
    _apply(cfg.regions, raw.get("regions"))
    _apply(cfg.webread, raw.get("webread"))
    _apply(cfg.fileio, raw.get("fileio"))
    _apply(cfg.shell, raw.get("shell"))
    _apply(cfg.visual_notify, raw.get("visual_notify"))
    _apply(cfg.doze, raw.get("doze"))
    _apply(cfg.voice, raw.get("voice"))
    _apply(cfg.skills, raw.get("skills"))
    _apply(cfg.ui, raw.get("ui"))
    return cfg
