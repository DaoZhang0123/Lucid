# Voice Input — Push-to-Talk 建任务

> **状态**：Phase 1–3 已落地（sidecar transcribe RPC + Tauri 全局快捷键 + 设置页）。
>   - 仅支持 `faster-whisper`（`sherpa-onnx` / `vosk` 已从 UI 下架，只留下本文的调研记录）。
>   - 语言只约束在 **en / zh / fr** 三种（与 Lucid UI i18n 保持一致）。
>   - 悬浮窗仅支持屏顶居中，`overlay_screen` / `overlay_position` / `start_feedback=vibrate-tray` 这三个占位项从 UI 删除（config.toml 中保留 fallback，老配置不会报错）。
>   - 下一步：§5.2 描述的 **意图分发 LLM**（新 thread / 中止当前 / 续写输入框），还未落地。
> **目标问题**：能否做到 **完全本地、不依赖大模型 API、不依赖外部云服务** 的语音输入。
> **结论先行**：**完全可行**。Windows / Python 生态下有 3 条成熟离线 ASR 路线，且都是宽松开源协议（MIT / Apache-2.0），离线模型 30 MB ~ 500 MB 可选。下面给出选型对比、推荐方案和落地拆解。

---

## 1. 需求

- **触发（默认）**：**长按单键 `Space` ≥ 5 秒** 进入录音模式；按 `stop_mode` 决定如何停止。
  - **可访问性 (a11y) 优先**：很多残疾用户没法同时按多键组合（Ctrl+Alt+X 这种），所以默认采用"按住一个键足够久"的方案，不依赖修饰键，单手单指可达。
  - **完全可调**（在 `/settings → Voice` 调整，无需改配置文件）：
    - 触发键 `hotkey`（任意单键或组合，默认 `Space`）。
    - 长按阈值 `hold_threshold_ms`（默认 5000 ms；范围 200-10000 ms；支持 `0` = 改为按住即录的传统 PTT）。
    - 录音停止方式 `stop_mode`：`release`（松开即停）/ `tap_again`（再点一下停，默认）/ `auto_silence`（VAD 静音 1.5s 自动停）。
    - 触发反馈 `start_feedback`：`beep` / `vibrate-tray` / `silent`。
  - **冲突保护**：默认键 `Space` 在文本输入框聚焦时会冲突 → 焦点感知屏蔽（输入框 / IME 组合状态时不夺取，详见 §4.2）。
- **结果落点（意图分发）**：**不再靠用户在设置页二选一**（agent / dictation），而是转写完成后走一个 **快、小、便宜的意图分类 LLM**（详见 §5.2），把转写文本分到1个动作档：
  1. **`thread_new`** — 开一个新 thread（“帮我打开记事本写入 hello”，明显是一个新任务）。
  2. **`thread_abort`** — 中止当前正在跑的 thread（“停下” / “取消” / “不要了”）；发 cancel RPC 后不再走后续。
  3. **`dictation_append`** — 续写到当前输入框（“... 這篇文章要提到” 这种装填词不该被拿去起任务）。
  - 分类器返回 `low` 置信度时，悬浮窗出快选 chip让用户 1 帧设定。不再依赖 `[voice].mode`（可保留作为 override、默认 `auto`）。
  - 意图 LLM 默认走同一个已配置的主 LLM provider（copilot / anthropic）但走一个 **独立、超短的 system prompt** 与独立 max_tokens（~150），不跳进主 agent 循环，避免跳动主线的上下文。
- **离线 ASR**：不调用任何云端 ASR（Azure / OpenAI Whisper API / Google），ASR 不依赖大模型；模型权重本地装。**注意**：转录后的文本仍然会送给 Lucid 主 LLM（这是 Agent 控制的入口），与 ASR 是否本地无关。
- **延迟**：停止录音后 ≤ 1.5s 出文本（小模型在 CPU 上即可达到）。
- **语言**：至少英 / 中 / 法（与 i18n 三语保持一致），auto-detect。
- **隐私**：录音文件 / 中间 PCM **不外发**；可选保留到 `~/.lucid/voice/` 供调试，默认关。

---

## 2. 完全本地的 ASR 候选

### 2.1 横向对比

| 引擎 | License | 安装方式 | 模型大小 | CPU 速度（13min 音频，i7-12700K, 8 线程）| 中文 | 备注 |
|---|---|---|---|---|---|---|
| **faster-whisper** (CTranslate2) | MIT | `pip install faster-whisper` | small INT8 ≈ 466 MB；distil-small.en ≈ 166 MB；large-v3 INT8 ≈ 1.5 GB | small INT8 = **1m42s**（~7.6× realtime） | ✅ 多语 | Whisper 复刻；INT8 量化 CPU 友好；自带 Silero VAD 集成；PyAV 自带 FFmpeg → **零外部 native 依赖** |
| **whisper.cpp** (`pywhispercpp`) | MIT | `pip install pywhispercpp` | 同上（GGML 格式） | small q5 ≈ 与 faster-whisper 相当 | ✅ | 纯 C++ 后端；CMake 编译 wheel；macOS 上 Metal 加速；Windows 走 AVX/F16C |
| **sherpa-onnx** | Apache-2.0 | `pip install sherpa-onnx`（带预编译 wheel，含 onnxruntime） | SenseVoice small ≈ 234 MB；Paraformer-zh ≈ 220 MB；Zipformer streaming ≈ 70 MB | streaming Zipformer 实时；SenseVoice ≈ 5-10× realtime | ✅ Paraformer 是阿里开源中文专用，CER 极佳 | 真正的 streaming（边说边出字幕）；模型生态杂、版本多；纯 ONNX，体积小 |
| **vosk** (Kaldi) | Apache-2.0 | `pip install vosk` | small en ≈ 40 MB；small zh ≈ 42 MB；small fr ≈ 41 MB；big ≈ 1-1.8 GB | streaming，10× realtime | ✅ small-cn-0.22 | 经典 Kaldi 模型；准确率不如 Whisper（small-en WER 9.85% on librispeech vs distil-small.en ~5%）；**模型最小**，资源占用最低 |
| **Windows.Media.SpeechRecognition** | 系统自带 | WinRT API | 0（系统语言包） | 实时 | 取决于系统语言包 | UWP/WinRT API；准确率一般；需要用户在系统设置预先安装离线语言包；用 `winsdk`/`pywinrt` 调用 |
| **Const-me/Whisper** | MPL-2.0 | 单独 .exe / C# nuget | ggml-medium ≈ 1.4 GB | DirectCompute GPU；medium 在 1080Ti 19s | ✅ | **Windows-only + GPU-only**，作者 3 年没更新；Lucid 要做 sidecar 集成不合适 |

### 2.2 评估维度

1. **安装 friction**：用户装 NSIS 一键就能用，**不能**让用户额外装 CUDA / cuDNN / FFmpeg / Visual C++ redist 才能跑。→ `faster-whisper`、`vosk`、`sherpa-onnx` 三家的 wheel 都是开箱即用；`pywhispercpp` 在 Windows 偶尔编译失败。
2. **bundle 体积影响**：小模型走在线下载、按需缓存到 `~/.lucid/voice/models/`；二进制依赖（CTranslate2 / onnxruntime）会让 PyInstaller 产物多约 30-80 MB。
3. **准确率**：英文场景 Whisper distil-small > Vosk small；中文场景 Paraformer-zh ≈ Whisper small > Vosk small-cn。Whisper 有罕见的"幻听"（沉默时编造文本），需 VAD 截断弥补。
4. **延迟**：PTT 场景人通常 < 30s 一段，模型只需在松开后跑一次推理 — 不需要 streaming。所以 **streaming 不是必需特性**，可以排除掉这一维度的优势比较。
5. **多语言**：Whisper 系列开箱多语；Vosk / sherpa-onnx 需要按语种下不同模型。

### 2.3 推荐

> **首选：[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) + 按需下载 `distil-small.en` 或 `small` 模型。**
>
> **可选 / 进阶**：高频中文用户在设置页切到 `sherpa-onnx + Paraformer-zh` 获得更好的中文 CER；极轻量场景（< 100 MB 安装预算）切到 `vosk small`。

理由：
- **零 native 编译**：MIT、纯 Python wheel，PyAV 自带 FFmpeg。
- **多语自动检测**：一份模型扫掉英 / 中 / 法 / ar / ru，与 i18n 路线一致。
- **CPU INT8 速度足够**：人按住一句话（5-15s）→ 松开 → 1s 内出文本完全做得到（small INT8 在 i7 上 ≈ 7.6× realtime）。
- **隔离干净**：`from faster_whisper import WhisperModel` 一个类，没有线程 / 全局状态 / 服务端，sidecar 嵌入零负担。
- **模型托管在 HuggingFace `Systran/faster-whisper-*`**，首次启动后台静默拉取，一次 ~150 MB。

---

## 3. 录音 / 麦克风采集

两条路：

### A. **Tauri 前端采集**（推荐）

```ts
// 浏览器原生 API，零额外 dep
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const mr = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
```

- ✅ 不用动 Rust，不用引 PortAudio。
- ✅ Tauri 已经允许（webview 是 WebView2，原生支持）。
- ⚠️ Tauri 2 的 capability 必须显式给 webview 域 `microphone` 权限 → 在 `app/src-tauri/capabilities/default.json` 加一条；Windows 隐私设置里"麦克风访问"必须开过。
- 录音结束 → `Blob` → ArrayBuffer → base64 → 走新 RPC `transcribe_audio(b64, mime_type, sample_rate)` 发给 sidecar。
- 优点：UI 可以实时画波形 / 倒计时，体验最佳。

### B. **Python 侧采集**

```python
import sounddevice as sd  # MIT, PortAudio 包装
audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
```

- ✅ 全 Python，PyInstaller 友好（PortAudio dll 一起打包）。
- ❌ 需要加 sounddevice (~3 MB) + PortAudio dll (~150 KB)；UI 进度反馈要绕一圈走 sidecar 事件。
- ❌ 全局快捷键的 PTT "按住 / 松开" 状态机，Python 侧拿键盘事件得用 `keyboard` 包并提权（HOOK API），与现有 `tauri-plugin-global-shortcut` 重复。

**结论：选 A**。Rust 已经有 `tauri-plugin-global-shortcut = "2"`（[Cargo.toml](app/src-tauri/Cargo.toml#L23)），快捷键和录音都在前端，sidecar 只负责"接 base64 音频 → 出文本"这一件事。

---

## 4. 全局快捷键 (PTT) — 长按单键方案

### 4.1 行为

- 默认键 `Space`，长按阈值 3000 ms，停止方式 `tap_again`。
- `tauri-plugin-global-shortcut` 的 `Pressed` / `Released` 事件由前端组合成长按状态机：
  ```
  on Pressed   → start timer(hold_threshold_ms)
                 if Released before timer fires → 普通空格透传，不触发录音
                 if timer fires → enter recording mode（开窗 + 麦克风 + start_feedback）
  on Released  → stop_mode == release   : stop & transcribe
                 stop_mode == tap_again : 留窗等待下一次短按 (<300 ms) 即停
                 stop_mode == auto_silence : 留窗，VAD 1.5s 静音自动停
  ```
- `tap_again` 为 a11y 默认 —— 不需要持续按住、也不需要在停止时精确计时。

### 4.2 焦点屏蔽 (focus_aware)

- 当前置窗口焦点在输入框 / 编辑器 / IME 组合状态时，`Space` 不被夺取，原生空格行为生效。
  - Web 侧：`document.activeElement` 是 `INPUT` / `TEXTAREA` / `[contenteditable]` 时，handler 直接 return。
  - Native 侧：用 `GetForegroundWindow + GetGUIThreadInfo` 检查前台是否聚焦在 EDIT / RichEdit 控件。`lucid/window.py` 已有相关 helper，可复用。

### 4.3 可调项（设置页 UI）

- `hotkey` —— 任意单键或组合。设置页提供"录键"按钮，用户按一下即捕获保存。
- `hold_threshold_ms` —— 拉条 0 / 200 / 500 / 1000 / 2000 / 3000 / 5000 / 8000 / 10000；`< 1000 ms` 弹黄色"易误触"警告；`0` = 经典 PTT。
- `stop_mode` —— `release` / `tap_again` / `auto_silence` 单选。
- 与 `safety.emergency_hotkey` 冲突时，前端校验拒绝保存。

### 4.4 可访问性

- 长按 + 单键设计本身就是 a11y-first：单手单指可达，无需按键组合。
- 设置页提供"长按测试器"：用户按一下试，UI 实时画进度条，到阈值变绿。
- 触发反馈三选一：`beep`（短哔，听障可关）/ `vibrate-tray`（托盘图标抖动 200 ms，纯视觉）/ `silent`。

---

## 5. 端到端架构

```
┌──────────────────────── Tauri (Svelte) ────────────────────────┐
│                                                                 │
│  global_shortcut(cfg.voice.hotkey, default="Space")             │
│     ├─ Pressed:                                                 │
│     │     • 输入框聚焦 → 透传，return                           │
│     │     • setTimeout(hold_threshold_ms, enterRecording)       │
│     ├─ Released (before threshold):                             │
│     │     • clearTimeout → 透传普通空格                         │
│     └─ enterRecording():                                        │
│           1. 打开 VoiceOverlay 独立窗（屏幕顶部居中, y=8px）    │
│              360×72, always-on-top, transparent, no-decoration, │
│              skip-taskbar, focus=false, cursor-passthrough      │
│           2. tray icon → red dot；播放 start_feedback           │
│           3. mediaDevices.getUserMedia → MediaRecorder.start()  │
│           4. wave-form animation, max cfg.voice.max_seconds     │
│           5. 按 stop_mode 监听停止条件                          │
│              → MediaRecorder.stop() → Blob → ArrayBuffer → b64  │
│           6. overlay → "🧠 Transcribing…"                       │
│           7. invoke("sidecar_transcribe", { b64, mime })        │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
                               │  stdio JSON-RPC
                               ▼
┌──────────────────────── Python sidecar ────────────────────────┐
│                                                                 │
│  rpc.transcribe_audio(b64, mime, sample_rate?)                  │
│     1. base64 decode → temp file ~/.lucid/voice/last.webm       │
│     2. PyAV / faster-whisper 自带 demux → 16kHz mono PCM        │
│     3. Silero VAD trim 静音段                                   │
│     4. WhisperModel.transcribe(pcm, language=cfg.voice.lang)    │
│     5. return { text, language, duration_ms, confidence }       │
│                                                                 │
│  模型懒加载（首次调用时下载 + 加载到内存，常驻直到 sidecar 退）│
│  下载来源：HuggingFace Systran/faster-whisper-* 镜像            │
│  缓存目录：~/.lucid/voice/models/<size>/                        │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────── 文本落点（前端逻辑）──────────────────┐
│                                                                 │
│  overlay 显示转录文本（1.5s 反悔窗口，用户可点 ✗ 撤销）         │
│                                                                 │
│  if cfg.voice.mode == "dictation":                              │
│      append text to current input box, focus input              │
│      （纯听写：用户人工确认按回车）                              │
│  else:  # mode == "agent"  （默认）                              │
│      # 让 LLM 当指挥官 —— 文本作为 user message 送主循环        │
│      # 用户可以说"关掉当前任务" / "开个新 thread" / "暂停"      │
│      # 由模型选择调用 meta_tools.thread_close / thread_new /     │
│      # pause / resume，或正常分派到 tools                       │
│      if active_thread and not cfg.voice.always_new_thread:       │
│          append_user_message(active_thread, text)                │
│      else:                                                      │
│          thread_new() → start_task(text)                        │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

### 5.1 VoiceOverlay 悬浮窗（新组件）

**这是一个独立的 Tauri WebviewWindow，不是主窗的子元素**，目的是不挤占主窗内容也不打断用户当前操作。要点：

- **位置**：当前主屏顶部水平居中，`y = overlay_y_offset_px`（默认 8）。`overlay_screen` / `overlay_position` 在当前版本均为占位，不再暴露到 UI。
- **尺寸**：录音 `360×72`，转录 `360×56`，错误 `360×96`。
- **窗口属性**：`always_on_top = true`、`decorations = false`、`shadow = false`、`skip_taskbar = true`、`resizable = false`、`transparent = true`、`focused = false`（不抢焦点）。
- **视觉**：圆角 + 半透明毛玻璃 —— `border-radius: 16px; backdrop-filter: blur(12px); background: rgba(0,0,0,0.55);`，不走原生边框与阴影。
- **鼠标穿透**：默认开 `set_ignore_cursor_events(true)`，用户可以直接点穿；只有 hover 检测到要交互（✗ 撤销 / 模式切换）时短暂关闭穿透。
- **状态机**：`idle-hidden` / `holding`（屏顶细长进度条）/ `recording`（波形 + 倒计时 + ✗）/ `transcribing`（旋转图标）/ `result`（文本 + 意图 独立 chip：🤖新任务 / ⏹中止 / ⌨续写 + ✗ 撤销）/ `error`（红边 + 重试/保留/关闭）。
- **路由**：独立页 `app/src/routes/voice-overlay/+page.svelte`，零开销，与主窗解耦。

### 5.2 意图分发 LLM（新品设计，未落地）

**动机**：老设计里 `mode` 是一个全局开关——要么全部转写都发任务，要么全部填输入框。但现实使用是混合的：
- “帮我在桌面起个记事本” → 明显该走 thread。
- “这个别跑了” / “取消这个” → 中止。
- “……还要提到 X 和 Y” → 填输入框的装填词。
靠一个全局开关选不准；靠主 LLM 去意图识别又太重（走主循环 = 吃 system prompt + 截图上下文 + 可能调用 meta_tools）。所以加一个独立的、超轻量的意图分类调用。

**接口（调度在 sidecar `voice_dispatch` 新 RPC 里）**：

```python
# lucid/voice_dispatch.py (新)
class IntentResult(TypedDict):
    intent: Literal["thread_new", "thread_abort", "dictation_append"]
    confidence: Literal["high", "medium", "low"]
    reason: str        # 一句中文解释，供悬浮窗 tooltip
    cleaned_text: str  # 去除唤醒词、口头禄丝后的文本

def classify(transcript: str, ctx: VoiceContext) -> IntentResult: ...

@dataclass
class VoiceContext:
    has_running_thread: bool       # 当前是否有在跑的 thread
    active_input_focus: bool       # 前台是否输入框有焦点（前端传入）
    last_user_text: str | None     # 同一个 thread 上一句，帮判断 “还有……” 这种接词
    locale: str                    # "en" / "zh" / "fr"
```

**System prompt（定位：超短，同时垃圾不走主上下文）**：

```
You are a voice-to-action router. Read the transcript and classify it as exactly one of:
  - thread_new      : the user wants Lucid to start a NEW task (open an app, send a message,
                      summarise something, etc.). Verbs like “open” / “send” / “summari·ze” /
                      “write” / “compute” / “tell me”.
  - thread_abort    : the user wants to STOP / cancel the currently-running task. Triggers:
                      “stop” / “cancel” / “abort” / “never mind” / “don’t do that” /
                      Chinese “停下” / “取消” / “不要了”. ONLY pick this if has_running_thread = true.
  - dictation_append: the transcript is filler / continuation / correction meant for the
                      currently focused input box. Triggers: starts mid-sentence, contains
                      “also” / “and” / “one more thing” / “oops” / Chinese “还有…” /
                      “补充一句” / “刚才那句改成”. Only pick this if active_input_focus = true.
Return STRICT JSON: {"intent": "...", "confidence": "high|medium|low", "reason": "...",
"cleaned_text": "去掉唤醒词与口头禄丝后的文本"}.
No prose outside the JSON.
```

**调用**：`max_tokens=200`、`temperature=0`；走与主会话同一个 `provider`（复用 token / OAuth）；超时默认 6s，出错 → fallback 到以下 **纯正则启发识听**：

```python
_ABORT_RE = re.compile(r"^\s*(停下|取消|不要了|别跑了|stop|cancel|abort|never mind)\b", re.I)
_NEW_VERBS = re.compile(r"\b(open|launch|send|write|summari[sz]e|tell me|打开|发|写|总结)\b", re.I)
```

规则：`_ABORT_RE` 中 & has_running_thread → abort；action `_NEW_VERBS` 命中 → `thread_new`；其它 & active_input_focus → `dictation_append`；完全不能区分 → 默认 `thread_new` + `confidence=low` 、悬浮窗跳出 chip 让用户 1 帧选。

**前端映射 · result 状态里 1.5s 反悔窗口**：
- `thread_new` → `🤖 将作为新任务执行` + 默认 OK
- `thread_abort` → `⏹ 将中止当前任务` + 默认 OK；如 has_running_thread = false → 降级为 `thread_new`
- `dictation_append` → `⌨ 填入输入框` + 默认 OK；如 active_input_focus = false → 降级为 `thread_new`
- `confidence == low` → 不默认 OK，同时显示三个 chip 让用户点

**与 `[voice].mode` 的关系**：保留为越位 override。默认 `auto`＝交给分发 LLM；`mode=agent` / `dictation` 仍然可以硕性锁死不调用分发 LLM（低资源、离线、决议performance 需求逆作选项）。

**成本估算**：claude-opus-4.6 调用 ≈ 200 in / 80 out tokens ≈ 0.0007 USD/句；同时达到1 RPS 不会压跨 Copilot quota。中期可切到 Haiku / mini 类模型进一步限住。

---

## 6. 配置（新增 `[voice]` 段）

```toml
[voice]
enabled = false           # 默认关；用户在 /settings 显式开
engine = "faster-whisper" # 当前只支持 faster-whisper（sherpa-onnx / vosk 已从 UI 下架）
model_size = "small"      # tiny | base | small | medium | distil-small.en | distil-large-v3
language = "auto"         # auto | en | zh | fr  （仅这三种与 UI 三语保持一致）
                          #   "auto" / "system" / "detect" / 空：从系统区域设置（Win32
                          #   GetUserDefaultLocaleName，回退 Python locale +
                          #   LC_*）解析出主语言并 **snap 到 {en, zh, fr}**（不在集合
                          #   内一律落到 "en"），再下发给 WhisperModel.transcribe 的
                          #   `language=` 参数；这样避免 <2s 短语在 zh / ko / cy 之间漂移，
                          #   也避免下载 UI 不支持的语种模型管道。
                          #   显式 "en" / "zh" / "fr" 始终最高优先级；其它字符串会被强制
                          #   coerce 到系统语言（仍 snap 到三者之一）并打一行 stderr 警告。
                          #   设置页此项是下拉框，只能选 自动 / English / 简体中文 / Français。
compute_type = "int8"     # int8 | int8_float16 | float16 | float32（决定速度 / 显存）
device = "cpu"            # cpu | cuda（cuda 需用户自己装 CUDA 12 + cuDNN 9）
vad_filter = true         # Silero VAD 切静音
beam_size = 5
max_seconds = 30          # 录音硬上限

# —— 触发（a11y 优先：默认单键长按） ——
hotkey = "Space"            # 任意单键或组合，默认单键 Space
hold_threshold_ms = 3000    # 长按多少毫秒进入录音；0 = 经典 PTT（按下即录）
stop_mode = "tap_again"     # release | tap_again | auto_silence
start_feedback = "beep"     # beep | silent  (vibrate-tray 占位项已删除)
focus_aware = true          # 输入框聚焦时不抢 Space

# —— 文本去向 ——
mode = "auto"               # auto（默认）→ 走§5.2 意图分发 LLM选 thread_new / thread_abort / dictation_append；
                            # agent＝硕性锁死只走 thread_new；dictation＝硕性锁死只填输入框
always_new_thread = false   # mode=auto/agent 且意图 = thread_new 时：true=总是起新 thread；false=有当前 thread 则追写

# —— 悬浮窗 ——
overlay_y_offset_px = 8     # 只保留 y 偏移；overlay_position / overlay_screen 已从默认 toml 删除
                            # （lucid/config.py 仍保留字段让老配置不报错，但 UI 不再暴露）

# —— 杂项 ——
keep_audio = false               # 是否保留 ~/.lucid/voice/last.webm 供调试
hf_endpoint = ""                 # 国内可填 https://hf-mirror.com
```

冲突校验：`hotkey` 与 `safety.emergency_hotkey`、与系统已注册全局快捷键冲突时，`/settings` 拒绝保存并 toast。

---

## 7. 依赖清单

新增到 `pyproject.toml`：
- `faster-whisper>=1.1.0`（拖 CTranslate2 + tokenizers + onnxruntime + huggingface_hub + av）

总体积：约 **+80 MB** 给 PyInstaller 产物（CTranslate2 native 库 ~30 MB + onnxruntime ~15 MB + av ~10 MB + 其余 Python 依赖）。模型 **不打包**，首次启动按需下载。

PyInstaller `lucid.spec` 改动：
- `hiddenimports += collect_submodules("faster_whisper")`
- `hiddenimports += collect_submodules("ctranslate2")`
- `datas += collect_data_files("faster_whisper")`（其实只有 tokenizer 资源很小）
- `excludes -= ["torch", "transformers"]` ← 注意 faster-whisper 本身**不需要** torch（这是它的卖点）；现有 spec 已经 exclude 了，保持。

Tauri 改动：
- `app/src-tauri/capabilities/default.json` 加 `"webview:allow-internal:default"` + 在 main window 配置 `webPreferences` 允许 `mediaDevices`（Tauri 2 默认允许，需校验）。
- 新 Rust 命令 `sidecar_transcribe(b64: String, mime: String) -> Result<TranscribeResult>` 转发到 Python RPC。

---

## 8. UX 细节

所有视觉反馈都发生在 **VoiceOverlay 悬浮窗**（屏幕顶部居中）+ 托盘图标，**不挤占主窗内容区**。

| 阶段 | 悬浮窗状态 | 托盘 | 听觉 |
|---|---|---|---|
| 长按起始 | 隐藏；仅在屏顶 8px 高显示一条进度条，从 0 → `hold_threshold_ms` 充满变绿 | 无 | 无 |
| 阈值达成（进入录音）| 进度条收成胶囊，展开为 `360×72` 录音窗：左 🎙、中 实时波形、右 倒计时 + ✗ | 红点 | 一短"哔"（受 `start_feedback` 控制）|
| 录音中 | 波形动画；到 `max_seconds-5s` 倒计时颜色蓝→黄→红；到点自动停 | 红点闪烁 | 倒数 5s 一短"哔" |
| 停止 | 收缩为 `360×56` "🧠 Transcribing…" + 旋转图标 | 黄点 | 无 |
| 转录成功（agent）| 显示 `🤖 "<text>"`，1.5s 反悔窗口；用户可点 ✗ 撤销，否则送 LLM | 灭 | 一短"叮" |
| 转录成功（dictation）| 显示 `⌨ "<text>" → 输入框`，0.8s 后关窗 | 灭 | 一短"叮" |
| 转录失败 | 展开 `360×96` 红边，显示原因 + "重试 / 保留录音 / 关闭" | 灭 | 一短"咚" |
| 静音 / 太短 | 黄色提示"未识别到语音"，1s 后关窗 | 灭 | 无 |

**模式切换**：录音窗右下角小图标 `🤖 / ⌨` 单击切换 agent ↔ dictation，记忆下次默认。

**撤销**：agent 模式淡出窗口前的 1.5s 是反悔窗口 —— 点 ✗ 立刻取消，文本不送 LLM。

---

## 9. 隐私

- 默认 `keep_audio = false`：转录完即删 temp 文件；模型加载后**不再**触摸硬盘。
- 录音绝不外发：sidecar `transcribe_audio` 是纯本地调用，不走任何 HTTP。
- 模型下载是唯一的网络流量；从 HuggingFace 拉 `~150 MB`，离线场景可以让用户手动放进 `~/.lucid/voice/models/`，sidecar 检测到本地存在则跳过下载。
- macOS / Windows 系统级麦克风权限弹窗由 Tauri webview 自动触发；用户拒绝后 footer 给"请在系统设置开放麦克风权限"toast。

---

## 10. 安全 / 风险

1. **Space 单键冲突**：默认键 `Space` 在文本输入、游戏、视频播放器都常用。靠 §4.2 焦点感知屏蔽 + 5 秒长按门槛 双重保险 —— 普通短按 / 输入框聚焦时不会误触。游戏 / 全屏播放器仍有风险，文档提示用户改键。
2. **长按阈值过短风险**：用户把 `hold_threshold_ms` 设得太低（如 200 ms）会导致误触飙升。设置页 `< 1000 ms` 弹黄色警告。
3. **agent 模式越权**：转录文本直送 LLM 意味着用户的口误可能触发危险操作（说"删除"被理解成执行）。Lucid 现有的高危动作仍走原有 confirmation gate（敏感操作二次确认），不因来源是语音而绕过。
4. **Whisper 幻听**：完全静音时小模型偶尔输出 "Thank you." / "感谢观看"。VAD 必须开（默认 `vad_filter = true`），并 transcribe 后做长度过滤：`text.strip().split()` < 2 词 → 弃用 + 悬浮窗"未识别到语音"。
5. **模型下载墙**：国内拉 HuggingFace 慢/失败需要镜像。配置 `[voice].hf_endpoint = "https://hf-mirror.com"`，sidecar 启动时设到 `HF_ENDPOINT` 环境变量。
6. **PyInstaller bundle 增重 80 MB**：voice 做成可选模块——`[voice].enabled = false` 时**不导入** `faster_whisper`，sidecar 启动不付出加载开销。安装包大小不变（依赖始终 bundle），但内存 / 启动延迟差异显著。
7. **模型常驻 RAM**：small INT8 ≈ 1.5 GB；distil-small.en ≈ 1 GB。空闲 30 分钟后 sidecar 主动 `del model; gc.collect()` 释放，下次按快捷键重新加载（冷启动 ~3s 一次性成本）。
8. **悬浮窗抢焦点 / 抢点击**：必须 `focused=false` + `set_ignore_cursor_events(true)` 默认开启，否则会打断用户当前操作。仅在 hover 检测到要交互时短暂解除穿透。

---

## 11. 落地拆解（实现 TODO）

1. ✅ **本调研稿**（[Docs/voice-input.md](voice-input.md)）。
2. **Phase 1：sidecar `transcribe_audio` RPC**（无 UI，先用 `Test/run.py` 喂 .wav 验证）
   - 新模块 `lucid/voice.py`：`Transcriber` 类，懒加载模型，`.transcribe(audio_bytes, mime) -> TranscribeResult`。
   - `lucid/sidecar.py` 加 RPC 路由 + `_NON_ACTIVITY_METHODS` 白名单。
   - `lucid/config.py` 加 `VoiceConfig` dataclass。
3. **Phase 2：Tauri 前端 — 长按检测 + 录音 + 悬浮窗**
   - `app/src/lib/voice.ts`：MediaRecorder 封装 + base64 编码 + 长按状态机（`hold_threshold_ms` + `stop_mode` 三模式）+ focus-aware 屏蔽。
   - **`app/src/routes/voice-overlay/+page.svelte` + `app/src-tauri/src/lib.rs` 创建独立 WebviewWindow**：屏幕顶部居中、always-on-top、transparent、no-decorations、skip-taskbar、focused=false、cursor-passthrough。
   - `app/src/lib/components/VoiceOverlay.svelte`：`holding/recording/transcribing/result/error` 状态视图、波形动画、倒计时、模式切换 🤖/⌨、撤销 ✗。
   - `app/src-tauri/src/lib.rs`：注册全局快捷键 + `sidecar_transcribe` 命令 + `voice_overlay_show / hide / set_state` 三个 IPC。
   - i18n key：`voice.holding / voice.recording / voice.transcribing / voice.failed / voice.too_short / voice.permission_denied / voice.cancelled / voice.mode_agent / voice.mode_dictation`。
4. **Phase 3：设置页**
   - `/settings` 加 `[voice]` 区块：开关、引擎、模型、语言、`hotkey`（录键按钮）、`hold_threshold_ms`（拉条 + 阈值警告）、`stop_mode`（单选）、`mode`（agent/dictation 单选）、`start_feedback`、`always_new_thread`、`overlay_screen`、`hf_endpoint`。
   - 模型下载状态：调用 `voice_status` RPC 查 `model_loaded / downloading / size_mb`，进度条显示。
   - 长按测试器：用户按键模拟，UI 实时画进度条，阈值达成变绿。
5. **Phase 4：可选引擎**
   - `engine = "sherpa-onnx"` 适配（中文专用）。
   - `engine = "vosk"` 适配（极轻量）。
   - 工厂模式：`build_transcriber(cfg) -> Transcriber`。

---

## 12. 参考

- faster-whisper README：<https://github.com/SYSTRAN/faster-whisper>
- sherpa-onnx：<https://github.com/k2-fsa/sherpa-onnx>
- vosk：<https://github.com/alphacep/vosk-api>，模型列表 <https://alphacephei.com/vosk/models>
- Const-me/Whisper（GPU Windows demo）：<https://github.com/Const-me/Whisper>
- whisper.cpp：<https://github.com/ggerganov/whisper.cpp>
- Tauri global shortcut plugin：<https://v2.tauri.app/plugin/global-shortcut/>
- MediaRecorder MDN：<https://developer.mozilla.org/en-US/docs/Web/API/MediaRecorder>
