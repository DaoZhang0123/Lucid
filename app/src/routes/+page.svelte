<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount, onDestroy } from "svelte";
  import { _ } from "svelte-i18n";
  import { open as openDialog } from "@tauri-apps/plugin-dialog";
  import { getCurrentWebview } from "@tauri-apps/api/webview";
  import type { UnlistenFn } from "@tauri-apps/api/event";
  import { appConfirm } from "$lib/appConfirm.svelte";
  import {
    chat,
    ensureChatListeners,
    startTask,
    cancelTask,
    newThread as storeNewThread,
    openThread,
    deleteThread,
    refreshThreadList,
    type FileRef,
  } from "$lib/chatStore.svelte";

  let instruction = $state("");
  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);
  let scrollEl: HTMLDivElement | undefined = $state();
  let sidebarOpen = $state(true);
  let lightbox = $state<string | null>(null);
  // 附件 chip 状态。发送后清空。图片 chip 异步填充 previewUrl（走 read_attachment_b64 命令）。
  type ChipRef = FileRef & { previewUrl?: string };
  let attachments = $state<ChipRef[]>([]);
  let dragActive = $state(false);
  // 一次性同步：sidecar ready 事件里带了 config 的 max_steps（chat.totalSteps），
  // 之后用户在输入框改的值不再被覆盖。
  let _maxStepsSyncedFromSidecar = false;
  $effect(() => {
    if (!_maxStepsSyncedFromSidecar && chat.totalSteps > 0) {
      maxSteps = chat.totalSteps;
      _maxStepsSyncedFromSidecar = true;
    }
  });

  $effect(() => {
    void chat.items.length;
    queueMicrotask(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });

  onMount(() => {
    void ensureChatListeners();
  });

  // ---------------- Attachments: paste / drag-drop / 📎 button ----------------

  const IMG_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp"]);

  function extOf(p: string): string {
    const i = p.lastIndexOf(".");
    return i >= 0 ? p.slice(i + 1).toLowerCase() : "";
  }
  function baseOf(p: string): string {
    const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
    return i >= 0 ? p.slice(i + 1) : p;
  }
  function kindOf(path: string): "image" | "file" {
    return IMG_EXTS.has(extOf(path)) ? "image" : "file";
  }
  function pushRef(path: string, name?: string, kindOverride?: "image" | "file" | "folder"): void {
    const trimmed = path.trim();
    if (!trimmed) return;
    if (attachments.some((a) => a.path === trimmed)) return;
    const k = kindOverride ?? kindOf(trimmed);
    const ref: ChipRef = { name: name?.trim() || baseOf(trimmed), path: trimmed, kind: k };
    attachments = [...attachments, ref];
    if (k === "image") {
      // 异步取 base64 缩略图（asset:// 默认未启用，走 b64 命令）
      const idx = attachments.length - 1;
      void invoke<string>("read_attachment_b64", { path: trimmed })
        .then((url) => {
          attachments = attachments.map((a, i) =>
            i === idx && a.path === trimmed ? { ...a, previewUrl: url } : a);
        })
        .catch(() => { /* 预览失败不致命，chip 退化成 🖼️ 图标 */ });
    }
  }
  function removeAttachment(idx: number): void {
    attachments = attachments.filter((_, i) => i !== idx);
  }

  async function pickFiles() {
    try {
      const sel = await openDialog({ multiple: true, directory: false });
      if (!sel) return;
      const arr = Array.isArray(sel) ? sel : [sel];
      for (const p of arr) if (typeof p === "string") pushRef(p);
    } catch (e) {
      chat.items = [...chat.items, { kind: "system", text: `选择文件失败：${e}` }];
    }
  }

  async function pickFolders() {
    try {
      // Tauri / Windows: a single dialog can pick files OR directories, not both,
      // hence the separate 📁 button. Folders are sent to the model with kind="folder"
      // so the [Attached files] block shows [folder] and the model knows to use
      // run_shell / dir instead of load_screenshot.
      const sel = await openDialog({ multiple: true, directory: true });
      if (!sel) return;
      const arr = Array.isArray(sel) ? sel : [sel];
      for (const p of arr) if (typeof p === "string") pushRef(p, undefined, "folder");
    } catch (e) {
      chat.items = [...chat.items, { kind: "system", text: `选择文件夹失败：${e}` }];
    }
  }

  async function onPaste(e: ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items || !items.length) return;
    let saved = 0;
    for (const it of Array.from(items)) {
      if (it.kind !== "file") continue;
      if (!it.type.startsWith("image/")) continue;
      const f = it.getAsFile();
      if (!f) continue;
      e.preventDefault();
      try {
        const buf = await f.arrayBuffer();
        const ext = (f.type.split("/")[1] || "png").split(";")[0];
        const name = f.name && f.name !== "image.png" ? f.name : `paste.${ext}`;
        const res: any = await invoke("save_inbox_image", { name, bytes: Array.from(new Uint8Array(buf)) });
        if (res?.path) {
          pushRef(res.path, baseOf(res.path));
          saved++;
        }
      } catch (err) {
        chat.items = [...chat.items, { kind: "system", text: `保存粘贴图片失败：${err}` }];
      }
    }
    if (saved) {
      // Tiny visual ping so the user knows something happened.
      // (No system-message spam — the chip itself is the confirmation.)
    }
  }

  // Tauri 2 drag-drop: paths arrive on the webview event, not via DOM `drop`.
  let unlistenDragDrop: UnlistenFn | null = null;
  onMount(() => {
    void (async () => {
      try {
        const wv = getCurrentWebview();
        unlistenDragDrop = await wv.onDragDropEvent((ev: any) => {
          const t = ev.payload?.type;
          if (t === "over" || t === "enter") {
            dragActive = true;
          } else if (t === "drop") {
            dragActive = false;
            const paths: string[] = ev.payload?.paths ?? [];
            for (const p of paths) pushRef(p);
          } else if (t === "leave" || t === "cancel") {
            dragActive = false;
          }
        });
      } catch {
        // browser dev mode without webview drag-drop — fall back to DOM drop on the chat container.
      }
    })();
  });
  onDestroy(() => {
    unlistenDragDrop?.();
    unlistenDragDrop = null;
  });

  async function start() {
    const text = instruction.trim();
    if (!text) return;
    instruction = "";
    const refs = attachments.map(({ name, path, kind }) => ({ name, path, kind }));
    attachments = [];
    await startTask(text, autonomy, maxSteps, refs);
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
      chat.items = [...chat.items, { kind: "system", text: $_("footer.autonomy_switch_failed", { values: { err: String(e) } }) }];
    }
  }

  async function onPickThread(id: string) {
    if (id === chat.activeThreadId) return;
    await openThread(id);
  }

  async function onDeleteThread(e: MouseEvent, id: string) {
    e.stopPropagation();
    if (!(await appConfirm($_("sidebar.thread_delete_confirm"), { danger: true }))) return;
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

  // Thread ids look like `thread-20260508-110047-d381f6-⏰_每日新闻简介` or
  // `20260508-110047-d381f6` (no prefix). Surface the 6-hex random suffix
  // so the user can quickly correlate a sidebar row with files on disk
  // (`%LOCALAPPDATA%\dev.otterscope\logs\threads\thread-*-<hex>-*\`).
  function extractHex(id?: string): string {
    if (!id) return "";
    const m = id.match(/-([0-9a-f]{6})(?:-|$)/);
    return m ? m[1] : "";
  }

  // -------- Sidebar pagination --------
  const THREAD_PAGE_SIZE = 10;
  let threadPage = $state(0);
  let threadPageCount = $derived(Math.max(1, Math.ceil(chat.threads.length / THREAD_PAGE_SIZE)));
  $effect(() => {
    // Clamp when threads list shrinks (e.g. after delete).
    if (threadPage > threadPageCount - 1) threadPage = threadPageCount - 1;
    if (threadPage < 0) threadPage = 0;
  });
  let pagedThreads = $derived(
    chat.threads.slice(threadPage * THREAD_PAGE_SIZE, (threadPage + 1) * THREAD_PAGE_SIZE)
  );
  // Always make the active thread visible — jump to its page if the user
  // picked / opened a thread that lives on a different page. We remember the
  // id we last auto-jumped for so that subsequent manual page clicks (which
  // don't change `activeThreadId`) aren't yanked back to page 0.
  let lastJumpedActive: string | null = null;
  $effect(() => {
    const aid = chat.activeThreadId;
    if (!aid) { lastJumpedActive = null; return; }
    if (aid === lastJumpedActive) return;
    const idx = chat.threads.findIndex((t) => t.id === aid);
    if (idx < 0) return;
    const p = Math.floor(idx / THREAD_PAGE_SIZE);
    if (p !== threadPage) threadPage = p;
    lastJumpedActive = aid;
  });
</script>

<div class="app">
  <header>
    <div class="title">{$_("app.title")}</div>
    <div class="status" class:on={chat.sidecarReady} class:running={chat.running}>
      {#if chat.running}
        {#if chat.queuedThreadIds.length}
          {$_("header.status_running_with_queue", { values: { current: chat.currentStep, total: chat.totalSteps, queued: chat.queuedThreadIds.length } })}
        {:else}
          {$_("header.status_running", { values: { current: chat.currentStep, total: chat.totalSteps } })}
        {/if}
      {:else if chat.sidecarReady}
        {#if chat.queuedThreadIds.length}
          {$_("header.status_ready_with_queue", { values: { queued: chat.queuedThreadIds.length } })}
        {:else}
          {$_("header.status_ready")}
        {/if}
      {:else}
        {$_("header.status_disconnected")}
      {/if}
    </div>
    <a class="link" href="/templates">{$_("header.nav_templates")}</a>
    <a class="link" href="/schedules">{$_("header.nav_schedules")}</a>
    <a class="link" href="/memory">{$_("header.nav_memory")}</a>
    <a class="link" href="/tools">{$_("header.nav_tools")}</a>
    <a class="link" href="/doze">{$_("header.nav_doze")}</a>
    <a class="link" href="/settings">{$_("header.nav_settings")}</a>
  </header>

  <div class="body">
    {#if !sidebarOpen}
      <div class="edge-reveal" aria-hidden="true">
        <button class="edge-toggle" title={$_("header.sidebar_show")}
                onclick={() => { sidebarOpen = true; void refreshThreadList(); }}>
          ›
        </button>
      </div>
    {/if}
    {#if sidebarOpen}
      <aside class="sidebar">
        <div class="side-head">
          <button class="side-toggle" title={$_("header.sidebar_hide")}
                  onclick={() => { sidebarOpen = false; }}>
            ‹
          </button>
          <span class="side-heading">{$_("sidebar.heading")}</span>
          <button class="side-new" title={$_("sidebar.new_thread_title")} onclick={newThread}>+</button>
        </div>
        <div class="thread-list">
          {#each pagedThreads as t (t.id)}
            <div class="thread" class:active={t.id === chat.activeThreadId}
                 role="button" tabindex="0"
                 onclick={() => onPickThread(t.id)}
                 onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); void onPickThread(t.id); } }}>
              <div class="t-title" title={t.title}>
                {#if chat.runningThreadId === t.id}<span class="t-tag run">{$_("sidebar.thread_running")}</span>{:else if chat.queuedThreadIds.includes(t.id)}<span class="t-tag queued">{$_("sidebar.thread_queued")}</span>{/if}
                {t.title || $_("sidebar.thread_unnamed")}
              </div>
              <div class="t-meta">
                {#if extractHex(t.id)}<span class="t-id" title={t.id}>{extractHex(t.id)}</span>{/if}
                <span>{fmtTime(t.updated_ms)}</span>
                {#if t.task_count}<span>{$_("sidebar.thread_task_count", { values: { n: t.task_count } })}</span>{/if}
              </div>
              <button class="t-del" title={$_("sidebar.thread_delete_title")} onclick={(e) => onDeleteThread(e, t.id)}>✕</button>
            </div>
          {/each}
          {#if !chat.threads.length}
            <div class="empty">{$_("sidebar.empty")}</div>
          {/if}
        </div>
        {#if threadPageCount > 1}
          <div class="pager">
            <button class="pg-btn" onclick={() => { if (threadPage > 0) threadPage -= 1; }} disabled={threadPage === 0} title="Previous page">‹</button>
            <span class="pg-info">{threadPage + 1} / {threadPageCount}</span>
            <button class="pg-btn" onclick={() => { if (threadPage < threadPageCount - 1) threadPage += 1; }} disabled={threadPage >= threadPageCount - 1} title="Next page">›</button>
          </div>
        {/if}
      </aside>
    {/if}

    <main>
      <div class="chat" bind:this={scrollEl}>
        {#each chat.items as it, i (i)}
          {#if it.kind === "user"}
            <div class="bubble user"><div class="role">{$_("chat.role_user")}</div><div class="text">{it.text}</div></div>
          {:else if it.kind === "assistant"}
            <div class="bubble assistant"><div class="role">{$_("chat.role_assistant", { values: { step: it.step ?? $_("chat.step_unknown") } })}</div><div class="text">{it.text}</div></div>
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
            <div class="img-card" class:from-user={it.fromUser}>
              <div class="img-meta">{$_("chat.image_meta", { values: { step: it.step, level: it.level } })}</div>
              {#if it.dataUrl}
                <img src={it.dataUrl} alt={$_("chat.image_alt", { values: { step: it.step } })}
                     onclick={() => (lightbox = it.dataUrl ?? null)} />
              {:else}
                <div class="img-loading">{$_("chat.image_loading")}</div>
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
        {#if attachments.length}
          <div class="chip-row">
            {#each attachments as a, i (a.path)}
              <div class="chip chip-{a.kind}" title={a.path}>
                {#if a.kind === 'image' && a.previewUrl}
                  <img src={a.previewUrl} alt={a.name}
                       onclick={() => (lightbox = a.previewUrl ?? null)} />
                {:else}
                  <span class="chip-icon">{a.kind === 'image' ? '🖼️' : a.kind === 'folder' ? '📁' : '📄'}</span>
                {/if}
                <span class="chip-name">{a.name}</span>
                <button type="button" class="chip-x" title="移除"
                        onclick={() => removeAttachment(i)}>✕</button>
              </div>
            {/each}
          </div>
        {/if}
        <div class="controls">
          <label>
            {$_("footer.autonomy_label")}
            <select bind:value={autonomy} onchange={setAutonomy}>
              <option value="full">{$_("footer.autonomy_full")}</option>
              <option value="confirm_critical">{$_("footer.autonomy_confirm_critical")}</option>
              <option value="confirm_each">{$_("footer.autonomy_confirm_each")}</option>
            </select>
          </label>
          <label>
            {$_("footer.max_steps_label")}
            <input type="number" min="1" max="200" bind:value={maxSteps} />
          </label>
          <button class="cancel" onclick={cancel} disabled={!chat.running}>{$_("footer.cancel_button")}</button>
        </div>
        <form onsubmit={(e) => { e.preventDefault(); start(); }}>
          <button type="button" class="attach" title={$_("footer.attach_title")}
                  onclick={pickFiles}>
            📎
          </button>
          <button type="button" class="attach" title={$_("footer.attach_folder_title")}
                  onclick={pickFolders}>
            📁
          </button>
          <textarea
            placeholder={$_("footer.input_placeholder")}
            rows="2"
            bind:value={instruction}
            onpaste={onPaste}
            onkeydown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(); } }}
          ></textarea>
          <button type="submit" disabled={!instruction.trim() && attachments.length === 0}>{$_("footer.send_button")}</button>
        </form>
        {#if chat.sidecarStderr.length}
          <details class="stderr">
            <summary>{$_("footer.stderr_summary", { values: { n: chat.sidecarStderr.length } })}</summary>
            <pre>{chat.sidecarStderr.join("\n")}</pre>
          </details>
        {/if}
      </footer>
    </main>
  </div>
</div>

{#if dragActive}
  <div class="drop-overlay">
    <div class="drop-overlay-inner">{$_("footer.drop_hint")}</div>
  </div>
{/if}

{#if lightbox}
  <div class="lightbox" onclick={() => (lightbox = null)} role="presentation">
    <img src={lightbox} alt={$_("chat.lightbox_alt")} />
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

  .body { flex: 1; display: flex; min-height: 0; position: relative; }
  .edge-reveal { position: absolute; left: 0; top: 0; bottom: 0; width: 14px; z-index: 5; }
  .edge-toggle {
    position: absolute; left: 6px; top: 50%; transform: translateY(-50%);
    width: 1.6rem; height: 2.4rem; border-radius: 0 6px 6px 0;
    background: #1f2937; color: #fff; border: 1px solid #4b5563; border-left: 0;
    cursor: pointer; font-size: 1rem; line-height: 1; padding: 0;
    opacity: 0; pointer-events: none; transition: opacity 0.15s;
    box-shadow: 2px 2px 6px rgba(0, 0, 0, 0.25);
  }
  .edge-reveal:hover .edge-toggle,
  .edge-toggle:focus-visible {
    opacity: 1; pointer-events: auto;
  }
  .edge-toggle:hover { background: #374151; }
  .sidebar { width: 16rem; background: #111827; color: #e5e7eb;
             display: flex; flex-direction: column; border-right: 1px solid #1f2937;
             min-width: 0; overflow: hidden; }
  .side-head { display: flex; align-items: center; gap: 0.4rem; padding: 0.6rem 0.8rem; font-size: 0.85rem;
               opacity: 0.85; border-bottom: 1px solid #1f2937; }
  .side-toggle { background: transparent; color: #fff; border: 1px solid #4b5563; width: 1.6rem;
                 height: 1.6rem; border-radius: 4px; cursor: pointer; font-size: 1rem; line-height: 1;
                 padding: 0; flex: 0 0 auto; }
  .side-toggle:hover { background: #1f2937; }
  .side-heading { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center; }
  .side-new { background: #2563eb; color: #fff; border: 0; border-radius: 4px;
              width: 1.6rem; height: 1.6rem; font-size: 1.1rem; line-height: 1; cursor: pointer;
              flex: 0 0 auto; }
  .thread-list { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 0.3rem; min-width: 0; }
  .pager { display: flex; align-items: center; justify-content: space-between; gap: 0.4rem;
           padding: 0.35rem 0.6rem; border-top: 1px solid #1f2937; background: #0b1220;
           font-size: 0.72rem; color: #cbd5e1; }
  .pg-btn { background: #1f2937; color: #e5e7eb; border: 1px solid #374151; border-radius: 4px;
            width: 1.6rem; height: 1.4rem; line-height: 1; cursor: pointer; font-size: 0.9rem; padding: 0; }
  .pg-btn:hover:not(:disabled) { background: #374151; }
  .pg-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .pg-info { font-variant-numeric: tabular-nums; opacity: 0.85; }
  .thread { display: block; width: 100%; text-align: left; background: transparent; color: inherit;
            border: 0; padding: 0.5rem 0.6rem; border-radius: 6px; cursor: pointer; position: relative;
            margin-bottom: 0.15rem; }
  .thread:hover { background: #1f2937; }
  .thread.active { background: #2563eb; }
  .t-title { font-size: 0.85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
             padding-right: 1.2rem; }
  .t-meta { font-size: 0.7rem; opacity: 0.65; margin-top: 0.15rem; display: flex; gap: 0.3rem; align-items: center; }
  .t-id { font-family: ui-monospace, Consolas, monospace; font-size: 0.65rem;
          background: rgba(148, 163, 184, 0.18); color: #cbd5e1; padding: 0.02rem 0.3rem;
          border-radius: 3px; letter-spacing: 0.02em; }
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
  .img-card.from-user { align-self: flex-end; max-width: 90%; }
  .img-card.from-user .img-meta { text-align: right; color: #1d4ed8; }
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
  form button.attach { padding: 0 0.7rem; background: #fff; color: #475569;
                       border: 1px solid #cbd5e1; font-size: 1.1rem; line-height: 1; }
  form button.attach:hover:not(:disabled) { background: #f1f5f9; color: #1e293b; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }
  .chip { display: inline-flex; align-items: center; gap: 0.4rem; padding: 0.25rem 0.4rem 0.25rem 0.3rem;
          border: 1px solid #cbd5e1; border-radius: 6px; background: #f8fafc; font-size: 0.8rem;
          max-width: 18rem; }
  .chip.chip-image { padding-left: 0.2rem; }
  .chip img { width: 2.4rem; height: 2.4rem; object-fit: cover; border-radius: 4px; cursor: zoom-in;
              border: 1px solid #e2e8f0; flex: 0 0 auto; }
  .chip-icon { width: 1.6rem; text-align: center; font-size: 1.1rem; }
  .chip-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 12rem; }
  .chip-x { background: transparent; border: 0; color: #64748b; cursor: pointer; padding: 0 0.2rem;
            font-size: 0.85rem; line-height: 1; }
  .chip-x:hover { color: #b91c1c; }
  .drop-overlay { position: fixed; inset: 0; background: rgba(37, 99, 235, 0.12);
                  border: 4px dashed #2563eb; pointer-events: none; z-index: 998;
                  display: flex; align-items: center; justify-content: center; }
  .drop-overlay-inner { background: rgba(255,255,255,0.95); padding: 1rem 1.6rem; border-radius: 8px;
                        font-size: 1.1rem; color: #1e293b; box-shadow: 0 4px 16px rgba(0,0,0,0.15); }
  .stderr { margin-top: 0.4rem; font-size: 0.75rem; color: #6b7280; }
  .stderr pre { background: #f3f4f6; padding: 0.4rem; max-height: 8rem; overflow: auto; }

  .lightbox { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.85);
              display: flex; align-items: center; justify-content: center;
              z-index: 999; cursor: zoom-out; }
  .lightbox img { max-width: 95vw; max-height: 95vh; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }
</style>
