import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type ChatItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; step?: number; text: string }
  | { kind: "tool"; step: number; action: string; args: any; result?: { ok: boolean; output?: string; error?: string } }
  | { kind: "image"; step: number; level: string; path?: string }
  | { kind: "system"; text: string }
  | { kind: "final"; status: string; text: string };

// 模块级单例：跨路由切换保持聊天状态。
export const chat = $state({
  items: [] as ChatItem[],
  running: false,
  currentStep: 0,
  totalSteps: 0,
  sidecarReady: false,
  sidecarStderr: [] as string[],
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

export async function ensureChatListeners(): Promise<void> {
  if (started) return;
  started = true;
  unlistenEvent = await listen<any>("ctrlapp://event", (e) => {
    const v = e.payload;
    const k = v.event;
    if (k === "ready") {
      chat.sidecarReady = true;
      push({ kind: "system", text: `sidecar ready · provider=${v.provider ?? "?"} · model=${v.model} · autonomy=${v.autonomy} · max_steps=${v.max_steps}` });
    } else if (k === "run_start") {
      chat.running = true;
      chat.currentStep = 0;
      chat.totalSteps = v.max_steps ?? 0;
      push({ kind: "system", text: `开始任务（${v.run_dir ?? ""}）` });
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
      push({ kind: "image", step: v.step, level: v.level, path: v.path });
    } else if (k === "final") {
      chat.running = false;
      push({ kind: "final", status: v.status, text: v.text });
    } else if (k === "error") {
      chat.running = false;
      push({ kind: "system", text: `错误：${v.message}` });
    }
  });
  unlistenSidecar = await listen<any>("ctrlapp://sidecar", (e) => {
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
}

export function disposeChatListeners(): void {
  unlistenEvent?.();
  unlistenSidecar?.();
  unlistenEvent = null;
  unlistenSidecar = null;
  started = false;
}

export async function startTask(text: string, autonomy: string, maxSteps: number): Promise<void> {
  if (!text) return;
  if (chat.running) {
    try { await invoke("sidecar_cancel"); } catch {}
    push({ kind: "system", text: "已取消上一任务，准备发送新任务…" });
    const t0 = Date.now();
    while (chat.running && Date.now() - t0 < 1500) {
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  push({ kind: "user", text });
  try {
    await invoke("sidecar_start_task", { args: { instruction: text, autonomy, maxSteps } });
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

export async function newThread(): Promise<void> {
  if (chat.running) {
    try { await invoke("sidecar_cancel"); } catch {}
  }
  chat.items = [];
  chat.currentStep = 0;
  chat.totalSteps = 0;
  push({ kind: "system", text: "— 新对话已开始 —" });
}
