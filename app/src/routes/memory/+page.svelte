<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";
  import { appConfirm } from "$lib/appConfirm.svelte";

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
    if (!(await appConfirm($_("memory.clear_confirm"), { danger: true }))) return;
    try {
      await invoke("memory_clear");
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{$_("memory.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("memory.heading")}</h1>
  </header>

  <p class="hint">
    {@html $_("memory.hint")}
  </p>
  <p class="meta">
    {$_("memory.status_label")} {enabled ? $_("memory.status_enabled") : $_("memory.status_disabled")}<br/>
    {$_("memory.path_label")} <code>{path}</code>
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <textarea bind:value={text} rows="22" disabled={!enabled}></textarea>

  <div class="actions">
    <button onclick={save} disabled={saving || !enabled}>{saving ? $_("memory.saving_button") : $_("memory.save_button")}</button>
    <button class="danger" onclick={clear} disabled={!enabled}>{$_("memory.clear_button")}</button>
    <button onclick={load}>{$_("memory.reload_button")}</button>
    {#if savedAt}<span class="ok">{$_("memory.saved_at", { values: { at: savedAt } })}</span>{/if}
  </div>
</div>

<style>
  .page { max-width: 56rem; margin: 0; padding: 1rem 1.5rem; font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
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
