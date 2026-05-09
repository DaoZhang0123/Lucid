<script lang="ts">
  import { isLoading } from "svelte-i18n";
  import { setupI18n } from "$lib/i18n";
  import { setupTheme } from "$lib/theme";
  import ConfirmModal from "$lib/ConfirmModal.svelte";

  // Bootstrap i18n once at the root layout. Locale lookup currently falls back
  // to navigator -> "en". A user-selectable locale (saved to config.toml [ui])
  // will be wired through the settings page in a follow-up.
  setupI18n();
  // Apply persisted light/dark theme synchronously before children render.
  setupTheme();

  let { children } = $props();
</script>

{#if $isLoading}
  <div style="padding: 2rem; font-family: sans-serif; color: #6b7280;">Loading…</div>
{:else}
  {@render children()}
  <ConfirmModal />
{/if}

<style>
  /* ---------- Dark theme overrides ----------
   * Component-level styles use hex colors directly (light theme as authored).
   * Rather than touch every <style> block we re-skin the most visible surfaces
   * via :global selectors gated on data-theme="dark" at <html>. New components
   * inheriting body / textarea / input / button defaults pick this up for free.
   */
  :global(html[data-theme="dark"]) { color-scheme: dark; }
  :global(html), :global(body) {
    overflow: hidden;
    /* The .app container already manages a 100vh column with internal scroll
       regions; lock the document so a stray pixel of overflow won't surface
       Windows's right/bottom scrollbars. */
  }
  :global(html[data-theme="dark"] body) { background: #0b1220 !important; color: #e5e7eb !important; }

  /* Cards / panels that defaulted to white. */
  :global(html[data-theme="dark"] .bubble.assistant) { background: #1f2937 !important; color: #e5e7eb !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] footer) { background: #111827 !important; border-top-color: #1f2937 !important; color: #e5e7eb !important; }
  :global(html[data-theme="dark"] form button.attach) { background: #1f2937 !important; color: #cbd5e1 !important; border-color: #334155 !important; }
  :global(html[data-theme="dark"] form button.attach:hover:not(:disabled)) { background: #334155 !important; color: #f1f5f9 !important; }
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
  :global(html[data-theme="dark"] .final-max_steps),
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
  :global(html[data-theme="light"] .thread.active .t-tag) { color: #fff !important; background: rgba(255,255,255,0.22) !important; }
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
  :global(html[data-theme="dark"] .thread.active .t-tag) { color: #fff !important; background: rgba(255,255,255,0.22) !important; }
  :global(html[data-theme="dark"] .thread.active .t-id) { background: rgba(255,255,255,0.22) !important; }
</style>
