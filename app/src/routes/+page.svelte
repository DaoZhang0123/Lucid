<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import {
    chat,
    ensureChatListeners,
    startTask,
    cancelTask,
    newThread as storeNewThread,
    openThread,
    deleteThread,
    refreshThreadList,
  } from "$lib/chatStore.svelte";

  let instruction = $state("");
  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);
  let scrollEl: HTMLDivElement | undefined = $state();
  let sidebarOpen = $state(true);
  let lightbox = $state<string | null>(null);

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

  async function onPickThread(id: string) {
    if (id === chat.activeThreadId) return;
    await openThread(id);
  }

  async function onDeleteThread(e: MouseEvent, id: string) {
    e.stopPropagation();
    if (!confirm("删除这个对话？")) return;
    await deleteThread(id);
  }

  function fmtArgs(args: any): string {
    try { return JSON.stringify(args); } catch { return String(args); }
  }

  function fmtTime(ms?: number): string {
    if (!ms) return "";
    const d = new Date(ms);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toTimeString().slice(0, 5);
    }
    return `${d.getMonth() + 1}/${d.getDate()}`;
  }
</script>

<div class="app">
  <header>
    <button class="toggle" title={sidebarOpen ? "隐藏侧边栏" : "显示侧边栏"}
            onclick={() => { sidebarOpen = !sidebarOpen; if (sidebarOpen) void refreshThreadList(); }}>
      {sidebarOpen ? "‹" : "›"}
    </button>
    <div class="title">ctrlapp · 桌面 Agent</div>
    <div class="status" class:on={chat.sidecarReady} class:running={chat.running}>
      {#if chat.running}
        运行中 · 第 {chat.currentStep}/{chat.totalSteps} 步{#if chat.queuedThreadIds.length} · 队列 {chat.queuedThreadIds.length}{/if}
      {:else if chat.sidecarReady}
        就绪{#if chat.queuedThreadIds.length} · 队列 {chat.queuedThreadIds.length}{/if}
      {:else}
        sidecar 未连接
      {/if}
    </div>
    <a class="link" href="/templates">模板</a>
    <a class="link" href="/schedules">定时</a>
    <a class="link" href="/memory">记忆</a>
    <a class="link" href="/tools">技巧</a>
    <a class="link" href="/settings">设置</a>
  </header>

  <div class="body">
    {#if sidebarOpen}
      <aside class="sidebar">
        <div class="side-head">
          <span>对话</span>
          <button class="side-new" title="新建对话" onclick={newThread}>+</button>
        </div>
        <div class="thread-list">
          {#each chat.threads as t (t.id)}
            <div class="thread" class:active={t.id === chat.activeThreadId}
                 role="button" tabindex="0"
                 onclick={() => onPickThread(t.id)}
                 onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void onPickThread(t.id); } }}>
              <div class="t-title" title={t.title}>
                {#if chat.runningThreadId === t.id}<span class="t-tag run">▶ 运行中</span>{:else if chat.queuedThreadIds.includes(t.id)}<span class="t-tag queued">⏳ 排队中</span>{/if}
                {t.title || "(未命名)"}
              </div>
              <div class="t-meta">
                <span>{fmtTime(t.updated_ms)}</span>
                {#if t.task_count}<span>· {t.task_count} 任务</span>{/if}
              </div>
              <button class="t-del" title="删除" onclick={(e) => onDeleteThread(e, t.id)}>✕</button>
            </div>
          {/each}
          {#if !chat.threads.length}
            <div class="empty">还没有对话，点击 + 新建</div>
          {/if}
        </div>
      </aside>
    {/if}

    <main>
      <div class="chat" bind:this={scrollEl}>
        {#each chat.items as it, i (i)}
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
            <div class="img-card">
              <div class="img-meta">📷 step {it.step} · {it.level}</div>
              {#if it.dataUrl}
                <img src={it.dataUrl} alt="step {it.step}"
                     onclick={() => (lightbox = it.dataUrl ?? null)} />
              {:else}
                <div class="img-loading">加载中…</div>
              {/if}
            </div>
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
    </main>
  </div>
</div>

{#if lightbox}
  <div class="lightbox" onclick={() => (lightbox = null)} role="presentation">
    <img src={lightbox} alt="screenshot" />
  </div>
{/if}

<style>
  :global(body) { margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f7f7f8; color: #222; }
  .app { display: flex; flex-direction: column; height: 100vh; }
  header {
    display: flex; align-items: center; gap: 1rem;
    padding: 0.6rem 1rem; background: #1f2937; color: #fff;
  }
  .toggle { background: transparent; color: #fff; border: 1px solid #4b5563; width: 1.6rem;
            height: 1.6rem; border-radius: 4px; cursor: pointer; font-size: 1rem; line-height: 1; }
  .title { font-weight: 600; }
  .status { font-size: 0.85rem; opacity: 0.8; }
  .status.on { color: #6ee7b7; opacity: 1; }
  .status.running { color: #fbbf24; opacity: 1; }
  .link { color: #93c5fd; text-decoration: none; font-size: 0.9rem; margin-left: 0.8rem; }
  .link:first-of-type { margin-left: auto; }

  .body { flex: 1; display: flex; min-height: 0; }
  .sidebar { width: 16rem; background: #111827; color: #e5e7eb;
             display: flex; flex-direction: column; border-right: 1px solid #1f2937;
             min-width: 0; overflow: hidden; }
  .side-head { display: flex; align-items: center; padding: 0.6rem 0.8rem; font-size: 0.85rem;
               opacity: 0.85; border-bottom: 1px solid #1f2937; }
  .side-new { margin-left: auto; background: #2563eb; color: #fff; border: 0; border-radius: 4px;
              width: 1.6rem; height: 1.6rem; font-size: 1.1rem; line-height: 1; cursor: pointer; }
  .thread-list { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 0.3rem; min-width: 0; }
  .thread { display: block; width: 100%; text-align: left; background: transparent; color: inherit;
            border: 0; padding: 0.5rem 0.6rem; border-radius: 6px; cursor: pointer; position: relative;
            margin-bottom: 0.15rem; }
  .thread:hover { background: #1f2937; }
  .thread.active { background: #2563eb; }
  .t-title { font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
             padding-right: 1.2rem; }
  .t-meta { font-size: 0.7rem; opacity: 0.65; margin-top: 0.15rem; display: flex; gap: 0.3rem; }
  .t-tag { display: inline-block; font-size: 0.65rem; padding: 0.05rem 0.3rem; border-radius: 3px;
           margin-right: 0.3rem; vertical-align: middle; opacity: 0.95; }
  .t-tag.run { background: rgba(34,197,94,0.25); color: #22c55e; }
  .t-tag.queued { background: rgba(234,179,8,0.25); color: #eab308; }
  .t-del { position: absolute; right: 0.3rem; top: 0.4rem; background: transparent; color: inherit;
           border: 0; width: 1.2rem; height: 1.2rem; cursor: pointer; opacity: 0; border-radius: 3px;
           font-size: 0.75rem; }
  .thread:hover .t-del { opacity: 0.7; }
  .t-del:hover { background: rgba(239, 68, 68, 0.6); opacity: 1; }
  .empty { padding: 0.8rem; font-size: 0.78rem; opacity: 0.6; text-align: center; }

  main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .chat { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
  .bubble { max-width: 80%; padding: 0.55rem 0.75rem; border-radius: 8px; }
  .bubble .role { font-size: 0.7rem; opacity: 0.6; margin-bottom: 0.2rem; }
  .bubble.user { align-self: flex-end; background: #2563eb; color: #fff; }
  .bubble.assistant { align-self: flex-start; background: #fff; border: 1px solid #e5e7eb; }
  .text { white-space: pre-wrap; }
  .tool { font-family: ui-monospace, Consolas, monospace; font-size: 0.78rem;
          background: #fef3c7; border: 1px solid #fde68a; padding: 0.3rem 0.5rem;
          border-radius: 6px; align-self: flex-start; max-width: 95%; word-break: break-all; }
  .badge { background: #92400e; color: #fff; padding: 0 0.4rem; border-radius: 4px; margin-right: 0.3rem; }
  .action { font-weight: 600; color: #92400e; }
  .args { color: #444; margin: 0 0.4rem; }
  .ok { color: #047857; }
  .err { color: #b91c1c; }
  .pending { color: #92400e; opacity: 0.7; }

  .img-card { align-self: flex-start; max-width: 90%; }
  .img-meta { font-size: 0.72rem; color: #6b7280; margin-bottom: 0.2rem; }
  .img-card img { max-width: 100%; max-height: 14rem; border: 1px solid #e5e7eb;
                  border-radius: 6px; cursor: zoom-in; display: block; }
  .img-loading { font-size: 0.75rem; color: #9ca3af; padding: 0.4rem 0.6rem;
                 background: #f3f4f6; border-radius: 6px; }

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

  .lightbox { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.85);
              display: flex; align-items: center; justify-content: center;
              z-index: 999; cursor: zoom-out; }
  .lightbox img { max-width: 95vw; max-height: 95vh; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }
</style>
