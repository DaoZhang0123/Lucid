<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import {
    chat,
    ensureChatListeners,
    startTask,
    cancelTask,
    newThread as storeNewThread,
  } from "$lib/chatStore.svelte";

  let instruction = $state("");
  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);
  let scrollEl: HTMLDivElement | undefined = $state();

  // 滚到底部：每次 items 变化都触发
  $effect(() => {
    void chat.items.length;
    queueMicrotask(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });

  onMount(() => {
    void ensureChatListeners();
  });

  async function start() {
    const text = instruction.trim();
    if (!text) return;
    instruction = "";
    await startTask(text, autonomy, maxSteps);
  }

  async function newThread() {
    instruction = "";
    await storeNewThread();
  }

  async function cancel() {
    await cancelTask();
  }

  async function setAutonomy() {
    try {
      await invoke("sidecar_set_autonomy", { autonomy });
    } catch (e) {
      chat.items = [...chat.items, { kind: "system", text: `切换自动度失败：${e}` }];
    }
  }

  function fmtArgs(args: any): string {
    try { return JSON.stringify(args); } catch { return String(args); }
  }
</script>

<div class="app">
  <header>
    <div class="title">ctrlapp · 桌面 Agent</div>
    <div class="status" class:on={chat.sidecarReady} class:running={chat.running}>
      {#if chat.running}
        运行中 · 第 {chat.currentStep}/{chat.totalSteps} 步
      {:else if chat.sidecarReady}
        就绪
      {:else}
        sidecar 未连接
      {/if}
    </div>
    <a class="link" href="/history">历史回放</a>
    <a class="link" href="/settings">设置</a>
  </header>

  <div class="chat" bind:this={scrollEl}>
    {#each chat.items as it (it)}
      {#if it.kind === "user"}
        <div class="bubble user"><div class="role">你</div><div class="text">{it.text}</div></div>
      {:else if it.kind === "assistant"}
        <div class="bubble assistant"><div class="role">Agent · 第{it.step ?? "?"}步</div><div class="text">{it.text}</div></div>
      {:else if it.kind === "tool"}
        <div class="tool">
          <span class="badge">step {it.step}</span>
          <span class="action">{it.action}</span>
          <span class="args">{fmtArgs(it.args)}</span>
          {#if it.result}
            {#if it.result.ok}
              <span class="ok">✓ {it.result.output ?? ""}</span>
            {:else}
              <span class="err">✗ {it.result.error ?? ""}</span>
            {/if}
          {:else}
            <span class="pending">…</span>
          {/if}
        </div>
      {:else if it.kind === "image"}
        <div class="img-meta">📷 step {it.step} · {it.level}</div>
      {:else if it.kind === "system"}
        <div class="system">· {it.text}</div>
      {:else if it.kind === "final"}
        <div class="final final-{it.status}">
          {#if it.status === "ok"}🟢{:else if it.status === "cancelled"}🟡{:else}🔴{/if}
          {it.status} · {it.text}
        </div>
      {/if}
    {/each}
  </div>

  <footer>
    <div class="controls">
      <label>
        自动度
        <select bind:value={autonomy} onchange={setAutonomy}>
          <option value="full">full（不确认）</option>
          <option value="confirm_critical">confirm_critical（危险词确认）</option>
          <option value="confirm_each">confirm_each（每步确认）</option>
        </select>
      </label>
      <label>
        步数
        <input type="number" min="1" max="200" bind:value={maxSteps} />
      </label>
      <button class="cancel" onclick={cancel} disabled={!chat.running}>急停 (Ctrl+Alt+Esc)</button>
    </div>
    <form onsubmit={(e) => { e.preventDefault(); start(); }}>
      <button type="button" class="new-thread" title="新建对话（清空当前聊天，运行中会先取消）"
              onclick={newThread}>+</button>
      <textarea
        placeholder="告诉 Agent 你要做什么……回车发送，Shift+回车换行"
        rows="2"
        bind:value={instruction}
        onkeydown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(); } }}
      ></textarea>
      <button type="submit" disabled={!instruction.trim()}>发送</button>
    </form>
    {#if chat.sidecarStderr.length}
      <details class="stderr">
        <summary>sidecar 日志（{chat.sidecarStderr.length}）</summary>
        <pre>{chat.sidecarStderr.join("\n")}</pre>
      </details>
    {/if}
  </footer>
</div>

<style>
  :global(body) { margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f7f7f8; color: #222; }
  .app { display: flex; flex-direction: column; height: 100vh; }
  header {
    display: flex; align-items: center; gap: 1rem;
    padding: 0.6rem 1rem; background: #1f2937; color: #fff;
  }
  .title { font-weight: 600; }
  .status { font-size: 0.85rem; opacity: 0.8; }
  .status.on { color: #6ee7b7; opacity: 1; }
  .status.running { color: #fbbf24; opacity: 1; }
  .link { margin-left: auto; color: #93c5fd; text-decoration: none; font-size: 0.9rem; }
  .chat { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
  .bubble { max-width: 80%; padding: 0.55rem 0.75rem; border-radius: 8px; }
  .bubble .role { font-size: 0.7rem; opacity: 0.6; margin-bottom: 0.2rem; }
  .bubble.user { align-self: flex-end; background: #2563eb; color: #fff; }
  .bubble.assistant { align-self: flex-start; background: #fff; border: 1px solid #e5e7eb; }
  .tool { font-family: ui-monospace, Consolas, monospace; font-size: 0.78rem;
          background: #fef3c7; border: 1px solid #fde68a; padding: 0.3rem 0.5rem;
          border-radius: 6px; align-self: flex-start; max-width: 95%; word-break: break-all; }
  .badge { background: #92400e; color: #fff; padding: 0 0.4rem; border-radius: 4px; margin-right: 0.3rem; }
  .action { font-weight: 600; color: #92400e; }
  .args { color: #444; margin: 0 0.4rem; }
  .ok { color: #047857; }
  .err { color: #b91c1c; }
  .pending { color: #92400e; opacity: 0.7; }
  .img-meta { font-size: 0.75rem; color: #6b7280; align-self: flex-start; }
  .system { font-size: 0.78rem; color: #6b7280; align-self: center; font-style: italic; }
  .final { font-weight: 600; align-self: center; padding: 0.3rem 0.6rem; border-radius: 6px; }
  .final-ok { background: #d1fae5; color: #065f46; }
  .final-cancelled { background: #fef3c7; color: #92400e; }
  .final-max_steps, .final-error { background: #fee2e2; color: #991b1b; }
  footer { border-top: 1px solid #e5e7eb; padding: 0.6rem 1rem; background: #fff; }
  .controls { display: flex; gap: 1rem; align-items: center; margin-bottom: 0.5rem; font-size: 0.85rem; }
  .controls label { display: flex; gap: 0.3rem; align-items: center; }
  .controls input { width: 4rem; }
  .controls .cancel { margin-left: auto; background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5;
                      padding: 0.3rem 0.7rem; border-radius: 4px; cursor: pointer; }
  .controls .cancel:disabled { opacity: 0.5; cursor: not-allowed; }
  form { display: flex; gap: 0.5rem; align-items: stretch; }
  textarea { flex: 1; padding: 0.5rem; border: 1px solid #d1d5db; border-radius: 6px; font: inherit; resize: vertical; }
  form button { padding: 0 1.2rem; background: #2563eb; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }
  form button:disabled { opacity: 0.5; cursor: not-allowed; }
  form button.new-thread { padding: 0 0.9rem; background: #fff; color: #2563eb;
                           border: 1px solid #93c5fd; font-size: 1.3rem; line-height: 1;
                           font-weight: 600; }
  form button.new-thread:hover:not(:disabled) { background: #eff6ff; }
  .stderr { margin-top: 0.4rem; font-size: 0.75rem; color: #6b7280; }
  .stderr pre { background: #f3f4f6; padding: 0.4rem; max-height: 8rem; overflow: auto; }
</style>
