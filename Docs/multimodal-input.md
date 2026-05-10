# 多模态输入框设计（图片 / 文件附件）

> 状态：**草案**，待用户确认后实施。
> 目标：让对话框 [+page.svelte](../app/src/routes/+page.svelte#L273) 不止能发文字，
> 还能附带 **图片**（截图 / 本地图）和 **本地文件**（任意类型）。

---

## 1. 统一走“存盘 + reference”路线

> 设计决策：**不区分图片与文件**。所有附件都先落盘到本地 inbox 目录，然后以 `[Attached files]` 路径列表的形式拼进 instruction，**模型按需调用工具读取**（图片 → `load_local_images(path=…)`；文本 → `read_file(path=…)`；其他 → `run_shell` 或对应 App）。

| 类型 | 例子 | 落盘 | 模型看到什么 |
| --- | --- | --- | --- |
| **图片** | Win+Shift+S 剪贴板截图、`*.png/jpg/webp/gif/bmp` | `%LOCALAPPDATA%\dev.lucid\inbox\<uuid>.<ext>` | `[Attached image] 原名.png  (1.2 MB, 1920x1080)  →  C:\…\inbox\….png   使用 load_local_images(path=…) 查看` |
| **文件 / 目录** | `report.pdf`、`data.csv`、任意文件夹 | 保持原路径（不复制） | `[Attached file] report.pdf  →  C:\…\report.pdf   需要时用 read_file / run_shell / launch_app 打开` |

重要原因为什么**截图不再 inline 为 `image_url` 块**：
- 多个 model provider 对单轮图数量有隐性上限（Anthropic 官方建议≤5 张，Copilot/OpenAI 也不鼓励超过几张同时出现）；一次丢 N 张是费 token + 可能被拒。
- 后端已有 [`load_local_images`](../lucid/meta_tools.py#L327) 工具专门读本地 PNG/JPG 重新附加为下一次请求的 image，与现有轨迹重加机制复用，零后端工具改动。
- “全部路径化”让附件只占一行文本 token，不设上限也不会炸上下文；同时 thread 持久化也天然干净（本来就只是路径，不会被 prune）。
- 一致的代码路径：“什么路进来都是 file ref” —— sidecar / loop 代码只要处理一种形式。

> Phase 0 已完成：输入框左侧 `+` → 📎；新建对话按钮迁到侧边栏 “对话” 标题右侧。

---

## 2. 三种输入方式

### 2.1 工具栏按钮（明确）
**输入框左侧原 `+`（新对话）按钮直接改成 📎 附件按钮**——避免与全行业惯例（iMessage / ChatGPT / Claude / 微信 / Telegram / Slack / 飞书 / 钉钉 等输入框左 `+` ＝ 加附件）冲突。

- 📎 点击 → 调用 Tauri `dialog.open({ multiple: true })`
- 按扩展名分发：图片 → image attachments，其他 → file references
- 原 "新建对话" 按钮**迁移到左侧对话列表顶栏**（与 ChatGPT / Claude / Cursor 一致），那里已有列表上下文，新建条目自然显示在列表顶部。

### 2.2 拖拽（drop）
整个 chat 区域监听 `dragover` / `drop` 事件：
- `e.dataTransfer.files` 里的每个 `File` 同样按扩展名分发
- 拖拽期间显示一个半透明遮罩 "松开以附加 N 个文件"

### 2.3 粘贴（paste，**最高频**）
`textarea` 监听 `paste` 事件：
- `e.clipboardData.items` 中 `kind === "file"` 且 `type.startsWith("image/")`
  的项目 → 直接 `getAsFile()` 拿到 PNG（来自 Win+Shift+S）→ 作为 image attachment
- 普通文本粘贴不受影响

> 这三种方式都汇聚到同一个 `addAttachment(file: File)` 入口。

---

## 3. UI 草图

左侧栏 "对话" 标题右侧加 `+`（新建对话）；输入框左侧原 `+` → **📎**；附件 chip 行插在 textarea 上方。

```
┌─ 侧边栏 ─────────┐  ┌─ 主区 ───────────────────────────────────────┐
│ ◀ 对话      [+]  │  │   …聊天历史…                                  │
│  ▸ thread A     │  ├──────────────────────────────────────────────┤
│  ▸ thread B     │  │ 自动度 [confirm_critical] 步数 [50]   急停    │
│  …              │  ├──────────────────────────────────────────────┤
│                 │  │ ┌─────────────┐ ┌──────────────────────────┐ │
│                 │  │ │🖼 截图1.png ✕│ │📄 report.pdf  2.3 MB   ✕│ │
│                 │  │ │  缩略图     │ │  C:\...\Downloads\…     │ │
│                 │  │ └─────────────┘ └──────────────────────────┘ │
│                 │  ├──────────────────────────────────────────────┤
│                 │  │ [📎] │ 告诉 Agent 你要做什么……   │ [发送]    │
└─────────────────┘  └──────────────────────────────────────────────┘
```

- **图片** chip：80×60 缩略图 + 文件名 + 删除 ✕，点击放大到 lightbox
- **文件** chip：图标（按扩展名）+ 文件名 + 大小 + 删除 ✕，hover 显示完整路径
- 发送后清空 chip 行；如取消尚未发送的输入也清空

---

## 4. 数据流

### 4.1 前端类型与发送

```ts
// chatStore.svelte.ts 内
type FileRef = {
  name: string;     // 显示名（不含路径）
  path: string;     // 绝对路径；inbox 里的那份不同于原始名
  kind: "image" | "file";   // 仅用于 UI 选择 chip 样式 / 提示词
  size?: number;    // 可选，inbox 图记录原始字节数
  width?: number;
  height?: number;
  preview_url?: string;  // 仅图片 chip 缩略显示用（blob: URL，不传后端）
};
```

发送时 (`startTask()` 扩展)：

```ts
await invoke("sidecar_start_task", {
  instruction,
  autonomy, maxSteps,
  fileRefs: refs.map(r => ({ name: r.name, path: r.path, kind: r.kind })),
});
```

### 4.2 三种入口 → 同一个 `addAttachment(File | path)`

| 入口 | 处理 |
| --- | --- |
| 📎 点击 | `dialog.open({ multiple: true, directory: false })` 返回绝对路径数组 → `kind="file"` （是否是图片看后缀），path 直接用 |
| 拖拽 | Tauri 2 的 `onFileDropEvent` 推送绝对路径列表 → 同上 |
| 粘贴截图 | `paste` 事件 → `clipboardData.items[i].getAsFile()` 拿到 PNG `Blob` → 发起 `invoke("save_inbox_image", { name, bytes })` → 拿到 inbox 路径 → `kind="image"` |

> 路径全交由 Tauri 后端产出，**前端不拼路径、不读磁盘**。

### 4.3 后端 sidecar schema

[`_rpc_start_task`](../lucid/sidecar.py#L1377) 新增一个可选字段：

```python
file_refs = params.get("file_refs") or []   # list[{name, path, kind?}]
```

透传：
1. `_rpc_start_task` 会把 `file_refs` 加进立即走 / 入队两条分支。
2. queue_item 加 `"file_refs"` 键保持不丢。
3. `_run_task(..., file_refs=file_refs)` → `Agent.run(instruction, file_refs=...)`；Agent 再贴到 instruction_content。

### 4.4 loop.py 渲染规则（唯一改动点）

在 [`loop.py` instruction_content 构造点](../lucid/loop.py#L582)，在“任务：…”之后、L1 说明之前插入 `[Attached files]` 块：

```python
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

text_parts = [f"任务：{instruction}"]

if file_refs:
    text_parts.append("")
    text_parts.append(
        "[Attached files] 用户随本次任务附带了以下本地文件/图片。它们未被预读，"
        "请根据任务需要**按需**调用对应工具读取：\n"
        "  • 图片 (.png/.jpg/.webp/...) → `load_local_images(path=…, level=\"L2\")` 重新附加为可看图像\n"
        "  • 文本文件 (.md/.txt/.json/.csv/.log/.py/…) → `read_file(path=…)`\n"
        "  • 其他二进制 (.pdf/.docx/…) → `run_shell` 调试/提取，或 `launch_app` 打开对应软件\n"
        "  • 路径是**目录** → `run_shell` 调 `dir` / `Get-ChildItem` 列出后再递归处理\n"
        "不要在没有读过的情况下猜测内容。所有路径都是绝对路径，不需要补全。"
    )
    text_parts.append("")
    for ref in file_refs:
        nm = _sanitize_inline(ref.get("name") or "")
        pt = _sanitize_inline(ref.get("path") or "")
        kind = ref.get("kind") or ("image" if Path(pt).suffix.lower() in IMG_EXTS else "file")
        tag = "image" if kind == "image" else "file"
        text_parts.append(f"  - [{tag}] {nm}  →  {pt}")
```

要点：
- **不产生 `image_url` 块**：是否变成可看图像完全由模型调 `load_local_images` 决定，避免爆上下文。
- `_sanitize_inline()` 剩 `\n`/`\r`、反引用、ANSI 控制字符，防 prompt injection。
- L1 起手截图逻辑不变；附件块在 L1 说明前面，让“你带了什么”紧跟“你要我做什么”。

---

## 5. 路径从哪里来

| 来源 | 拿到的是什么 | 处理 |
| --- | --- | --- |
| **拖拽** | Tauri 2 的 `getCurrentWebview().onDragDropEvent` 事件里的 `paths: string[]`（绝对路径） | 直接用，按后缀判 kind |
| **📎 按钮** | `@tauri-apps/plugin-dialog` `open()` 返回绝对路径 / null | 同上 |
| **粘贴截图** | `paste` 事件里 `clipboardData.items[i].getAsFile()` 拿到 PNG `Blob`，**无磁盘路径** | 调 Tauri 命令 `save_inbox_image(name, bytes)` 写入 `%LOCALAPPDATA%\dev.lucid\inbox\<uuid>.png`，返回绝对路径 |
| 粘贴一个文件（Explorer 复制后 Ctrl+V） | `clipboardData.files[0]` 在 Tauri 下也有 path 属性 | 同拖拽 |

> inbox 目录不自动清理（不是运行时进程，没有可靠的生命周期）。错过了就是错过了，用户可随时二选全删。
> 未来可加一个启动时 “删除 inbox 中 > 30 天未访问的文件” 的清理环节，本期不做。

---

## 6. 大小 / 安全

- **存盘路径**按 `_sanitize_inline` 处理后拼接，防止文件名伪造 system prompt 边界。
- **不限附件数量、不限大小**。路径本身 token 只几十字节，爆不了上下文；是否读、读多大完全交给模型。
- **路径白名单**：不做。Lucid `safety` 模块已管控写操作；读取是模型的主动工具调用，沉用现有 HITL。
- **不带路径的粘贴**（Win+Shift+S 剪贴板）：Tauri 后端命令 `save_inbox_image` 接收 `Vec<u8>` 写入 inbox，文件名 `<yyyymmdd-HHMMSS>-<uuid8>.png`。
- **F3 续接**：file_refs 只在本次任务的起手 user message 里出现，不入 prelude。用户下个任务不会被上个任务的附件干扰。`load_local_images` 调后产生的图片照现有 prune 逻辑逐渐变占位符。

---

## 7. 受影响文件清单

```
app/src/routes/+page.svelte                ID UI（chip 行 + 三种入口 + drop 遮罩）
app/src/lib/chatStore.svelte.ts            ID FileRef 类型、attachments state、startTask 带 fileRefs
app/src/lib/i18n/messages/{zh-CN,en,fr-FR}.json   ID 文案
app/src-tauri/src/lib.rs                   ID 新 Tauri 命令 save_inbox_image 、start_task 透传 file_refs
app/src-tauri/Cargo.toml                   ID +tauri-plugin-dialog
app/src-tauri/capabilities/default.json    ID 允许 dialog:default + drag-drop 事件
lucid/sidecar.py               ID _rpc_start_task / _run_task / queue_item 加 file_refs
lucid/loop.py                  ID Agent.run 接 file_refs，instruction_content 加 [Attached files] 块
```

---

## 8. 不做什么

1. **不**在后端自动快取附件内容。读什么、读多少、读几次，都是模型决定。
2. **不**自动 OCR / PDF 解析。该开 Adobe Reader 就开。
3. **不**支持 http(s) 远程图片 URL。需要用 `read_webpage` 或先下载。
4. **不**拷贝原始文件到 inbox（只有粘贴截图才写 inbox）。拖拽 / 选择的文件保留原路径，避免重复磁盘、避免路径不一致。
5. **不**动 LLM client 层：附件是 instruction 里的一段文本，现有传输路径未变。
6. **不**限制附件数量与大小——全部走路径 reference 后爆不了上下文。
7. **不**为附件加“快速操作”按钮（如 OCR / 总结），保持最小表面。

---

## 9. 实施分期建议

- **Phase 0（已完成）** 输入框左侧 `+` → 📎。
- **Phase 1：粘贴截图**：`paste` 事件 → `save_inbox_image` Tauri 命令 → chip UI → 随 start_task 带 file_refs → sidecar / loop 接住 → [Attached files] 块递交。
- **Phase 2：📎 按钮**接通 `dialog.open` + 拖拽 `onDragDropEvent` 提供路径。
- **Phase 3：打磨**：Tab/Delete 键盘可达、拖拽遮罩、i18n 漏网、chip 点击预览。

三个 Phase 本轮**一次性实施**（用户明确要求：“你把所有的 Phase 都打通”）。

---

## 10. 待用户确认的问题

1. 一次最多附几张图？不限制图片数量，先将图片保存到本地，告诉模型按需读取，以防model provider拒绝处理过多图片
2. 文件 reference 是否需要在前端也能看到大小 / 存在性校验？否，让模型决定
3. 拖入一个**目录**怎么处理？让模型决定
4. 是否给附件加"快速操作"按钮？不用
