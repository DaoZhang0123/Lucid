<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount, onDestroy } from "svelte";
  import { _ } from "svelte-i18n";
  import { revealItemInDir } from "@tauri-apps/plugin-opener";
  import { appConfirm } from "$lib/appConfirm.svelte";

  type Status = {
    enabled: boolean;
    running: boolean;
    last_pass_ms: number;
    last_thread_id: string;
    last_outcome: Record<string, number>;
    processed_count: number;
    totals?: Record<string, number>;
    log_path?: string;
    last_activity_ms: number;
    last_error: string;
  };

  type Output = {
    id: string;
    ts: string;
    ts_ms: number;
    name: string;
    kind: string;        // "tip" | "memory"
    app: string;
    tip_kind: string;
    text: string;
    file: string;
    entry: string;
    thread_id: string;
  };

  let status = $state<Status | null>(null);
  let outputs = $state<Output[]>([]);
  let outputsPath = $state<string>("");
  let busy = $state(false);
  let err = $state("");
  let info = $state("");
  let timer: ReturnType<typeof setInterval> | null = null;

  function fmtTime(ms: number): string {
    if (!ms) return $_("doze.value_dash");
    return new Date(ms).toLocaleString();
  }

  function idleSec(): number {
    if (!status?.last_activity_ms) return 0;
    return Math.max(0, Math.floor((Date.now() - status.last_activity_ms) / 1000));
  }

  async function loadStatus() {
    try {
      status = await invoke<Status>("doze_status");
    } catch (e) {
      err = String(e);
    }
  }

  async function loadOutputs() {
    try {
      const res = await invoke<{ items: Output[]; path: string }>("doze_outputs", { limit: 200 });
      outputs = res.items || [];
      outputsPath = res.path || "";
    } catch (e) {
      err = String(e);
    }
  }

  async function refresh() {
    err = "";
    await Promise.all([loadStatus(), loadOutputs()]);
  }

  async function runNow() {
    busy = true; err = ""; info = "";
    try {
      await invoke("doze_run_now");
      info = $_("doze.info_scheduled");
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  async function clearProcessed() {
    if (!(await appConfirm($_("doze.confirm_clear"), { danger: true }))) return;
    try {
      await invoke("doze_clear_processed");
      info = $_("doze.info_cleared");
      await loadStatus();
    } catch (e) {
      err = String(e);
    }
  }

  async function openLog() {
    if (!status?.log_path) return;
    try {
      await revealItemInDir(status.log_path);
    } catch (e) {
      err = String(e);
    }
  }

  async function deleteOutput(item: Output) {
    const msg = $_("doze.outputs_confirm_delete", {
      values: { kind: item.kind, file: item.file },
    });
    if (!(await appConfirm(msg, { danger: true }))) return;
    busy = true; err = ""; info = "";
    try {
      const res = await invoke<{ ok: boolean; removed_from_file?: boolean; error?: string }>(
        "doze_delete_output",
        { id: item.id },
      );
      if (!res.ok) {
        err = res.error || "delete failed";
      } else if (res.removed_from_file === false) {
        info = $_("doze.outputs_warn_not_in_file");
      } else {
        info = $_("doze.outputs_info_deleted");
      }
      await loadOutputs();
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  async function openOutputFile(item: Output) {
    if (!item.file) return;
    try {
      await revealItemInDir(item.file);
    } catch (e) {
      err = String(e);
    }
  }

  onMount(() => {
    refresh();
    timer = setInterval(refresh, 5000);
  });
  onDestroy(() => {
    if (timer) clearInterval(timer);
  });
</script>

<svelte:head>
  <title>{$_("doze.page_title")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("header.nav_doze")}</h1>
  </header>

  <p class="hint">{@html $_("doze.hint")}</p>

  {#if err}<p class="err">{err}</p>{/if}
  {#if info}<p class="info">{info}</p>{/if}

  <section class="status">
    <h2>{$_("doze.status_heading")}</h2>
    {#if status}
      <table>
        <tbody>
          <tr><th>{$_("doze.row_enabled")}</th><td>{status.enabled ? $_("doze.value_yes") : $_("doze.value_no_disabled")}</td></tr>
          <tr><th>{$_("doze.row_running")}</th><td>{status.running ? $_("doze.value_yes") : $_("doze.value_no")}</td></tr>
          <tr><th>{$_("doze.row_idle_for")}</th><td>{$_("doze.value_seconds", { values: { n: idleSec() } })}</td></tr>
          <tr><th>{$_("doze.row_last_pass")}</th><td>{fmtTime(status.last_pass_ms)}</td></tr>
          <tr><th>{$_("doze.row_last_thread")}</th><td><code>{status.last_thread_id || $_("doze.value_dash")}</code></td></tr>
          <tr><th>{$_("doze.row_last_outcome")}</th><td>
            {#if Object.keys(status.last_outcome).length}
              {#each Object.entries(status.last_outcome) as [k, v]}
                <span class="pill">{k}={v}</span>
              {/each}
            {:else}{$_("doze.value_dash")}{/if}
          </td></tr>
          <tr><th>{$_("doze.row_processed_count")}</th><td>{status.processed_count}</td></tr>
          <tr><th>{$_("doze.row_totals")}</th><td>
            {#if status.totals && Object.keys(status.totals).length}
              {#each Object.entries(status.totals) as [k, v]}
                <span class="pill">{k}={v}</span>
              {/each}
            {:else}{$_("doze.value_dash")}{/if}
          </td></tr>
          {#if status.log_path}
            <tr><th>{$_("doze.row_log_path")}</th><td><code>{status.log_path}</code></td></tr>
          {/if}
          {#if status.last_error}
            <tr><th>{$_("doze.row_last_error")}</th><td class="err">{status.last_error}</td></tr>
          {/if}
        </tbody>
      </table>
      <p class="hint" style="margin-top:0.5rem">{$_("doze.why_zero_hint")}</p>
      <div class="actions">
        <button onclick={runNow} disabled={busy || !status.enabled}>{$_("doze.button_run_now")}</button>
        <button onclick={clearProcessed} disabled={busy}>{$_("doze.button_clear")}</button>
        <button onclick={refresh} disabled={busy}>{$_("doze.button_refresh")}</button>
        {#if status.log_path}
          <button onclick={openLog} disabled={busy}>{$_("doze.button_open_log")}</button>
        {/if}
      </div>
    {:else}
      <p>{$_("doze.loading")}</p>
    {/if}
  </section>

  <section class="outputs">
    <h2>{$_("doze.outputs_heading")}</h2>
    {#if outputsPath}
      <p class="hint">
        <span>{$_("doze.outputs_path_label")}</span>
        <code>{outputsPath}</code>
      </p>
    {/if}
    {#if outputs.length === 0}
      <p class="hint">{$_("doze.outputs_empty")}</p>
    {:else}
      <table class="outputs-table">
        <thead>
          <tr>
            <th>{$_("doze.outputs_col_kind")}</th>
            <th>{$_("doze.outputs_col_when")}</th>
            <th>{$_("doze.outputs_col_app")}</th>
            <th>{$_("doze.outputs_col_text")}</th>
            <th>{$_("doze.outputs_col_file")}</th>
            <th>{$_("doze.outputs_col_actions")}</th>
          </tr>
        </thead>
        <tbody>
          {#each outputs as item (item.id)}
            <tr>
              <td>
                <span class="pill kind-{item.kind}">
                  {item.kind === "memory"
                    ? $_("doze.outputs_kind_memory")
                    : $_("doze.outputs_kind_tip")}
                  {#if item.tip_kind}· {item.tip_kind}{/if}
                </span>
              </td>
              <td class="nowrap">{item.ts}</td>
              <td>
                {#if item.kind === "tip"}
                  {item.app || $_("doze.outputs_app_global")}
                {:else}—{/if}
              </td>
              <td class="text-cell">{item.text}</td>
              <td><code class="path">{item.file}</code></td>
              <td class="nowrap">
                <button class="link" onclick={() => openOutputFile(item)}>
                  {$_("doze.outputs_button_open")}
                </button>
                <button class="link danger" onclick={() => deleteOutput(item)} disabled={busy}>
                  {$_("doze.outputs_button_delete")}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </section>
</div>

<style>
  .page { max-width: 64rem; margin: 0 auto; padding: 1rem 1.5rem;
          font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  header { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 0.5rem; }
  .back { color: #2563eb; text-decoration: none; font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  h2 { margin: 1.2rem 0 0.5rem; font-size: 1.05rem; }
  .hint { font-size: 0.85rem; color: #4b5563; }
  .err { color: #b91c1c; }
  .info { color: #047857; }
  table { border-collapse: collapse; width: 100%; max-width: 38rem; }
  th, td { text-align: left; padding: 0.25rem 0.6rem; vertical-align: top; font-size: 0.88rem; }
  th { color: #6b7280; font-weight: 500; width: 9rem; }
  code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; font-size: 0.82rem; }
  .pill { display: inline-block; background: #eef2ff; color: #3730a3;
          padding: 0.05rem 0.45rem; border-radius: 999px; margin-right: 0.3rem; font-size: 0.8rem; }
  .actions { margin-top: 0.6rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .actions button { padding: 0.35rem 0.9rem; background: #2563eb; color: #fff;
                    border: 0; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  section { margin-bottom: 1.5rem; }
  .outputs-table { width: 100%; max-width: none; }
  .outputs-table th, .outputs-table td { vertical-align: top; padding: 0.4rem 0.6rem;
    border-top: 1px solid #f1f5f9; font-size: 0.85rem; }
  .outputs-table th { color: #6b7280; font-weight: 500; width: auto;
    border-top: 0; border-bottom: 1px solid #e5e7eb; background: #f9fafb; }
  .outputs-table .text-cell { white-space: pre-wrap; word-break: break-word; max-width: 28rem; }
  .outputs-table .path { font-size: 0.75rem; word-break: break-all; }
  .outputs-table .nowrap { white-space: nowrap; }
  .pill.kind-tip { background: #ecfdf5; color: #047857; }
  .pill.kind-memory { background: #fef3c7; color: #92400e; }
  button.link { background: transparent; color: #2563eb; border: 0; padding: 0.1rem 0.3rem;
    cursor: pointer; font-size: 0.8rem; }
  button.link.danger { color: #b91c1c; }
  button.link:hover { text-decoration: underline; }
</style>
