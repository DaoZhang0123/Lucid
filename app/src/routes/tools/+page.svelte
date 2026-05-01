<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";

  let enabled = $state(true);
  let path = $state("");
  let text = $state("");
  let saving = $state(false);
  let savedAt = $state("");
  let err = $state("");

  // 手动追加一条
  let newTip = $state("");
  let newKind = $state<"success" | "failure" | "tip">("tip");

  async function load() {
    err = "";
    try {
      const r = await invoke<any>("tools_read");
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
      await invoke("tools_write", { text });
      savedAt = new Date().toLocaleTimeString();
    } catch (e) {
      err = String(e);
    } finally {
      saving = false;
    }
  }

  async function appendTip() {
    const t = newTip.trim();
    if (!t) return;
    err = "";
    try {
      await invoke("tools_append", { text: t, kind: newKind, source: "user" });
      newTip = "";
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function reset() {
    if (!confirm($_("tools_page.reset_confirm"))) return;
    try {
      await invoke("tools_reset");
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>{$_("tools_page.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("tools_page.heading")}</h1>
  </header>

  <p class="hint">
    {@html $_("tools_page.hint")}
  </p>
  <p class="meta">
    {$_("tools_page.status_label")} {enabled ? $_("tools_page.status_enabled") : $_("tools_page.status_disabled")}<br/>
    {$_("tools_page.path_label")} <code>{path}</code>
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <div class="quick">
    <input
      type="text"
      placeholder={$_("tools_page.append_placeholder")}
      bind:value={newTip}
      onkeydown={(e) => { if (e.key === "Enter") appendTip(); }}
      disabled={!enabled}
    />
    <select bind:value={newKind} disabled={!enabled}>
      <option value="tip">{$_("tools_page.kind_tip")}</option>
      <option value="success">{$_("tools_page.kind_success")}</option>
      <option value="failure">{$_("tools_page.kind_failure")}</option>
    </select>
    <button onclick={appendTip} disabled={!enabled || !newTip.trim()}>{$_("tools_page.append_button")}</button>
  </div>

  <textarea bind:value={text} rows="22" disabled={!enabled}></textarea>

  <div class="actions">
    <button onclick={save} disabled={saving || !enabled}>{saving ? $_("tools_page.saving_button") : $_("tools_page.save_full_button")}</button>
    <button class="danger" onclick={reset} disabled={!enabled}>{$_("tools_page.reset_button")}</button>
    <button onclick={load}>{$_("tools_page.reload_button")}</button>
    {#if savedAt}<span class="ok">{$_("tools_page.saved_at", { values: { at: savedAt } })}</span>{/if}
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
  .quick { display: flex; gap: 0.4rem; margin: 0.6rem 0; }
  .quick input { flex: 1; padding: 0.4rem 0.6rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; }
  .quick select { padding: 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; }
  .quick button { padding: 0.4rem 1rem; background: #2563eb; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
  .quick button:disabled { opacity: 0.5; cursor: not-allowed; }
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
