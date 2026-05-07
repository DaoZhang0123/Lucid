<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount, onDestroy } from "svelte";
  import { _ } from "svelte-i18n";
  import { appConfirm } from "$lib/appConfirm.svelte";

  type Status = {
    enabled: boolean;
    running: boolean;
    last_pass_ms: number;
    last_thread_id: string;
    last_outcome: Record<string, number>;
    processed_count: number;
    last_activity_ms: number;
    last_error: string;
  };

  let status = $state<Status | null>(null);
  let busy = $state(false);
  let err = $state("");
  let info = $state("");
  let timer: ReturnType<typeof setInterval> | null = null;

  function fmtTime(ms: number): string {
    if (!ms) return "—";
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

  async function refresh() {
    err = "";
    await loadStatus();
  }

  async function runNow() {
    busy = true; err = ""; info = "";
    try {
      await invoke("doze_run_now");
      info = "scheduled — next tick will pick a thread (status will update shortly)";
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  async function clearProcessed() {
    if (!(await appConfirm("Clear the processed-thread index? Past threads will be re-learned next pass.", { danger: true }))) return;
    try {
      await invoke("doze_clear_processed");
      info = "processed index cleared";
      await loadStatus();
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
  <title>{$_("header.nav_doze")}</title>
</svelte:head>

<div class="page">
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("header.nav_doze")}</h1>
  </header>

  <p class="hint">
    Idle-time reflection: when the sidecar has been quiet for a while, a low-priority
    background pass scans your past threads and asks the LLM what's worth saving as
    long-term tips / memory. See <code>Docs/doze.md</code>.
  </p>

  {#if err}<p class="err">{err}</p>{/if}
  {#if info}<p class="info">{info}</p>{/if}

  <section class="status">
    <h2>Status</h2>
    {#if status}
      <table>
        <tbody>
          <tr><th>Enabled</th><td>{status.enabled ? "yes" : "no (set [doze].enabled = true in config)"}</td></tr>
          <tr><th>Running now</th><td>{status.running ? "yes" : "no"}</td></tr>
          <tr><th>Idle for</th><td>{idleSec()} s</td></tr>
          <tr><th>Last pass</th><td>{fmtTime(status.last_pass_ms)}</td></tr>
          <tr><th>Last thread</th><td><code>{status.last_thread_id || "—"}</code></td></tr>
          <tr><th>Last outcome</th><td>
            {#if Object.keys(status.last_outcome).length}
              {#each Object.entries(status.last_outcome) as [k, v]}
                <span class="pill">{k}={v}</span>
              {/each}
            {:else}—{/if}
          </td></tr>
          <tr><th>Processed threads</th><td>{status.processed_count}</td></tr>
          {#if status.last_error}
            <tr><th>Last error</th><td class="err">{status.last_error}</td></tr>
          {/if}
        </tbody>
      </table>
      <div class="actions">
        <button onclick={runNow} disabled={busy || !status.enabled}>Run a pass now</button>
        <button onclick={clearProcessed} disabled={busy}>Clear processed index</button>
        <button onclick={refresh} disabled={busy}>Refresh</button>
      </div>
    {:else}
      <p>loading…</p>
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
</style>
