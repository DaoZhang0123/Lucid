<script lang="ts">
  /**
   * First-launch disclaimer gate.
   *
   * Blocks the app behind an "I have read and accept" checkbox the user can
   * only enable after scrolling the terms to the bottom. Acceptance is
   * persisted to localStorage with a version field; bumping `DISCLAIMER_VERSION`
   * re-prompts existing installs (e.g. when terms materially change).
   *
   * Decline closes the window. Accept hides the gate and renders nothing.
   *
   * Persistence choice: localStorage instead of a sidecar file because
   *   (a) we want zero-latency cold-start check (no flash before sidecar boot);
   *   (b) "user cleared browser data → re-prompt" is acceptable for a one-time
   *       legal acknowledgement (no harm in showing it again).
   */
  import { _ } from "svelte-i18n";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { onMount } from "svelte";

  // Bump this integer when terms change materially. Old installs whose
  // accepted_version is below this number will see the modal again.
  const DISCLAIMER_VERSION = 1;
  const STORAGE_KEY = "lucid.disclaimer.accepted_version";
  const ACCEPTED_AT_KEY = "lucid.disclaimer.accepted_at";

  function readAcceptedVersion(): number {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return 0;
      const n = parseInt(raw, 10);
      return Number.isFinite(n) ? n : 0;
    } catch {
      return 0;
    }
  }

  // Default to "not visible" so we don't flash the modal during the
  // localStorage read on returning users. onMount flips it on if needed.
  let visible = $state(false);
  let bumped = $state(false); // true = user previously accepted an older version
  let scrolledToEnd = $state(false);
  let checked = $state(false);
  let bodyEl: HTMLDivElement | null = $state(null);

  onMount(() => {
    const accepted = readAcceptedVersion();
    if (accepted < DISCLAIMER_VERSION) {
      visible = true;
      bumped = accepted > 0;
    }
  });

  function onScroll() {
    if (!bodyEl) return;
    // 24 px tolerance — covers sub-pixel rounding on HiDPI scrollbars.
    const remaining = bodyEl.scrollHeight - bodyEl.clientHeight - bodyEl.scrollTop;
    if (remaining < 24) scrolledToEnd = true;
  }

  function decline() {
    void getCurrentWindow().close();
  }

  function accept() {
    if (!checked) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, String(DISCLAIMER_VERSION));
      window.localStorage.setItem(ACCEPTED_AT_KEY, new Date().toISOString());
    } catch {
      /* private mode — accept for this session only */
    }
    visible = false;
  }

  function onKey(e: KeyboardEvent) {
    if (!visible) return;
    // Block accidental dismissal: Esc declines (closes window), no other
    // shortcut commits. Backdrop click is also a no-op (see template).
    if (e.key === "Escape") {
      e.preventDefault();
      decline();
    }
  }
</script>

<svelte:window on:keydown={onKey} />

{#if visible}
  <div class="dg-backdrop" role="presentation">
    <div
      class="dg-card"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="dg-title"
    >
      <div id="dg-title" class="dg-title">
        {$_("disclaimer.title", { default: "Terms of Use" })}
        {#if bumped}
          <span class="dg-bumped">
            · {$_("disclaimer.version_changed", { default: "updated — please re-read" })}
          </span>
        {/if}
      </div>

      <div
        bind:this={bodyEl}
        class="dg-body"
        tabindex="0"
        onscroll={onScroll}
        role="document"
      >
        {$_("disclaimer.body", { default: "" })}
      </div>

      <div class="dg-hint">
        {#if scrolledToEnd}
          <label class="dg-check">
            <input type="checkbox" bind:checked />
            <span>{$_("disclaimer.checkbox", { default: "I have read and accept the terms; I use Lucid at my own risk." })}</span>
          </label>
        {:else}
          <span class="dg-scroll-hint">
            ↓ {$_("disclaimer.scroll_hint", { default: "Please scroll to the bottom to enable the acknowledgement." })}
          </span>
        {/if}
      </div>

      <div class="dg-actions">
        <button class="dg-btn dg-decline" type="button" onclick={decline}>
          {$_("disclaimer.decline", { default: "Decline and exit" })}
        </button>
        <button
          class="dg-btn dg-accept"
          type="button"
          disabled={!checked}
          onclick={accept}
        >
          {$_("disclaimer.accept", { default: "Accept and continue" })}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .dg-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: flex; align-items: center; justify-content: center;
    z-index: 10000;
  }
  .dg-card {
    background: var(--bg, #fff);
    color: var(--fg, #111827);
    border-radius: 10px;
    width: min(680px, 92vw);
    max-height: 86vh;
    display: flex; flex-direction: column;
    padding: 1.1rem 1.3rem 1rem;
    box-shadow: 0 16px 48px rgba(0,0,0,0.35);
    font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  :global(.dark) .dg-card {
    background: #1f2937;
    color: #e5e7eb;
  }
  .dg-title {
    font-weight: 600; font-size: 1.05rem;
    margin-bottom: 0.7rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid rgba(120,120,120,0.25);
  }
  .dg-bumped { font-weight: 400; font-size: 0.85rem; color: #b45309; }
  :global(.dark) .dg-bumped { color: #fbbf24; }
  .dg-body {
    flex: 1 1 auto;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.55;
    font-size: 0.92rem;
    padding: 0.4rem 0.8rem 0.4rem 0.2rem;
    margin: 0 -0.3rem;
    border: 1px solid rgba(120,120,120,0.18);
    border-radius: 6px;
    background: rgba(120,120,120,0.05);
  }
  .dg-body:focus { outline: 2px solid #2563eb; outline-offset: -2px; }
  .dg-hint {
    margin-top: 0.7rem;
    min-height: 1.4rem;
    font-size: 0.88rem;
  }
  .dg-scroll-hint { color: #6b7280; font-style: italic; }
  .dg-check {
    display: inline-flex; align-items: center; gap: 0.5rem;
    cursor: pointer;
  }
  .dg-check input { width: 1rem; height: 1rem; }
  .dg-actions {
    margin-top: 0.9rem;
    display: flex; justify-content: flex-end; gap: 0.6rem;
  }
  .dg-btn {
    padding: 0.5rem 1rem;
    border-radius: 6px;
    border: 1px solid rgba(120,120,120,0.35);
    background: transparent;
    color: inherit;
    cursor: pointer;
    font-size: 0.92rem;
  }
  .dg-btn:hover { background: rgba(120,120,120,0.1); }
  .dg-decline { color: #b91c1c; }
  :global(.dark) .dg-decline { color: #fca5a5; }
  .dg-accept {
    background: #2563eb; color: #fff; border-color: #2563eb;
  }
  .dg-accept:hover:not(:disabled) { background: #1d4ed8; }
  .dg-accept:disabled {
    background: rgba(120,120,120,0.25);
    border-color: rgba(120,120,120,0.25);
    color: rgba(120,120,120,0.7);
    cursor: not-allowed;
  }
</style>
