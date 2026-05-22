<script lang="ts">
  import { isLoading } from "svelte-i18n";
  import { setupI18n } from "$lib/i18n";
  import { setupTheme } from "$lib/theme";
  import ConfirmModal from "$lib/ConfirmModal.svelte";
  import DisclaimerGate from "$lib/DisclaimerGate.svelte";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { onMount } from "svelte";
  import { initVoice } from "$lib/voice";

  // Bootstrap i18n once at the root layout. Locale lookup currently falls back
  // to navigator -> "en". A user-selectable locale (saved to config.toml [ui])
  // will be wired through the settings page in a follow-up.
  setupI18n();
  // Apply persisted light/dark theme synchronously before children render.
  setupTheme();

  let { children } = $props();

  const appWindow = getCurrentWindow();
  function winMin() { appWindow.minimize(); }
  function winMax() { appWindow.toggleMaximize(); }
  function winClose() { appWindow.close(); }

  // Voice (push-to-talk) — only init for the main window. The voice-overlay
  // window has its own layout so this layout doesn't run there.
  onMount(() => {
    if (appWindow.label === "main") {
      void initVoice();
    }
  });
</script>

{#if $isLoading}
  <div class="splash" role="status" aria-label="Loading Lucid">
    <div class="splash-stage">
      <svg class="reticle" viewBox="0 0 64 64" aria-hidden="true">
        <circle cx="32" cy="32" r="22" fill="none" stroke="currentColor" stroke-width="2.2" />
        <circle cx="32" cy="32" r="2.6" fill="currentColor" />
        <line x1="32" y1="2"  x2="32" y2="14" stroke="currentColor" stroke-width="2.2" />
        <line x1="32" y1="50" x2="32" y2="62" stroke="currentColor" stroke-width="2.2" />
        <line x1="2"  y1="32" x2="14" y2="32" stroke="currentColor" stroke-width="2.2" />
        <line x1="50" y1="32" x2="62" y2="32" stroke="currentColor" stroke-width="2.2" />
      </svg>
      <span class="crab" aria-hidden="true">🦀</span>
    </div>
    <div class="splash-title">Lucid</div>
    <div class="splash-sub">Vision Agent for Windows</div>
  </div>
{:else if appWindow.label !== "main"}
  <!-- Auxiliary Tauri windows (voice-overlay, future popups) get NO custom
       titlebar / ConfirmModal / DisclaimerGate. SvelteKit child +layout.svelte
       files still compose with this root layout, so we must explicitly skip
       the chrome here — otherwise the overlay shows the main window's dark
       titlebar (or its light-theme #f3f4f6 ≈ white variant) on top of the
       transparent rounded overlay pill, which looks like a stray window
       frame. The overlay's own +layout.svelte handles its transparent body. -->
  {@render children()}
{:else}
  <div class="root">
    <div class="titlebar" data-tauri-drag-region>
      <div class="tb-spacer" data-tauri-drag-region></div>
      <div class="tb-controls">
        <button class="tb-btn" type="button" title="Minimize" aria-label="Minimize" onclick={winMin}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M0 5h10" stroke="currentColor" stroke-width="1" /></svg>
        </button>
        <button class="tb-btn" type="button" title="Maximize" aria-label="Maximize" onclick={winMax}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1" /></svg>
        </button>
        <button class="tb-btn tb-close" type="button" title="Close" aria-label="Close" onclick={winClose}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M0 0l10 10M10 0L0 10" stroke="currentColor" stroke-width="1" /></svg>
        </button>
      </div>
    </div>
    <div class="root-body">
      {@render children()}
    </div>
  </div>
  <ConfirmModal />
  <DisclaimerGate />
{/if}

<style>
  /* ---------- Custom titlebar (native decorations are off) ---------- */
  .root { display: flex; flex-direction: column; height: 100vh; min-height: 100vh; }
  .titlebar {
    flex: none;
    height: 30px;
    display: flex;
    align-items: stretch;
    /* Match the in-page header background (.app header in +page.svelte) so the
       titlebar and the menu bar below it form a single visual surface. Light
       theme override is below. */
    background: #1f2937;
    color: #e5e7eb;
    user-select: none;
  }
  .tb-spacer { flex: 1; }
  .tb-controls { display: flex; align-items: stretch; }
  .tb-btn {
    width: 46px; height: 100%;
    display: inline-flex; align-items: center; justify-content: center;
    background: transparent; color: inherit; border: 0; cursor: pointer; padding: 0;
    transition: background 0.12s;
  }
  .tb-btn:hover { background: rgba(255,255,255,0.10); }
  .tb-close:hover { background: #e81123; color: #fff; }
  .root-body { flex: 1; min-height: 0; overflow: auto; display: flex; flex-direction: column; }
  /* The chat page uses height:100vh on .app — make it fill the body slot
     instead of the whole viewport so it doesn't get clipped under titlebar. */
  :global(.root-body > .app) { height: 100% !important; flex: 1; }
  /* Light theme titlebar */
  :global(html[data-theme="light"]) .titlebar { background: #f3f4f6; color: #111827; }
  :global(html[data-theme="light"]) .tb-btn:hover { background: rgba(0,0,0,0.06); }

  /* ---------- Dark theme overrides ----------
   * Component-level styles use hex colors directly (light theme as authored).
   * Rather than touch every <style> block we re-skin the most visible surfaces
   * via :global selectors gated on data-theme="dark" at <html>. New components
   * inheriting body / textarea / input / button defaults pick this up for free.
   */
  :global(html[data-theme="dark"]) { color-scheme: dark; }
  /* Lock document scroll only on the chat page (it manages its own height:
     100vh layout with internal scrollers via .app). Subroutes (settings,
     schedules, etc.) need normal document scrolling. */
  :global(.app) { overflow: hidden; }
  :global(html[data-theme="dark"] body) { background: #0b1220 !important; color: #e5e7eb !important; }

  /* Cards / panels that defaulted to white. */
  :global(html[data-theme="dark"] .bubble.assistant) { background: #1f2937 !important; color: #e5e7eb !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] footer) { background: #111827 !important; border-top-color: #1f2937 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] form button.attach) { background: #1f2937 !important; color: #cbd5e1 !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] form button.attach:hover:not(:disabled)) { background: #334155 !important; color: #f1f5f9 !important; }
  :global(html[data-theme="dark"] form button.mic) { background: #1f2937 !important; color: #cbd5e1 !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] form button.mic:hover:not(:disabled)) { background: #334155 !important; color: #f1f5f9 !important; }
  :global(html[data-theme="dark"] form button.mic.mic-recording) { background: #4c1010 !important; color: #fca5a5 !important; border-color: #7f1d1d !important; }
  :global(html[data-theme="dark"] form button.mic.mic-transcribing) { background: #1e293b !important; color: #93c5fd !important; border-color: #1e40af !important; }
  :global(html[data-theme="dark"] .row) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .row.active) { border-color: #3b82f6 !important; }
  :global(html[data-theme="dark"] .chip) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .img-loading) { background: #1f2937 !important; color: #cbd5e1 !important; }
  :global(html[data-theme="dark"] .img-card img),
  :global(html[data-theme="dark"] .chip img) { border-color: #334155 !important; }

  /* Tool/badge call-outs (tan/yellow on light → muted dark). */
  :global(html[data-theme="dark"] .tool) { background: #2a2410 !important; border-color: #574012 !important; color: #fde68a !important; }
  :global(html[data-theme="dark"] .args) { color: #cbd5e1 !important; }

  /* Final-status pills. */
  :global(html[data-theme="dark"] .final-ok) { background: #064e3b !important; color: #6ee7b7 !important; }
  :global(html[data-theme="dark"] .final-cancelled) { background: #3f2d04 !important; color: #fde68a !important; }
  :global(html[data-theme="dark"] .final-step_cap),
  :global(html[data-theme="dark"] .final-error) { background: #4c1010 !important; color: #fca5a5 !important; }

  /* Form inputs. */
  :global(html[data-theme="dark"] textarea),
  :global(html[data-theme="dark"] input),
  :global(html[data-theme="dark"] select) {
    background: #1f2937 !important; color: #e5e7eb !important; border-color: #334155 !important;
  }
  :global(html[data-theme="dark"] textarea:disabled),
  :global(html[data-theme="dark"] input:disabled),
  :global(html[data-theme="dark"] select:disabled) {
    background: #111827 !important; color: #6b7280 !important;
  }
  :global(html[data-theme="dark"] textarea::placeholder),
  :global(html[data-theme="dark"] input::placeholder) { color: #6b7280 !important; }

  /* Code / inline tags. */
  :global(html[data-theme="dark"] code) { background: #1f2937 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .hint) { color: #9ca3af !important; }
  :global(html[data-theme="dark"] .meta) { color: #9ca3af !important; }
  :global(html[data-theme="dark"] .empty) { color: #6b7280 !important; }
  :global(html[data-theme="dark"] .system) { color: #9ca3af !important; }
  :global(html[data-theme="dark"] .img-meta) { color: #94a3b8 !important; }
  :global(html[data-theme="dark"] .img-card.from-user .img-meta) { color: #93c5fd !important; }

  /* Settings/templates/etc. small surfaces (scope-bar, fieldsets, dialogs). */
  :global(html[data-theme="dark"] fieldset.trigger),
  :global(html[data-theme="dark"] fieldset.constraints),
  :global(html[data-theme="dark"] .scope-bar) { background: #111827 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .apps-picker-trigger),
  :global(html[data-theme="dark"] .apps-picker-panel) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .apps-picker-row:hover) { background: #334155 !important; }

  /* Confirm modal card. */
  :global(html[data-theme="dark"] .cf-card) { background: #1f2937 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .cf-msg) { color: #cbd5e1 !important; }
  :global(html[data-theme="dark"] .cf-cancel) { background: #334155 !important; color: #e5e7eb !important; border-color: #475569 !important; }
  :global(html[data-theme="dark"] .cf-cancel:hover) { background: #475569 !important; }

  /* ---------- Dark theme: subroute pages ----------
   * settings / schedules / doze / templates / memory / tools authored with
   * hard-coded light colors. Override the high-impact white card surfaces
   * and accent pills to dark equivalents.
   */
  /* page-level text on subroutes (settings <main> hard-codes #222) */
  :global(html[data-theme="dark"] main) { color: #e5e7eb !important; }
  :global(html[data-theme="dark"] main h1),
  :global(html[data-theme="dark"] main h2) { color: #f1f5f9 !important; }
  :global(html[data-theme="dark"] main h3) { color: #cbd5e1 !important; }

  /* settings cards */
  :global(html[data-theme="dark"] .card) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .copilot-status) { background: #111827 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .usercode) { background: #0b1220 !important; color: #e5e7eb !important; border-color: #475569 !important; }
  :global(html[data-theme="dark"] .path) { color: #94a3b8 !important; }

  /* settings: left tab-nav sidebar */
  :global(html[data-theme="dark"] .tabs) { background: #1f2937 !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] .tab) { color: #cbd5e1 !important; }
  :global(html[data-theme="dark"] .tab:hover) { background: #334155 !important; }
  :global(html[data-theme="dark"] .tab.active) { background: #2563eb !important; color: #fff !important; }

  /* settings: about-tab — ghost button + contact links */
  :global(html[data-theme="dark"] .btn-link-ghost) { background: #1f2937 !important; color: #93c5fd !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] .btn-link-ghost:hover) { background: #334155 !important; }
  :global(html[data-theme="dark"] .contact-label) { color: #94a3b8 !important; }
  :global(html[data-theme="dark"] .contact a) { color: #93c5fd !important; }

  /* chat: ClampText "Show more / less" toggle */
  :global(html[data-theme="dark"] .clamp-toggle) { color: #93c5fd !important; }
  :global(html[data-theme="dark"] .clamp-toggle:hover) { color: #bfdbfe !important; }

  /* schedules list rows + ghost buttons + tags */
  :global(html[data-theme="dark"] .row) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .actions button.ghost),
  :global(html[data-theme="dark"] .ops button.ghost),
  :global(html[data-theme="dark"] fieldset.trigger button.ghost) {
    background: #1f2937 !important; color: #93c5fd !important; border-color: #334155 !important;
  }
  :global(html[data-theme="dark"] .ops button.danger) {
    background: #4c1010 !important; color: #fca5a5 !important; border-color: #7f1d1d !important;
  }
  :global(html[data-theme="dark"] .trigger-tag) { background: #1e3a8a !important; color: #bfdbfe !important; }
  :global(html[data-theme="dark"] .type-tag) { background: #134e4a !important; color: #99f6e4 !important; }
  :global(html[data-theme="dark"] .instr) { color: #cbd5e1 !important; }
  :global(html[data-theme="dark"] .empty) { color: #6b7280 !important; }

  /* schedules apps-picker */
  :global(html[data-theme="dark"] .apps-picker-trigger),
  :global(html[data-theme="dark"] .apps-picker-panel) { background: #1f2937 !important; border-color: #334155 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] .apps-picker-trigger:hover) { border-color: #93c5fd !important; }
  :global(html[data-theme="dark"] .apps-picker-row:hover) { background: #334155 !important; }
  :global(html[data-theme="dark"] .apps-picker-row.checked) { background: #1e3a8a !important; }
  :global(html[data-theme="dark"] .apps-picker-chip) { background: #1e3a8a !important; border-color: #3b82f6 !important; color: #bfdbfe !important; }
  :global(html[data-theme="dark"] .apps-picker-chip .chip-x) { color: #bfdbfe !important; }
  :global(html[data-theme="dark"] .apps-picker-chip .chip-x:hover) { color: #fca5a5 !important; }

  /* doze pills + table head */
  :global(html[data-theme="dark"] .pill) { background: #1e293b !important; color: #c7d2fe !important; }
  :global(html[data-theme="dark"] .pill.kind-tip) { background: #064e3b !important; color: #6ee7b7 !important; }
  :global(html[data-theme="dark"] .pill.kind-memory) { background: #4a2f06 !important; color: #fbbf24 !important; }
  :global(html[data-theme="dark"] table th) { color: #94a3b8 !important; }
  :global(html[data-theme="dark"] .outputs-table th) { background: #111827 !important; border-bottom-color: #334155 !important; color: #94a3b8 !important; }
  :global(html[data-theme="dark"] .outputs-table th),
  :global(html[data-theme="dark"] .outputs-table td) { border-top-color: #1f2937 !important; }
  :global(html[data-theme="dark"] button.link) { color: #93c5fd !important; }
  :global(html[data-theme="dark"] button.link.danger) { color: #fca5a5 !important; }

  /* ---------- Light theme: re-skin header & sidebar ----------
   * The main page authored header / .sidebar / .pager etc. with hard-coded
   * dark colors. In light mode lighten them so the chrome matches the body.
   */
  :global(html[data-theme="light"] header) { background: #f3f4f6 !important; color: #111827 !important; border-bottom: 1px solid #e5e7eb; }
  :global(html[data-theme="light"] header .toggle),
  :global(html[data-theme="light"] header .theme-toggle),
  :global(html[data-theme="light"] header .nav-icon) { color: #111827 !important; border-color: #d1d5db !important; }
  :global(html[data-theme="light"] header .theme-toggle:hover),
  :global(html[data-theme="light"] header .nav-icon:hover) { background: rgba(0,0,0,0.05) !important; border-color: #9ca3af !important; }
  :global(html[data-theme="light"] header .nav-icon::after) { background: #111827 !important; color: #fff !important; }
  :global(html[data-theme="light"] header .title .logo),
  :global(html[data-theme="light"] header .title-text) { color: #111827 !important; }
  :global(html[data-theme="light"] header .link) { color: #2563eb !important; }
  :global(html[data-theme="light"] header .status) { color: #4b5563 !important; }
  :global(html[data-theme="light"] header .status.on) { color: #059669 !important; }
  :global(html[data-theme="light"] header .status.running) { color: #b45309 !important; }

  :global(html[data-theme="light"] .sidebar) { background: #f9fafb !important; color: #111827 !important; border-right: 1px solid #e5e7eb !important; }
  :global(html[data-theme="light"] .side-head) { border-bottom: 1px solid #e5e7eb !important; color: #374151 !important; }
  :global(html[data-theme="light"] .side-toggle) { color: #111827 !important; border-color: #d1d5db !important; }
  :global(html[data-theme="light"] .side-toggle:hover) { background: #e5e7eb !important; }
  :global(html[data-theme="light"] .thread:hover) { background: #e5e7eb !important; }
  /* Active thread: blue background → force WHITE on every descendant
     (title / meta / id pill / tags) so text doesn't disappear into the blue. */
  :global(html[data-theme="light"] .thread.active),
  :global(html[data-theme="light"] .thread.active .t-title),
  :global(html[data-theme="light"] .thread.active .t-meta),
  :global(html[data-theme="light"] .thread.active .t-id),
  :global(html[data-theme="light"] .thread.active .t-tag) { color: #fff !important; }
  :global(html[data-theme="light"] .thread.active .t-title),
  :global(html[data-theme="light"] .thread.active .t-meta) { background: transparent !important; }
  :global(html[data-theme="light"] .thread.active .t-tag) { background: rgba(255,255,255,0.22) !important; }
  :global(html[data-theme="light"] .thread.active) { background: #2563eb !important; }
  :global(html[data-theme="light"] .thread.active .t-meta) { opacity: 0.9 !important; }
  :global(html[data-theme="light"] .thread.active .t-id) { background: rgba(255,255,255,0.22) !important; }
  /* Inactive thread meta — bump contrast (default opacity 0.65 over light bg is too pale). */
  :global(html[data-theme="light"] .t-meta) { color: #4b5563 !important; opacity: 0.85 !important; }
  :global(html[data-theme="light"] .t-id) { background: rgba(15, 23, 42, 0.08) !important; color: #475569 !important; }
  /* Status tags (running / queued): low-alpha pills washed out on white → use solid. */
  :global(html[data-theme="light"] .t-tag.run) { background: #d1fae5 !important; color: #047857 !important; }
  :global(html[data-theme="light"] .t-tag.queued) { background: #fef3c7 !important; color: #b45309 !important; }
  :global(html[data-theme="light"] .pager) { background: #f3f4f6 !important; border-top: 1px solid #e5e7eb !important; color: #475569 !important; }
  :global(html[data-theme="light"] .pg-btn) { background: #fff !important; color: #111827 !important; border-color: #d1d5db !important; }
  :global(html[data-theme="light"] .pg-btn:hover:not(:disabled)) { background: #e5e7eb !important; }
  :global(html[data-theme="light"] .edge-toggle) { background: #f3f4f6 !important; color: #111827 !important; border-color: #d1d5db !important; }
  :global(html[data-theme="light"] .edge-toggle:hover) { background: #e5e7eb !important; }

  /* Bump muted text contrast across the chat surface in light mode. */
  :global(html[data-theme="light"] .system) { color: #4b5563 !important; }
  :global(html[data-theme="light"] .img-meta) { color: #4b5563 !important; }
  :global(html[data-theme="light"] .img-card.from-user .img-meta) { color: #1d4ed8 !important; }
  :global(html[data-theme="light"] .empty) { color: #6b7280 !important; }
  :global(html[data-theme="light"] textarea::placeholder),
  :global(html[data-theme="light"] input::placeholder) { color: #6b7280 !important; opacity: 1; }

  /* Dark mode: also enforce white on active-thread descendants for symmetry. */
  :global(html[data-theme="dark"] .thread.active .t-title),
  :global(html[data-theme="dark"] .thread.active .t-meta),
  :global(html[data-theme="dark"] .thread.active .t-id),
  :global(html[data-theme="dark"] .thread.active .t-tag) { color: #fff !important; }
  :global(html[data-theme="dark"] .thread.active .t-title),
  :global(html[data-theme="dark"] .thread.active .t-meta) { background: transparent !important; }
  :global(html[data-theme="dark"] .thread.active .t-tag),
  :global(html[data-theme="dark"] .thread.active .t-id) { background: rgba(255,255,255,0.22) !important; }

  /* ---------- Launch splash ---------- */
  .splash {
    position: fixed; inset: 0;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 0.6rem;
    background: #f3f4f6; color: #1f2937;
    font-family: "JetBrains Mono", "Cascadia Code", Consolas, "SF Mono", ui-monospace, monospace;
    z-index: 9999;
  }
  :global(html[data-theme="dark"]) .splash { background: #0b1220; color: #e5e7eb; }
  .splash-stage {
    position: relative;
    width: 96px; height: 96px;
    display: flex; align-items: center; justify-content: center;
  }
  .reticle { width: 80px; height: 80px; opacity: 0.92; }
  .crab {
    position: absolute;
    left: 50%; top: 50%;
    font-size: 22px; line-height: 1;
    transform: translate(-150px, -50%);
    animation: lucid-crab-walk 1.6s cubic-bezier(0.22, 0.61, 0.36, 1) 0.15s forwards;
    will-change: transform, opacity;
  }
  @keyframes lucid-crab-walk {
    0%   { transform: translate(-150px, -50%) rotate(0deg);   opacity: 0; }
    10%  { opacity: 1; }
    35%  { transform: translate(-60px, -50%)  rotate(-6deg);  }
    50%  { transform: translate(-30px, -50%)  rotate(6deg);   }
    78%  { transform: translate(-10px, -50%)  rotate(-3deg);  opacity: 1; }
    100% { transform: translate(-12px, -50%)  rotate(0deg);   opacity: 0; }
  }
  .splash-title {
    font-size: 1.4rem; font-weight: 500; letter-spacing: 0.06em;
    margin-top: 0.4rem;
  }
  .splash-sub {
    font-size: 0.78rem; opacity: 0.6; letter-spacing: 0.04em;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  @media (prefers-reduced-motion: reduce) {
    .crab { animation: none; opacity: 0; }
  }
</style>
