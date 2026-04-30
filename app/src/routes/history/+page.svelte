<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

  type Run = { name: string; mtime_ms: number; path: string };
  let runs = $state<Run[]>([]);
  let selected = $state<string | null>(null);
  let detail = $state<any | null>(null);
  let images = $state<Record<string, string>>({});
  let activeImage = $state<string | null>(null);

  async function refresh() {
    runs = (await invoke<Run[]>("list_runs")) ?? [];
  }

  async function open(name: string) {
    selected = name;
    detail = null;
    images = {};
    activeImage = null;
    detail = await invoke("read_run", { name });
  }

  async function showImage(file: string) {
    if (!selected) return;
    if (!images[file]) {
      images[file] = await invoke<string>("read_image_b64", { runName: selected, fileName: file });
    }
    activeImage = file;
  }

  function fmtTime(ms: number) {
    if (!ms) return "";
    return new Date(ms).toLocaleString();
  }

  onMount(refresh);
</script>

<div class="page">
  <aside>
    <div class="bar">
      <a href="/">← 返回</a>
      <button onclick={refresh}>刷新</button>
    </div>
    <ul>
      {#each runs as r}
        <li class:active={selected === r.name}>
          <button onclick={() => open(r.name)}>
            <div class="name">{r.name}</div>
            <div class="time">{fmtTime(r.mtime_ms)}</div>
          </button>
        </li>
      {/each}
      {#if !runs.length}
        <li class="empty">还没有运行记录</li>
      {/if}
    </ul>
  </aside>

  <main>
    {#if !detail}
      <div class="placeholder">从左侧选一次运行查看时间线</div>
    {:else}
      <h2>{detail.name}</h2>
      <div class="dir">{detail.dir}</div>

      <section class="timeline">
        {#each (detail.steps ?? []) as step}
          <div class="step">
            {#if step.step}
              <div class="step-head">step {step.step}</div>
            {/if}
            {#if step.assistant_text}
              <div class="text">{step.assistant_text}</div>
            {/if}
            {#if step.tools}
              {#each step.tools as t}
                <div class="tool">→ {t.action} <code>{JSON.stringify(t.args)}</code> · {t.result}</div>
              {/each}
            {/if}
            {#if step.post_image}
              <button class="thumb" onclick={() => showImage(step.post_image)}>📷 {step.post_image}</button>
            {/if}
            {#if step.verify_image}
              <button class="thumb" onclick={() => showImage(`${step.verify_image}.png`)}>🔍 {step.verify_image}</button>
            {/if}
            {#if step.save_dialog_hint}
              <div class="hint">⚠ 触发了保存对话框 sidebar guard</div>
            {/if}
            {#if step.final_text}
              <div class="final">🟢 {step.final_text}</div>
            {/if}
          </div>
        {/each}
      </section>

      {#if activeImage && images[activeImage]}
        <div class="lightbox" onclick={() => (activeImage = null)} role="presentation">
          <img src={images[activeImage]} alt={activeImage} />
        </div>
      {/if}

      <details class="rawlog">
        <summary>run.log</summary>
        <pre>{detail.log}</pre>
      </details>
    {/if}
  </main>
</div>

<style>
  :global(body) { margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  .page { display: grid; grid-template-columns: 280px 1fr; height: 100vh; }
  aside { border-right: 1px solid #e5e7eb; overflow-y: auto; background: #f9fafb; }
  .bar { display: flex; gap: 0.5rem; padding: 0.5rem; align-items: center; border-bottom: 1px solid #e5e7eb; }
  .bar a { text-decoration: none; color: #2563eb; }
  ul { list-style: none; padding: 0; margin: 0; }
  li.active button { background: #dbeafe; }
  li.empty { padding: 1rem; color: #9ca3af; font-style: italic; }
  li button { width: 100%; text-align: left; background: none; border: 0; padding: 0.5rem 0.7rem;
              border-bottom: 1px solid #f3f4f6; cursor: pointer; }
  li button:hover { background: #f3f4f6; }
  .name { font-size: 0.85rem; font-weight: 500; word-break: break-all; }
  .time { font-size: 0.7rem; color: #6b7280; }
  main { overflow-y: auto; padding: 1rem 1.5rem; }
  .placeholder { color: #9ca3af; padding: 3rem; text-align: center; }
  .dir { font-size: 0.75rem; color: #6b7280; margin-bottom: 1rem; word-break: break-all; }
  .timeline { display: flex; flex-direction: column; gap: 0.6rem; }
  .step { background: #fff; border: 1px solid #e5e7eb; border-radius: 6px; padding: 0.6rem 0.8rem; }
  .step-head { font-size: 0.7rem; color: #6b7280; }
  .step .text { white-space: pre-wrap; }
  .step .tool { font-family: ui-monospace, Consolas, monospace; font-size: 0.78rem; color: #92400e; margin-top: 0.3rem; }
  .step .tool code { background: #fef3c7; padding: 0 0.3rem; border-radius: 3px; }
  .thumb { display: inline-block; margin: 0.4rem 0.4rem 0 0; padding: 0.25rem 0.5rem;
           background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 4px; cursor: pointer; font-size: 0.78rem; }
  .hint { color: #92400e; font-size: 0.78rem; margin-top: 0.3rem; }
  .final { color: #065f46; font-weight: 600; margin-top: 0.4rem; }
  .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: flex;
              align-items: center; justify-content: center; z-index: 100; cursor: zoom-out; }
  .lightbox img { max-width: 95vw; max-height: 95vh; box-shadow: 0 0 40px rgba(0,0,0,0.5); }
  .rawlog { margin-top: 1rem; font-size: 0.75rem; }
  .rawlog pre { background: #f3f4f6; padding: 0.6rem; max-height: 14rem; overflow: auto; white-space: pre-wrap; }
</style>
