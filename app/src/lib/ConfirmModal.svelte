<script lang="ts">
  import { _ } from "svelte-i18n";
  import { confirmState, resolveConfirm } from "$lib/appConfirm.svelte";

  function onKey(e: KeyboardEvent) {
    if (!confirmState.pending) return;
    if (e.key === "Escape") { e.preventDefault(); resolveConfirm(false); }
    else if (e.key === "Enter") { e.preventDefault(); resolveConfirm(true); }
  }
</script>

<svelte:window on:keydown={onKey} />

{#if confirmState.pending}
  <div
    class="cf-backdrop"
    role="presentation"
    onclick={() => resolveConfirm(false)}
    onkeydown={() => {}}
  >
    <div
      class="cf-card"
      role="alertdialog"
      aria-modal="true"
      onclick={(e) => e.stopPropagation()}
      onkeydown={() => {}}
    >
      <div class="cf-title">{confirmState.pending.title ?? $_("confirm.title", { default: "Please confirm" })}</div>
      <div class="cf-msg">{confirmState.pending.message}</div>
      <div class="cf-actions">
        <button class="cf-btn cf-cancel" onclick={() => resolveConfirm(false)}>
          {confirmState.pending.cancelLabel ?? $_("confirm.cancel", { default: "Cancel" })}
        </button>
        <button
          class="cf-btn cf-ok"
          class:danger={confirmState.pending.danger}
          autofocus
          onclick={() => resolveConfirm(true)}
        >
          {confirmState.pending.okLabel ?? $_("confirm.ok", { default: "OK" })}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .cf-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.4);
    display: flex; align-items: center; justify-content: center;
    z-index: 9999;
  }
  .cf-card {
    background: #fff; color: #111827; border-radius: 8px;
    min-width: 22rem; max-width: 32rem; padding: 1rem 1.2rem;
    box-shadow: 0 12px 32px rgba(0,0,0,0.25);
    font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .cf-title { font-weight: 600; font-size: 0.95rem; margin-bottom: 0.5rem; }
  .cf-msg { white-space: pre-wrap; line-height: 1.45; color: #374151; margin-bottom: 1rem; }
  .cf-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
  .cf-btn {
    padding: 0.4rem 1rem; border-radius: 4px; cursor: pointer;
    font: inherit; border: 1px solid transparent;
  }
  .cf-cancel { background: #f3f4f6; color: #374151; border-color: #d1d5db; }
  .cf-cancel:hover { background: #e5e7eb; }
  .cf-ok { background: #2563eb; color: #fff; }
  .cf-ok:hover { background: #1d4ed8; }
  .cf-ok.danger { background: #dc2626; }
  .cf-ok.danger:hover { background: #b91c1c; }
</style>
