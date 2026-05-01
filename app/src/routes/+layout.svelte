<script lang="ts">
  import { isLoading } from "svelte-i18n";
  import { setupI18n } from "$lib/i18n";

  // Bootstrap i18n once at the root layout. Locale lookup currently falls back
  // to navigator -> "en". A user-selectable locale (saved to config.toml [ui])
  // will be wired through the settings page in a follow-up.
  setupI18n();

  let { children } = $props();
</script>

{#if $isLoading}
  <div style="padding: 2rem; font-family: sans-serif; color: #6b7280;">Loading…</div>
{:else}
  {@render children()}
{/if}
