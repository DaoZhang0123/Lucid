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
    /** result: agent | dictation */
    mode?: "agent" | "dictation";
    /** error: short reason to display */
    error?: string;
    /** result: countdown until auto-dismiss (ms) */
    autoDismissMs?: number;
  };

  let state = $state<OverlayState>("holding");
  let holdProgress = $state(0);
  let recordingMs = $state(0);
  let maxMs = $state(30000);
  let text = $state("");
  let mode = $state<"agent" | "dictation">("agent");
  let error = $state("");
  let autoDismissMs = $state(0);

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
    mode = mode === "agent" ? "dictation" : "agent";
    emitFromOverlay("set_mode", { mode });
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
        <span class="orb-icon">{mode === "agent" ? "🤖" : "⌨"}</span>
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
    {@render orb(state, 1, mode === "agent" ? "#22c55e" : "#3b82f6", mode === "agent" ? "#86efac" : "#93c5fd")}
    <div class="info" data-tauri-drag-region>
      <div class="title-row">
        <span class="title small">{mode === "agent" ? "Sending to agent" : "Inserting"}</span>
      </div>
      <div class="result-text" title={text}>{text || "(empty)"}</div>
    </div>
    <button
      class="ctl mode"
      type="button"
      title={mode === "agent" ? "Switch to dictation" : "Switch to agent mode"}
      onclick={onToggleMode}
    >{mode === "agent" ? "⌨" : "🤖"}</button>
    <button class="ctl close" type="button" title="Cancel (don't send)" onclick={onCancel}>✗</button>
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
    background: rgba(15, 23, 42, 0.78);
    backdrop-filter: blur(14px) saturate(140%);
    -webkit-backdrop-filter: blur(14px) saturate(140%);
    border-radius: 20px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.45);
    cursor: grab;
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

  .overlay-root[data-state="error"] {
    border-color: rgba(220, 38, 38, 0.6);
    background: rgba(80, 20, 20, 0.85);
  }
</style>
