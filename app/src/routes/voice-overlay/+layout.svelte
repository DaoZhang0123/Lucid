<script lang="ts">
  // The voice overlay is its own Tauri WebviewWindow — must NOT inherit the
  // root layout (titlebar, splash, ConfirmModal). This empty layout overrides
  // the parent so the overlay route renders on a fully transparent body.
  let { children } = $props();
</script>

{@render children()}

<style>
  /* Make the overlay window fully transparent. The Tauri webview is
     created with transparent=true; we still need the document/html to
     have no background so the rounded card in +page.svelte sits on a
     transparent canvas.

     The :global on html/body matches the SvelteKit-generated root. */
  :global(html), :global(body) {
    background: transparent !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    /* Disable text selection so click-drag inside the overlay never highlights */
    user-select: none;
    -webkit-user-select: none;
  }
</style>
