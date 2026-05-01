<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

  let enabled = $state(true);
  let path = $state("");
  let text = $state("");
  let saving = $state(false);
  let savedAt = $state("");
  let err = $state("");

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("memory_read");
      enabled = !!r.enabled;
      path = r.path ?? "";
      text = r.text ?? "";
    } catch (e) {
      err = String(e);
    }
  }

  async function save() {
    saving = true;
    err = "";
    try {
      await invoke("memory_write", { text });
      savedAt = new Date().toLocaleTimeString();
    } catch (e) {
      err = String(e);
    } finally {
      saving = false;
    }
  }

  async function clear() {
    if (!confirm("清空整份 memory.md？此操作不可撤销。")) return;
    try {
      await invoke("memory_clear");
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  onMount(load);
</script>

<div class="page">
  <header>
    <a class="back" href="/">‹ 返回</a>
    <h1>长期记忆 · memory.md</h1>
  </header>

  <p class="hint">
    每次任务起手会把这里的内容追加到 system prompt 末尾。Agent 也能用 <code>remember</code>
    工具主动写入。<strong>不要</strong>在这里写密码 / token 等敏感信息。
  </p>
  <p class="meta">
    状态：{enabled ? "已启用" : "已禁用（在 config.toml [memory] 里开启）"}<br/>
    路径：<code>{path}</code>
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <textarea bind:value={text} rows="22" disabled={!enabled}></textarea>

  <div class="actions">
    <button onclick={save} disabled={saving || !enabled}>{saving ? "保存中…" : "保存"}</button>
    <button class="danger" onclick={clear} disabled={!enabled}>清空</button>
    <button onclick={load}>重新加载</button>
    {#if savedAt}<span class="ok">已保存 {savedAt}</span>{/if}
  </div>
</div>

<style>
  .page { max-width: 56rem; margin: 0 auto; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 0.5rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .meta { font-size: 0.78rem; color: #6b7280; }
  .err { color: #b91c1c; }
  textarea { width: 100%; box-sizing: border-box; font: 13px ui-monospace, Consolas, monospace;
             padding: 0.6rem; border: 1px solid #d1d5db; border-radius: 6px; }
  textarea:disabled { background: #f3f4f6; color: #9ca3af; }
  .actions { display: flex; gap: 0.5rem; margin-top: 0.6rem; align-items: center; }
  .actions button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0;
                    border-radius: 4px; cursor: pointer; }
  .actions button:disabled { opacity: 0.5; cursor: not-allowed; }
  .actions button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .ok { color: #047857; font-size: 0.85rem; }
  code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; }
</style>
