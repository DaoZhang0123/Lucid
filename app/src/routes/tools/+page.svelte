<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";

  type AppItem = { slug: string; title: string; file: string; lines: number; has_user_entries: boolean };

  // "global" = tools.md ; otherwise app slug.
  let scope = $state<string>("global");

  // Global state
  let enabled = $state(true);
  let path = $state("");
  let text = $state("");
  let saving = $state(false);
  let savedAt = $state("");
  let err = $state("");

  // App-specific
  let apps = $state<AppItem[]>([]);
  let appsDir = $state("");
  let newAppSlug = $state("");

  // 手动追加一条
  let newTip = $state("");
  let newKind = $state<"success" | "failure" | "tip">("tip");

  async function loadApps() {
    try {
      const r = await invoke<any>("app_tips_list");
      apps = (r.items ?? []) as AppItem[];
      appsDir = r.dir ?? "";
    } catch (e) {
      err = String(e);
    }
  }

  async function load() {
    err = "";
    savedAt = "";
    try {
      if (scope === "global") {
        const r = await invoke<any>("tools_read");
        enabled = !!r.enabled;
        path = r.path ?? "";
        text = r.text ?? "";
      } else {
        const r = await invoke<any>("app_tips_read", { app: scope });
        path = r.path ?? "";
        text = r.text ?? "";
        // enabled flag piggybacks on the global tools toggle
      }
      await loadApps();
    } catch (e) {
      err = String(e);
    }
  }

  async function save() {
    saving = true;
    err = "";
    try {
      if (scope === "global") {
        await invoke("tools_write", { text });
      } else {
        await invoke("app_tips_write", { app: scope, text });
      }
      savedAt = new Date().toLocaleTimeString();
      await loadApps();
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
      if (scope === "global") {
        await invoke("tools_append", { text: t, kind: newKind, source: "user" });
      } else {
        await invoke("app_tips_append", { app: scope, text: t, kind: newKind, source: "user" });
      }
      newTip = "";
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function reset() {
    const msg = scope === "global"
      ? $_("tools_page.reset_confirm")
      : `Reset tips/${scope}.md to its built-in seed? Custom entries will be lost.`;
    if (!confirm(msg)) return;
    try {
      if (scope === "global") {
        await invoke("tools_reset");
      } else {
        await invoke("app_tips_reset", { app: scope });
      }
      await load();
    } catch (e) {
      err = String(e);
    }
  }

  async function selectScope(s: string) {
    scope = s;
    await load();
  }

  async function createNewApp() {
    const slug = newAppSlug.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
    if (!slug) return;
    try {
      // Write an empty body — backend will add header + (optionally) seed.
      await invoke("app_tips_write", { app: slug, text: "" });
      newAppSlug = "";
      scope = slug;
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

  <div class="tabs">
    <button class="tab" class:active={scope === "global"} onclick={() => selectScope("global")}>
      <span class="tab-title">Global · tools.md</span>
      <span class="tab-sub">always-on</span>
    </button>
    {#each apps as a (a.slug)}
      <button class="tab" class:active={scope === a.slug} onclick={() => selectScope(a.slug)}>
        <span class="tab-title">{a.title}</span>
        <span class="tab-sub">
          tips/{a.slug}.md · {a.lines}{a.has_user_entries ? " ✎" : ""}
        </span>
      </button>
    {/each}
    <div class="tab newapp">
      <input
        type="text"
        placeholder="new app slug"
        bind:value={newAppSlug}
        onkeydown={(e) => { if (e.key === "Enter") createNewApp(); }}
      />
      <button onclick={createNewApp} disabled={!newAppSlug.trim()}>+ Add</button>
    </div>
  </div>

  <p class="meta">
    {#if scope === "global"}
      {$_("tools_page.status_label")} {enabled ? $_("tools_page.status_enabled") : $_("tools_page.status_disabled")}<br/>
      {$_("tools_page.path_label")} <code>{path}</code>
    {:else}
      Scope: <code>{scope}</code> — only loaded when the agent calls <code>load_app_tips(app="{scope}")</code> or <code>launch_app(name="{scope}")</code>.<br/>
      Path: <code>{path}</code>
    {/if}
  </p>

  {#if err}<p class="err">{err}</p>{/if}

  <div class="quick">
    <input
      type="text"
      placeholder={scope === "global" ? $_("tools_page.append_placeholder") : `Append a tip to ${scope}.md`}
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

  .tabs { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.6rem 0 0.4rem; }
  .tab { display: flex; flex-direction: column; align-items: flex-start; gap: 0.1rem;
         padding: 0.35rem 0.7rem; background: #f3f4f6; border: 1px solid #e5e7eb;
         border-radius: 6px; cursor: pointer; font: inherit; min-width: 8rem; }
  .tab:hover { background: #e5e7eb; }
  .tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
  .tab-title { font-weight: 600; font-size: 0.85rem; }
  .tab-sub { font-size: 0.7rem; opacity: 0.75; }
  .tab.newapp { background: #fff; padding: 0.2rem 0.3rem; flex-direction: row; align-items: center; gap: 0.25rem; cursor: default; }
  .tab.newapp input { width: 7rem; padding: 0.25rem 0.4rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; font-size: 0.8rem; }
  .tab.newapp button { padding: 0.25rem 0.5rem; background: #10b981; color: #fff; border: 0; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .tab.newapp button:disabled { opacity: 0.5; cursor: not-allowed; }

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
