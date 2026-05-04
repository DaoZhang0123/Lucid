<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";
  import { appConfirm } from "$lib/appConfirm.svelte";

  type AppItem = { slug: string; title: string; file: string; lines: number; has_user_entries: boolean; is_seeded?: boolean };

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
    if (!(await appConfirm(msg, { danger: true, okLabel: "Reset" }))) return;
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

  async function selectScope(s: string) {
    scope = s;
    await load();
  }

  let currentApp = $derived<AppItem | undefined>(
    scope === "global" ? undefined : apps.find((a) => a.slug === scope)
  );
  let canDeleteApp = $derived(scope !== "global");

  async function deleteApp() {
    if (!canDeleteApp) return;
    const slug = scope;
    const builtin = currentApp?.is_seeded === true;
    const msg = builtin
      ? `Delete built-in app tips tips/${slug}/tips.md? A '.disabled' marker will be created so the seeder won't recreate it. Click "Reset to seed" later to re-enable the built-in defaults.`
      : `Delete user app tips tips/${slug}/tips.md? This is irreversible.`;
    if (!(await appConfirm(msg, { danger: true, okLabel: "Delete" }))) return;
    err = "";
    try {
      const r = await invoke<any>("app_tips_delete", { app: slug });
      if (r && r.ok === false) {
        err = `Delete refused: ${r.reason ?? "unknown"}`;
        return;
      }
      // Pop back to global view and refresh list.
      scope = "global";
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

  <div class="scope-bar">
    <label class="scope-label">
      <span>Scope</span>
      <select class="scope-select" value={scope} onchange={(e) => selectScope((e.currentTarget as HTMLSelectElement).value)}>
        <option value="global">Global · tools.md (always-on)</option>
        {#if apps.length}
          <optgroup label="Per-app tips">
            {#each apps as a (a.slug)}
              <option value={a.slug}>
                {a.title} · tips/{a.slug}.md · {a.lines} lines{a.has_user_entries ? " ✎" : ""}
              </option>
            {/each}
          </optgroup>
        {/if}
      </select>
    </label>
    <div class="newapp">
      <input
        type="text"
        placeholder="new app slug"
        bind:value={newAppSlug}
        onkeydown={(e) => { if (e.key === "Enter") createNewApp(); }}
      />
      <button onclick={createNewApp} disabled={!newAppSlug.trim()}>+ Add app</button>
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
    {#if scope !== "global"}
      <button
        class="danger"
        onclick={deleteApp}
        disabled={!canDeleteApp}
        title={currentApp?.is_seeded
          ? `Delete tips/${scope}/tips.md and write a .disabled marker (built-in app — Reset to seed re-enables it)`
          : `Delete tips/${scope}/tips.md`}
      >Delete app tips</button>
    {/if}
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

  .scope-bar { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
               margin: 0.7rem 0 0.5rem; padding: 0.5rem 0.7rem;
               background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; }
  .scope-label { display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; color: #374151; }
  .scope-label > span { font-weight: 600; }
  .scope-select { padding: 0.35rem 0.5rem; border: 1px solid #d1d5db; border-radius: 4px;
                  font: inherit; min-width: 22rem; background: #fff; cursor: pointer; }
  .newapp { display: flex; align-items: center; gap: 0.3rem; margin-left: auto; }
  .newapp input { width: 9rem; padding: 0.3rem 0.5rem; border: 1px solid #d1d5db; border-radius: 4px; font: inherit; font-size: 0.8rem; }
  .newapp button { padding: 0.3rem 0.65rem; background: #10b981; color: #fff; border: 0; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  .newapp button:disabled { opacity: 0.5; cursor: not-allowed; }

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
