import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { get } from "svelte/store";
import { _ } from "svelte-i18n";

function t(key: string, values?: Record<string, string | number | boolean | Date | null | undefined>): string {
  try {
    const fn = get(_);
    return values ? fn(key, { values }) : fn(key);
  } catch {
    return key;
  }
}

// 由 startTask 设为 true；handleEvent 里下一次 thread_changed 后重置。
// 避免本轮刚 push 的用户消息被 thread 创建事件冲掉。
let _skipNextThreadChangedClear = false;

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; step?: number; text: string }
  | { kind: "tool"; step: number; action: string; args: any; result?: { ok: boolean; output?: string; error?: string } }
  | { kind: "image"; step: number; level: string; threadId?: string; file?: string; path?: string; dataUrl?: string; fromUser?: boolean }
  | { kind: "system"; text: string }
  | { kind: "final"; status: string; text: string };

export type ThreadMeta = {
  id: string;
  title: string;
  created_ms?: number;
  updated_ms?: number;
  task_count?: number;
};

// 模块级单例：跨路由切换保持聊天状态。
export const chat = $state({
  items: [] as ChatItem[],
  running: false,
  currentStep: 0,
  totalSteps: 0,
  sidecarReady: false,
  sidecarStderr: [] as string[],
  threads: [] as ThreadMeta[],
  activeThreadId: null as string | null,
  // 任务队列中的 thread id 集合，用于侧边栏“⏳ 排队中”标记
  queuedThreadIds: [] as string[],
  // 当前正在运行的 thread id
  runningThreadId: null as string | null,
});

let started = false;
let unlistenEvent: UnlistenFn | null = null;
let unlistenSidecar: UnlistenFn | null = null;

function push(item: ChatItem) {
  chat.items = [...chat.items, item];
}

function lastToolCallFor(step: number, action: string): ChatItem | undefined {
  for (let i = chat.items.length - 1; i >= 0; i--) {
    const it = chat.items[i];
    if (it.kind === "tool" && it.step === step && it.action === action && !it.result) return it;
  }
  return undefined;
}

async function loadImageDataUrl(threadId: string, file: string): Promise<string | undefined> {
  try {
    return await invoke<string>("thread_read_image", { threadId, fileName: file });
  } catch {
    return undefined;
  }
}

function handleEvent(v: any) {
  const k = v.event;
  if (k === "ready") {
    chat.sidecarReady = true;
    if (typeof v.max_steps === "number" && v.max_steps > 0) chat.totalSteps = v.max_steps;
    push({ kind: "system", text: `sidecar ready · provider=${v.provider ?? "?"} · model=${v.model} · autonomy=${v.autonomy} · max_steps=${v.max_steps}` });
    void refreshThreadList();
  } else if (k === "thread_changed") {
    const newId = v.id ?? null;
    const prevId = chat.activeThreadId;
    chat.activeThreadId = newId;
    // 若 sidecar 把活动 thread 切换到了另一条（例如调度器到点新建了一个 thread），
    // 旧 thread 的 chat.items 不能继续接收新 thread 的事件——清空并按需重放新 thread
    // 已有的历史事件（新建的空 thread 直接得到空白视图）。
    // 但：若本轮 startTask 刚 push 了一条用户消息，后端刚创建出来的
    // thread_changed 正好会把它冲掉；为此加一个一次性跳过标志。
    if (newId && newId !== prevId && !_skipNextThreadChangedClear) {
      chat.items = [];
      chat.currentStep = 0;
      chat.totalSteps = 0;
      void (async () => {
        try {
          const data: any = await invoke<any>("thread_read", { id: newId });
          const events: any[] = data?.events ?? [];
          // 中途切换：仅重放，不重复 push 当前 sidecar 还在 emit 的事件。
          // 简单起见：如果本地 items 还是空，就用历史事件填充；之后的实时事件会自然 append。
          if (!chat.items.length && events.length) {
            for (const ev of events) {
              const ek = ev.event;
              if (ek === "user_input") chat.items.push({ kind: "user", text: ev.text ?? "" });
              else if (ek === "user_attachments") pushUserAttachments(Array.isArray(ev.refs) ? ev.refs : []);
              else if (ek === "run_start") chat.items.push({ kind: "system", text: t("chat.run_started") });
              else if (ek === "assistant_text") chat.items.push({ kind: "assistant", step: ev.step, text: ev.text ?? "" });
              else if (ek === "tool_call") chat.items.push({ kind: "tool", step: ev.step, action: ev.action, args: ev.args });
              else if (ek === "tool_result") {
                for (let i = chat.items.length - 1; i >= 0; i--) {
                  const it = chat.items[i];
                  if (it.kind === "tool" && it.step === ev.step && it.action === ev.action && !it.result) {
                    it.result = { ok: ev.ok, output: ev.output, error: ev.error };
                    break;
                  }
                }
              } else if (ek === "step_image") {
                chat.items.push({ kind: "image", step: ev.step, level: ev.level, threadId: ev.thread_id ?? newId, file: ev.file, path: ev.path });
              } else if (ek === "final") chat.items.push({ kind: "final", status: ev.status, text: ev.text ?? "" });
              else if (ek === "error") chat.items.push({ kind: "system", text: `错误：${ev.message}` });
            }
            chat.items = [...chat.items];
          }
        } catch {
          // 新 thread 还没落盘，忽略；后续实时事件会填充视图
        }
      })();
    }
    _skipNextThreadChangedClear = false;
    void refreshThreadList();
  } else if (k === "run_start") {
    chat.running = true;
    chat.currentStep = 0;
    chat.totalSteps = v.max_steps ?? 0;
    if (v.thread_id) chat.runningThreadId = v.thread_id;
    push({ kind: "system", text: t("chat.run_started") });
  } else if (k === "step_start") {
    chat.currentStep = v.step;
    chat.totalSteps = v.max_steps ?? chat.totalSteps;
  } else if (k === "assistant_text") {
    push({ kind: "assistant", step: v.step, text: v.text });
  } else if (k === "tool_call") {
    push({ kind: "tool", step: v.step, action: v.action, args: v.args });
  } else if (k === "tool_result") {
    const t = lastToolCallFor(v.step, v.action);
    if (t && t.kind === "tool") {
      t.result = { ok: v.ok, output: v.output, error: v.error };
      chat.items = [...chat.items];
    }
  } else if (k === "step_image") {
    const item: ChatItem = { kind: "image", step: v.step, level: v.level, threadId: v.thread_id, file: v.file, path: v.path };
    push(item);
    // 异步加载缩略图（直接读 thread 目录里的文件）
    if (v.thread_id && v.file) {
      void (async () => {
        const url = await loadImageDataUrl(v.thread_id, v.file);
        if (url) {
          item.dataUrl = url;
          chat.items = [...chat.items];
        }
      })();
    }
  } else if (k === "final") {
    chat.running = false;
    chat.runningThreadId = null;
    push({ kind: "final", status: v.status, text: v.text });
  } else if (k === "error") {
    chat.running = false;
    chat.runningThreadId = null;
    push({ kind: "system", text: `错误：${v.message}` });
  } else if (k === "user_input") {
    // 这个事件只在重放老 thread 时使用；实时 user 消息已经在 startTask 里 push
  } else if (k === "user_attachments") {
    // live：用户在当前 thread 提交了附件；渲染 chip 卡片。
    pushUserAttachments(Array.isArray(v.refs) ? v.refs : []);
  } else if (k === "task_queued") {
    if (v.thread_id && !chat.queuedThreadIds.includes(v.thread_id)) {
      chat.queuedThreadIds = [...chat.queuedThreadIds, v.thread_id];
    }
    void refreshThreadList();
  } else if (k === "task_dequeued") {
    if (v.thread_id) {
      chat.queuedThreadIds = chat.queuedThreadIds.filter((x) => x !== v.thread_id);
      chat.runningThreadId = v.thread_id;
    }
    if (Array.isArray(v.queue)) {
      chat.queuedThreadIds = v.queue.map((it: any) => it.thread_id);
    }
    void refreshThreadList();
  } else if (k === "queue_changed") {
    if (Array.isArray(v.queue)) {
      chat.queuedThreadIds = v.queue.map((it: any) => it.thread_id);
    }
  }
}

export async function ensureChatListeners(): Promise<void> {
  if (started) return;
  started = true;
  unlistenEvent = await listen<any>("lucid://event", (e) => handleEvent(e.payload));
  unlistenSidecar = await listen<any>("lucid://sidecar", (e) => {
    const v = e.payload;
    if (v.kind === "stderr") {
      chat.sidecarStderr = [...chat.sidecarStderr, v.line].slice(-50);
    } else if (v.kind === "spawn") {
      push({ kind: "system", text: `sidecar 启动中：${v.exe}` });
    } else if (v.kind === "exit") {
      chat.sidecarReady = false;
      chat.running = false;
      push({ kind: "system", text: `sidecar 已退出（code=${v.code}），1 秒后自动重启` });
    } else if (v.kind === "spawn_error") {
      push({ kind: "system", text: `sidecar 启动失败：${v.message}` });
    } else if (v.kind === "emergency_stop") {
      push({ kind: "system", text: "🛑 急停：已请求取消任务" });
    }
  });
  void refreshThreadList();
}

export function disposeChatListeners(): void {
  unlistenEvent?.();
  unlistenSidecar?.();
  unlistenEvent = null;
  unlistenSidecar = null;
  started = false;
}

export async function refreshThreadList(): Promise<void> {
  try {
    const r = await invoke<any>("thread_list");
    chat.threads = (r?.threads ?? []) as ThreadMeta[];
    chat.activeThreadId = (r?.active ?? chat.activeThreadId) as string | null;
  } catch {
    // sidecar 可能还没起来，忽略
  }
}

export type FileRef = {
  name: string;
  path: string;
  kind: "image" | "file" | "folder";
};

/** 把一组用户附件渲染成聊天项：图片走右对齐 image-card（异步填 dataUrl），
 * 非图片走系统文本行。被三处共用：(1) startTask 立即 push 之前的 echo
 * （现已下线，由后端事件统一产生）；(2) live 事件 user_attachments；
 * (3) openThread 重放历史 thread。 */
function pushUserAttachments(refs: FileRef[]): void {
  if (!refs.length) return;
  const images = refs.filter((r) => r.kind === "image");
  const others = refs.filter((r) => r.kind !== "image");
  for (const im of images) {
    const card: any = { kind: "image", step: 0, level: "📎 附件", path: im.path, fromUser: true };
    chat.items.push(card);
    const idx = chat.items.length - 1;
    void invoke<string>("read_attachment_b64", { path: im.path })
      .then((url) => {
        chat.items = chat.items.map((x, i) =>
          i === idx && (x as any).path === im.path && x.kind === "image"
            ? ({ ...x, dataUrl: url } as any)
            : x);
      })
      .catch((e) => {
        chat.items = chat.items.map((x, i) =>
          i === idx ? ({ kind: "system", text: `📎 ${im.name}（无法预览：${e}）` } as any) : x);
      });
  }
  if (others.length) {
    const lines = others.map((r) => {
      const icon = r.kind === "folder" ? "📁" : "📎";
      return `  ${icon} ${r.name}  →  ${r.path}`;
    }).join("\n");
    chat.items.push({ kind: "system", text: `附带 ${others.length} 个项：\n${lines}` });
  }
  chat.items = [...chat.items];
}

export async function startTask(
  text: string,
  autonomy: string,
  maxSteps: number,
  fileRefs: FileRef[] = [],
): Promise<void> {
  if (!text) return;
  // 用户气泡先落地；附件 chip 由后端持久化的 user_attachments 事件统一驱动
  // （live 实时事件 + 重新打开 thread 重放）。
  push({ kind: "user", text });
  // 后端会为本次 task 创建 / 切换 thread，会发出一条 thread_changed；
  // 其默认处理是清空 chat.items。这里提前设一下标志跳过那一次，
  // 避免用户刚输入的一条 user 消息被冲掉。
  _skipNextThreadChangedClear = true;
  try {
    const res: any = await invoke("sidecar_start_task", {
      args: {
        instruction: text,
        autonomy,
        maxSteps,
        fileRefs: fileRefs.length ? fileRefs : undefined,
      },
    });
    if (res && res.queued) {
      const pos = res.position ?? "?";
      push({ kind: "system", text: `⏳ 已加入队列（第 ${pos} 位），当前任务完成后自动执行` });
      if (res.thread_id && !chat.queuedThreadIds.includes(res.thread_id)) {
        chat.queuedThreadIds = [...chat.queuedThreadIds, res.thread_id];
      }
      void refreshThreadList();
    }
  } catch (e) {
    push({ kind: "system", text: `启动失败：${e}` });
  }
}

export async function cancelTask(): Promise<void> {
  try {
    await invoke("sidecar_cancel");
    push({ kind: "system", text: "已发送取消请求…" });
  } catch (e) {
    push({ kind: "system", text: `取消失败：${e}` });
  }
}

/** 新建 thread：清空 UI 和 active 标记，但不真的创建目录；
 *  等用户首次发送任务时，后端会自动以 instruction 作为标题创建 thread。 */
export async function newThread(): Promise<void> {
  // 不再取消当前任务 —— 后续发送会自动排队。
  chat.items = [];
  chat.currentStep = 0;
  chat.totalSteps = 0;
  try {
    await invoke("thread_set_active", { id: null });
    chat.activeThreadId = null;
    push({ kind: "system", text: "— 新对话已开始，输入消息后将出现在左侧列表 —" });
  } catch (e) {
    push({ kind: "system", text: `新建对话失败：${e}` });
  }
  void refreshThreadList();
}

/** 把某个历史 thread 加载进聊天窗口，并设为 active（后续输入会续写到它里面）。 */
export async function openThread(id: string): Promise<void> {
  // 不再取消当前任务；它会在后台继续写到原来的 thread。
  try {
    await invoke("thread_set_active", { id });
  } catch (e) {
    push({ kind: "system", text: `切换 thread 失败：${e}` });
    return;
  }
  let data: any;
  try {
    data = await invoke<any>("thread_read", { id });
  } catch (e) {
    push({ kind: "system", text: `读取 thread 失败：${e}` });
    return;
  }
  chat.items = [];
  chat.activeThreadId = id;
  chat.currentStep = 0;
  chat.totalSteps = 0;
  const events: any[] = data?.events ?? [];
  // 把 events.jsonl 重放成 ChatItem 列表
  for (const v of events) {
    const k = v.event;
    if (k === "user_input") {
      chat.items.push({ kind: "user", text: v.text ?? "" });
    } else if (k === "user_attachments") {
      pushUserAttachments(Array.isArray(v.refs) ? v.refs : []);
    } else if (k === "run_start") {
      chat.items.push({ kind: "system", text: t("chat.run_started") });
    } else if (k === "assistant_text") {
      chat.items.push({ kind: "assistant", step: v.step, text: v.text ?? "" });
    } else if (k === "tool_call") {
      chat.items.push({ kind: "tool", step: v.step, action: v.action, args: v.args });
    } else if (k === "tool_result") {
      // 找最近一个未填 result 的 tool_call 填进去
      for (let i = chat.items.length - 1; i >= 0; i--) {
        const it = chat.items[i];
        if (it.kind === "tool" && it.step === v.step && it.action === v.action && !it.result) {
          it.result = { ok: v.ok, output: v.output, error: v.error };
          break;
        }
      }
    } else if (k === "step_image") {
      chat.items.push({ kind: "image", step: v.step, level: v.level, threadId: v.thread_id ?? id, file: v.file, path: v.path });
    } else if (k === "final") {
      chat.items.push({ kind: "final", status: v.status, text: v.text ?? "" });
    } else if (k === "task_close") {
      // 静默
    } else if (k === "error") {
      chat.items.push({ kind: "system", text: `错误：${v.message}` });
    }
  }
  chat.items = [...chat.items];
  // 异步加载所有图片
  for (const it of chat.items) {
    if (it.kind === "image" && it.threadId && it.file && !it.dataUrl) {
      void (async () => {
        const url = await loadImageDataUrl(it.threadId!, it.file!);
        if (url) {
          it.dataUrl = url;
          chat.items = [...chat.items];
        }
      })();
    }
  }
}

export async function deleteThread(id: string): Promise<void> {
  try {
    await invoke("thread_delete", { id });
  } catch (e) {
    push({ kind: "system", text: `删除 thread 失败：${e}` });
    return;
  }
  if (chat.activeThreadId === id) {
    chat.activeThreadId = null;
    chat.items = [];
  }
  void refreshThreadList();
}
