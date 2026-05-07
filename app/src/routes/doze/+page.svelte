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

  type Proposal = {
    id: string;
    label: string;
    description: string;
    source_thread: string;
    source_file: string;
    x: number;
    y: number;
    w: number;
    h: number;
    added_ms: number;
  };

  let status = $state<Status | null>(null);
  let proposals = $state<Proposal[]>([]);
  let pngs = $state<Record<string, string>>({});
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

  async function loadProposals() {
    try {
      const r = await invoke<{ items: Proposal[] }>("doze_proposals_list");
      proposals = r.items ?? [];
      // Lazily fetch png_b64 for ones we haven't loaded yet.
      for (const p of proposals) {
        if (pngs[p.id]) continue;
        try {
          const r2 = await invoke<{ png_b64: string }>("doze_proposal_read_png", { id: p.id });
          pngs[p.id] = r2.png_b64;
        } catch {
          /* ignore single failure */
        }
      }
    } catch (e) {
      err = String(e);
    }
  }

  async function refresh() {
    err = "";
    await Promise.all([loadStatus(), loadProposals()]);
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

  async function accept(p: Proposal) {
    busy = true; err = "";
    try {
      await invoke("doze_proposal_accept", { id: p.id, label: p.label, description: p.description });
      info = `accepted ${p.label} → icon atlas`;
      delete pngs[p.id];
      await loadProposals();
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  async function reject(p: Proposal) {
    if (!(await appConfirm(`Reject icon proposal '${p.label}'?`, { danger: true }))) return;
    busy = true; err = "";
    try {
      await invoke("doze_proposal_reject", { id: p.id });
      delete pngs[p.id];
      await loadProposals();
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  async function clearAll() {
    if (!proposals.length) return;
    if (!(await appConfirm(`Reject all ${proposals.length} pending proposals?`, { danger: true }))) return;
    busy = true; err = "";
    try {
      await invoke("doze_proposals_clear");
      pngs = {};
      await loadProposals();
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
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
    long-term tips / memory / icon proposals. Icon proposals queue here for your review.
    See <code>Docs/doze.md</code>.
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

  <section class="proposals">
    <div class="proposals-head">
      <h2>Icon proposals ({proposals.length})</h2>
      {#if proposals.length}
        <button class="danger" onclick={clearAll} disabled={busy}>Reject all</button>
      {/if}
    </div>
    {#if proposals.length === 0}
      <p class="muted">No pending proposals. The doze reflector will queue icon crops here when it spots concrete coordinate hints in past threads.</p>
    {:else}
      <div class="grid">
        {#each proposals as p (p.id)}
          <div class="card">
            <div class="preview">
              {#if pngs[p.id]}
                <img src={"data:image/png;base64," + pngs[p.id]} alt={p.label} />
              {:else}
                <div class="placeholder">…</div>
              {/if}
            </div>
            <div class="meta">
              <input bind:value={p.label} class="lab" placeholder="label" />
              <textarea bind:value={p.description} placeholder="description" rows="2"></textarea>
              <p class="src">
                <code>{p.source_file}</code> @ ({p.x},{p.y}) {p.w}×{p.h}
                <br/><span class="muted">from {p.source_thread}</span>
              </p>
              <div class="row">
                <button onclick={() => accept(p)} disabled={busy}>Accept → atlas</button>
                <button class="danger" onclick={() => reject(p)} disabled={busy}>Reject</button>
              </div>
            </div>
          </div>
        {/each}
      </div>
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
  .muted { color: #6b7280; font-size: 0.85rem; }
  table { border-collapse: collapse; width: 100%; max-width: 38rem; }
  th, td { text-align: left; padding: 0.25rem 0.6rem; vertical-align: top; font-size: 0.88rem; }
  th { color: #6b7280; font-weight: 500; width: 9rem; }
  code { background: #f3f4f6; padding: 0 0.25rem; border-radius: 3px; font-size: 0.82rem; }
  .pill { display: inline-block; background: #eef2ff; color: #3730a3;
          padding: 0.05rem 0.45rem; border-radius: 999px; margin-right: 0.3rem; font-size: 0.8rem; }
  .actions { margin-top: 0.6rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .actions button, .row button { padding: 0.35rem 0.9rem; background: #2563eb; color: #fff;
                                  border: 0; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.danger { background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
  .proposals-head { display: flex; align-items: baseline; gap: 1rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr));
          gap: 0.75rem; }
  .card { border: 1px solid #e5e7eb; border-radius: 6px; padding: 0.6rem; background: #fff;
          display: flex; flex-direction: column; gap: 0.4rem; }
  .preview { display: flex; align-items: center; justify-content: center;
             min-height: 5rem; background: #f9fafb; border: 1px dashed #d1d5db; border-radius: 4px;
             padding: 0.4rem; }
  .preview img { max-width: 100%; max-height: 8rem; image-rendering: pixelated; }
  .placeholder { color: #9ca3af; }
  .meta input.lab { width: 100%; box-sizing: border-box; padding: 0.3rem; border: 1px solid #d1d5db;
                    border-radius: 4px; font-size: 0.88rem; }
  .meta textarea { width: 100%; box-sizing: border-box; padding: 0.3rem; border: 1px solid #d1d5db;
                   border-radius: 4px; font: 12px ui-monospace, Consolas, monospace; resize: vertical; }
  .src { margin: 0.2rem 0; font-size: 0.78rem; color: #4b5563; }
  .row { display: flex; gap: 0.4rem; }
  section { margin-bottom: 1.5rem; }
</style>
