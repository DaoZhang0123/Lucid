<script lang="ts">
  // Inline collapsible text. Renders up to `lines` lines (default 3) with an
  // ellipsis; shows a "Show more / less" toggle when overflowing.
  // Used for long tool args/outputs (read_file dumps, launch_app traces) so
  // they don't dominate the chat scroll.
  import { _ } from "svelte-i18n";

  type Props = {
    text: string;
    lines?: number;
  };
  let { text, lines = 3 }: Props = $props();

  let expanded = $state(false);
  let measured = $state(false);
  let overflowing = $state(false);
  let el: HTMLDivElement | null = $state(null);

  // Re-measure when text changes.
  $effect(() => {
    void text;
    measured = false;
    expanded = false;
    queueMicrotask(() => {
      if (!el) return;
      // scrollHeight > clientHeight (within 1px tolerance) means clamped.
      overflowing = el.scrollHeight - el.clientHeight > 1;
      measured = true;
    });
  });
</script>

<span class="clamp-wrap">
  <span
    class="clamp-text"
    class:clamped={!expanded}
    style:--clamp-lines={lines}
    bind:this={el}
  >{text}</span>
  {#if measured && overflowing}
    <button
      type="button"
      class="clamp-toggle"
      onclick={() => (expanded = !expanded)}
    >{expanded ? $_("chat.clamp_show_less") : $_("chat.clamp_show_more")}</button>
  {/if}
</span>

<style>
  .clamp-wrap { display: inline-block; max-width: 100%; vertical-align: top; }
  .clamp-text {
    display: inline;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .clamp-text.clamped {
    display: -webkit-box;
    -webkit-line-clamp: var(--clamp-lines, 3);
    line-clamp: var(--clamp-lines, 3);
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .clamp-toggle {
    margin-left: 0.4rem;
    padding: 0;
    background: transparent;
    border: 0;
    color: #2563eb;
    cursor: pointer;
    font: inherit;
    font-size: 0.85em;
    text-decoration: underline;
  }
  .clamp-toggle:hover { color: #1d4ed8; }
</style>
