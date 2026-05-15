<script lang="ts">
  /*
   * Voice overlay window. Loaded into the always-on-top, transparent,
   * click-through `voice-overlay` Tauri WebviewWindow. Listens to
   * `voice-overlay-state` events from the main process / main window for
   * state transitions and wave-form updates.
   *
   * State machine matches Docs/voice-input.md §5.1:
   *   holding       — long-press accumulator (top-edge progress bar)
   *   recording     — actively capturing audio (waveform + countdown + ✗)
   *   transcribing  — sidecar working
   *   result        — show transcribed text + ↺ undo + 🤖/⌨ mode toggle
   *   error         — red border + retry / keep / close
   *   too_short     — yellow "no speech" notice, auto-closes
   */
  import { onMount, onDestroy } from "svelte";
  import { listen, type UnlistenFn } from "@tauri-apps/api/event";

  type OverlayState =
    | "holding"
    | "recording"
    | "transcribing"
    | "result"
    | "error"
    | "too_short";

  type StatePayload = {
    state?: OverlayState;
    /** holding: 0..1 */
    holdProgress?: number;
    /** recording: elapsed milliseconds */
    recordingMs?: number;
    /** recording: max duration in milliseconds */
    maxMs?: number;
    /** result: transcribed text */
    text?: string;
    /** result: agent | dictation | auto */
    mode?: "thread_new" | "dictation_append" | "auto";
    /** error: short reason to display */
    error?: string;
    /** result: countdown until auto-dismiss (ms) */
    autoDismissMs?: number;
    /** result: dispatched intent (Docs/voice-input.md §5.2) */
    intent?: "thread_new" | "thread_abort" | "dictation_append";
    /** result: classifier confidence */
    confidence?: "high" | "medium" | "low";
    /** result: short rationale from the classifier */
    reason?: string;
    /** result: whether the host will auto-commit (controls ✓ vs auto-dismiss) */
    autoSend?: boolean;
    /** result: render the three intent-pick chips (low confidence + !autoSend) */
    showChips?: boolean;
  };

  type DispatchIntent = "thread_new" | "thread_abort" | "dictation_append";

  let state = $state<OverlayState>("holding");
  let holdProgress = $state(0);
  let recordingMs = $state(0);
  let maxMs = $state(30000);
  let text = $state("");
  let mode = $state<"thread_new" | "dictation_append" | "auto">("auto");
  let error = $state("");
  let autoDismissMs = $state(0);
  let intent = $state<DispatchIntent>("thread_new");
  let confidence = $state<"high" | "medium" | "low">("high");
  let reason = $state("");
  let autoSend = $state(true);
  let showChips = $state(false);

  // wave-form bars driven by a self-running timer when recording — no real
  // PCM hooked up here (audio capture happens in the main window's voice.ts);
  // the bars are a visual proxy for "we're listening".
  let waveBars = $state<number[]>(Array(24).fill(0.2));
  let waveTimer: number | null = null;
  function startWave() {
    if (waveTimer !== null) return;
    waveTimer = window.setInterval(() => {
      waveBars = waveBars.map(() => 0.15 + Math.random() * 0.85);
    }, 90);
  }
  function stopWave() {
    if (waveTimer !== null) { clearInterval(waveTimer); waveTimer = null; }
  }

  let unlisten: UnlistenFn | null = null;

  onMount(async () => {
    // Pick up an initial state from the URL hash so we render correctly on
    // first paint (before the host has had a chance to push a state event).
    const hash = window.location.hash.replace(/^#/, "");
    if (hash) state = (hash as OverlayState) || "holding";

    unlisten = await listen<StatePayload>("voice-overlay-state", (ev) => {
      const p = ev.payload || {};
      if (p.state) state = p.state;
      if (typeof p.holdProgress === "number") holdProgress = p.holdProgress;
      if (typeof p.recordingMs === "number") recordingMs = p.recordingMs;
      if (typeof p.maxMs === "number") maxMs = p.maxMs;
      if (typeof p.text === "string") text = p.text;
      if (p.mode) mode = p.mode;
      if (typeof p.error === "string") error = p.error;
      if (typeof p.autoDismissMs === "number") autoDismissMs = p.autoDismissMs;
      if (p.intent) intent = p.intent;
      if (p.confidence) confidence = p.confidence;
      if (typeof p.reason === "string") reason = p.reason;
      if (typeof p.autoSend === "boolean") autoSend = p.autoSend;
      if (typeof p.showChips === "boolean") showChips = p.showChips;
    });
  });

  onDestroy(() => {
    stopWave();
    if (unlisten) unlisten();
  });

  // Drive the wave-form lifecycle from state.
  $effect(() => {
    if (state === "recording") startWave();
    else stopWave();
  });

  // ---- interactive controls --------------------------------------------
  // Window is interactive (cursor not ignored) so the controls work directly
  // and `data-tauri-drag-region` lets the user reposition the overlay.

  function emitFromOverlay(action: string, payload: Record<string, unknown> = {}) {
    // The host (main window) listens for these and reacts. Re-uses the same
    // `voice-overlay-state` event name with a `from: "overlay"` tag so the
    // host can distinguish.
    try {
      // dynamic import to keep the bundle small at first paint
      import("@tauri-apps/api/event").then(({ emit }) => {
        emit("voice-overlay-action", { action, ...payload });
      });
    } catch { /* ignore */ }
  }

  function onCancel() { emitFromOverlay("cancel"); }
  function onRetry() { emitFromOverlay("retry"); }
  function onClose() { emitFromOverlay("close"); }
  function onToggleMode() {
    mode = mode === "thread_new" ? "dictation_append" : "thread_new";
    emitFromOverlay("set_mode", { mode });
  }
  function onPickIntent(next: DispatchIntent) {
    intent = next;
    confidence = "high";
    emitFromOverlay("pick_intent", { intent: next });
  }
  function onCommit() {
    emitFromOverlay("commit");
  }

  // Pretty label + icon for the dispatch chips and result header.
  function intentIcon(i: DispatchIntent): string {
    if (i === "thread_new") return "\u{1F916}"; // robot
    if (i === "thread_abort") return "\u23F9"; // stop
    return "\u2328"; // keyboard
  }
  function intentLabel(i: DispatchIntent): string {
    if (i === "thread_new") return "New task";
    if (i === "thread_abort") return "Stop task";
    return "Dictation";
  }
  function intentAccent(i: DispatchIntent): string {
    if (i === "thread_new") return "#22c55e";
    if (i === "thread_abort") return "#ef4444";
    return "#3b82f6";
  }
  function intentGlow(i: DispatchIntent): string {
    if (i === "thread_new") return "#86efac";
    if (i === "thread_abort") return "#fca5a5";
    return "#93c5fd";
  }

  // Pretty-print mm:ss countdown for the recording state.
  function fmtCountdown(remainingMs: number): string {
    const s = Math.max(0, Math.ceil(remainingMs / 1000));
    return `${s}s`;
  }

  // Derived values
  const remainingMs = $derived(Math.max(0, maxMs - recordingMs));
  const recordingProgress = $derived(maxMs > 0 ? Math.min(1, recordingMs / maxMs) : 0);
  // Color shifts blue → yellow → red over the last 5 seconds
  const ringColor = $derived(
    remainingMs < 1000 ? "#dc2626" :
    remainingMs < 5000 ? "#f59e0b" :
    "#3b82f6"
  );
  // Holding ring colour transitions blue → green as the user gets closer to
  // the threshold; the orb glow follows along.
  const holdAccent = $derived(
    holdProgress >= 0.95 ? "#22c55e" :
    holdProgress >= 0.5  ? "#a3e635" :
                           "#3b82f6"
  );
  const holdGlow = $derived(
    holdProgress >= 0.95 ? "#86efac" :
    holdProgress >= 0.5  ? "#bef264" :
                           "#93c5fd"
  );
</script>

<svelte:head>
  <title>Lucid voice</title>
</svelte:head>

{#snippet orb(stateName: OverlayState, ringProgress: number, accent: string, glow: string)}
  <div class="orb-wrap" aria-hidden="true">
    <svg class="orb-ring" viewBox="0 0 72 72" width="72" height="72">
      <!-- track -->
      <circle cx="36" cy="36" r="32" stroke="rgba(255,255,255,0.08)" stroke-width="3" fill="none" />
      <!-- progress arc; rotated -90deg so it starts at 12 o'clock -->
      <circle
        cx="36" cy="36" r="32" fill="none"
        stroke={accent}
        stroke-width="3"
        stroke-linecap="round"
        stroke-dasharray={2 * Math.PI * 32}
        stroke-dashoffset={2 * Math.PI * 32 * (1 - ringProgress)}
        transform="rotate(-90 36 36)"
        style="transition: stroke-dashoffset 90ms linear, stroke 180ms ease;"
      />
    </svg>
    <div
      class="orb-core"
      class:pulse={stateName === "recording"}
      class:spin={stateName === "transcribing"}
      style="background: radial-gradient(circle at 30% 30%, {glow}, {accent}); box-shadow: 0 0 18px {accent}66;"
    >
      {#if stateName === "result"}
        <span class="orb-icon">{intentIcon(intent)}</span>
      {:else if stateName === "error"}
        <span class="orb-icon">!</span>
      {:else if stateName === "too_short"}
        <span class="orb-icon">?</span>
      {:else}
        <span class="orb-dot"></span>
      {/if}
    </div>
  </div>
{/snippet}

<div class="overlay-root" data-state={state} data-tauri-drag-region role="status" aria-live="polite">
  {#if state === "holding"}
    {@render orb(state, holdProgress, holdAccent, holdGlow)}
    <div class="info" data-tauri-drag-region>
      <div class="title">{Math.round(holdProgress * 100)}%</div>
      <div class="sub">Keep holding to record…</div>
    </div>
  {:else if state === "recording"}
    {@render orb(state, recordingProgress, ringColor, ringColor)}
    <div class="info" data-tauri-drag-region>
      <div class="title-row">
        <span class="title">Listening</span>
        <span class="countdown" style="color: {ringColor};">{fmtCountdown(remainingMs)}</span>
      </div>
      <div class="wave" aria-hidden="true">
        {#each waveBars as h, i (i)}
          <span class="bar" style="height: {Math.round(h * 100)}%; background: linear-gradient(180deg, {ringColor}cc, {ringColor});"></span>
        {/each}
      </div>
    </div>
    <button class="ctl close" type="button" title="Cancel (Esc)" onclick={onCancel}>✗</button>
  {:else if state === "transcribing"}
    {@render orb(state, 1, "#a855f7", "#c084fc")}
    <div class="info" data-tauri-drag-region>
      <div class="title">Transcribing…</div>
      <div class="sub">Whisper is working locally</div>
    </div>
  {:else if state === "result"}
    {@render orb(state, 1, intentAccent(intent), intentGlow(intent))}
    <div class="info" data-tauri-drag-region>
      <div class="title-row">
        <span class="title small">{intentLabel(intent)}</span>
        {#if confidence !== "high"}
          <span class="conf" title={reason}>?</span>
        {/if}
      </div>
      <div class="result-text" title={text}>{text || "(empty)"}</div>
      {#if showChips}
        <div class="chip-row">
          <button
            type="button"
            class="chip"
            class:active={intent === "thread_new"}
            style="--chip-accent: {intentAccent('thread_new')};"
            onclick={() => onPickIntent("thread_new")}
            title="Start a new agent task"
          >{intentIcon("thread_new")} {intentLabel("thread_new")}</button>
          <button
            type="button"
            class="chip"
            class:active={intent === "thread_abort"}
            style="--chip-accent: {intentAccent('thread_abort')};"
            onclick={() => onPickIntent("thread_abort")}
            title="Cancel the running task"
          >{intentIcon("thread_abort")} {intentLabel("thread_abort")}</button>
          <button
            type="button"
            class="chip"
            class:active={intent === "dictation_append"}
            style="--chip-accent: {intentAccent('dictation_append')};"
            onclick={() => onPickIntent("dictation_append")}
            title="Insert into the focused input"
          >{intentIcon("dictation_append")} {intentLabel("dictation_append")}</button>
        </div>
      {/if}
    </div>
    {#if autoSend}
      <button class="ctl close" type="button" title="Cancel" onclick={onCancel}>✗</button>
    {:else}
      <button class="ctl mode" type="button" title="Send now" onclick={onCommit}>✓</button>
      <button class="ctl close" type="button" title="Cancel (don't send)" onclick={onCancel}>✗</button>
    {/if}
  {:else if state === "too_short"}
    {@render orb(state, 1, "#f59e0b", "#fcd34d")}
    <div class="info warn" data-tauri-drag-region>
      <div class="title">No speech detected</div>
      <div class="sub">Try again — speak right after the ring fills.</div>
    </div>
  {:else if state === "error"}
    {@render orb(state, 1, "#ef4444", "#fca5a5")}
    <div class="info" data-tauri-drag-region>
      <div class="title">{error || "Voice transcription failed"}</div>
      <div class="btn-row">
        <button type="button" class="mini" onclick={onRetry}>Retry</button>
        <button type="button" class="mini" onclick={onClose}>Close</button>
      </div>
    </div>
  {/if}
</div>

<style>
  :global(html), :global(body) {
    margin: 0;
    background: transparent;
    overflow: hidden;
    user-select: none;
    -webkit-user-select: none;
  }

  .overlay-root {
    width: 100vw;
    height: 100vh;
    display: flex;
    align-items: center;
    gap: 14px;
    color: #f8fafc;
    font: 13px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif;
    padding: 10px 14px 10px 10px;
    box-sizing: border-box;
    background: rgba(15, 23, 42, 0.96);
    backdrop-filter: blur(14px) saturate(140%);
    -webkit-backdrop-filter: blur(14px) saturate(140%);
    border-radius: 20px;
    border: 0;
    outline: none;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
    cursor: grab;
    overflow: hidden;
  }
  .overlay-root:active { cursor: grabbing; }

  /* ----- orb + ring ----- */
  .orb-wrap {
    position: relative;
    width: 72px;
    height: 72px;
    flex: 0 0 72px;
  }
  .orb-ring {
    position: absolute;
    inset: 0;
  }
  .orb-core {
    position: absolute;
    top: 8px; left: 8px;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 240ms ease, box-shadow 240ms ease;
  }
  .orb-core.pulse { animation: orb-pulse 1.6s ease-in-out infinite; }
  .orb-core.spin  { animation: orb-spin  0.9s linear infinite; }
  .orb-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: rgba(255,255,255,0.85);
    box-shadow: 0 0 8px rgba(255,255,255,0.6);
  }
  .orb-icon { font-size: 22px; line-height: 1; }
  @keyframes orb-pulse {
    0%, 100% { transform: scale(1.0); }
    50%      { transform: scale(1.08); }
  }
  @keyframes orb-spin {
    to { transform: rotate(360deg); }
  }

  /* ----- info column ----- */
  .info {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
  }
  .title { font-weight: 600; font-size: 14px; }
  .title.small { font-size: 12px; opacity: 0.7; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }
  .sub { font-size: 11px; opacity: 0.65; }
  .title-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .countdown { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 13px; }
  .info.warn .title { color: #fde68a; }

  /* ----- waveform (recording) ----- */
  .wave { display: flex; align-items: center; gap: 2px; height: 22px; }
  .bar {
    flex: 1;
    border-radius: 2px;
    min-height: 2px;
    transition: height 60ms linear;
  }

  /* ----- result text ----- */
  .result-text {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-style: italic;
    opacity: 0.95;
    font-size: 12px;
  }

  /* ----- buttons ----- */
  .ctl {
    background: rgba(255, 255, 255, 0.08);
    color: inherit;
    border: 0;
    border-radius: 8px;
    width: 28px;
    height: 28px;
    cursor: pointer;
    font-size: 14px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.12s, transform 0.12s;
    flex: 0 0 auto;
  }
  .ctl:hover { background: rgba(255, 255, 255, 0.18); transform: translateY(-1px); }
  .ctl.mode { width: auto; padding: 0 8px; }

  .btn-row { display: flex; gap: 6px; margin-top: 4px; }
  .btn-row .mini {
    background: rgba(255, 255, 255, 0.10);
    color: #fff;
    border: 0;
    border-radius: 6px;
    padding: 3px 8px;
    cursor: pointer;
    font: inherit;
    font-size: 11px;
  }
  .btn-row .mini:hover { background: rgba(255, 255, 255, 0.20); }

  .conf {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: rgba(245, 158, 11, 0.25);
    color: #fcd34d;
    font-size: 10px;
    font-weight: 700;
    margin-left: 6px;
    cursor: help;
  }

  .chip-row {
    display: flex;
    gap: 6px;
    margin-top: 6px;
    flex-wrap: wrap;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: rgba(255, 255, 255, 0.08);
    color: #f8fafc;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 3px 10px;
    cursor: pointer;
    font: inherit;
    font-size: 11px;
    white-space: nowrap;
    transition: background 120ms, border-color 120ms;
  }
  .chip:hover {
    background: color-mix(in srgb, var(--chip-accent) 22%, transparent);
    border-color: color-mix(in srgb, var(--chip-accent) 55%, transparent);
  }
  .chip.active {
    background: color-mix(in srgb, var(--chip-accent) 28%, transparent);
    border-color: var(--chip-accent);
  }
  .overlay-root[data-state="error"] {
    border: 0;
    background: rgba(80, 20, 20, 0.85);
    box-shadow: 0 6px 18px rgba(220, 38, 38, 0.35);
  }
</style>
