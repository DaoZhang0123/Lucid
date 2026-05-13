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
  import { invoke } from "@tauri-apps/api/core";

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
  // The window is click-through by default. Buttons need pointer events,
  // so we toggle passthrough off while the user hovers a button container.

  async function setPassthrough(on: boolean) {
    try { await invoke("voice_overlay_set_passthrough", { passthrough: on }); }
    catch { /* harmless during teardown */ }
  }

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
  function onKeepAudio() { emitFromOverlay("keep_audio"); }
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
  // Color shifts blue → yellow → red over the last 5 seconds
  const ringColor = $derived(
    remainingMs < 1000 ? "#dc2626" :
    remainingMs < 5000 ? "#f59e0b" :
    "#3b82f6"
  );
</script>

<svelte:head>
  <title>Lucid voice</title>
</svelte:head>

<div class="overlay-root" data-state={state} role="status" aria-live="polite">
  {#if state === "holding"}
    <div class="hold-bar" aria-label="Hold to record" style="width: 100%;">
      <div class="hold-fill" style="width: {Math.round(holdProgress * 100)}%;"></div>
    </div>
  {:else if state === "recording"}
    <div class="row">
      <span class="icon" aria-hidden="true">🎙️</span>
      <div class="wave" aria-hidden="true">
        {#each waveBars as h, i (i)}
          <span class="bar" style="height: {Math.round(h * 100)}%;"></span>
        {/each}
      </div>
      <div class="countdown" style="color: {ringColor};">
        {fmtCountdown(remainingMs)}
      </div>
      <button
        class="ctl"
        type="button"
        title="Cancel"
        onclick={onCancel}
        onmouseenter={() => setPassthrough(false)}
        onmouseleave={() => setPassthrough(true)}
      >✗</button>
    </div>
  {:else if state === "transcribing"}
    <div class="row">
      <span class="spinner" aria-hidden="true"></span>
      <span class="label">Transcribing…</span>
    </div>
  {:else if state === "result"}
    <div class="row result-row">
      <span class="icon" aria-hidden="true">{mode === "agent" ? "🤖" : "⌨"}</span>
      <div class="result-text" title={text}>{text || "(empty)"}</div>
      <button
        class="ctl mode"
        type="button"
        title={mode === "agent" ? "Switch to dictation" : "Switch to agent mode"}
        onclick={onToggleMode}
        onmouseenter={() => setPassthrough(false)}
        onmouseleave={() => setPassthrough(true)}
      >{mode === "agent" ? "⌨" : "🤖"}</button>
      <button
        class="ctl"
        type="button"
        title="Cancel (don't send)"
        onclick={onCancel}
        onmouseenter={() => setPassthrough(false)}
        onmouseleave={() => setPassthrough(true)}
      >✗</button>
    </div>
  {:else if state === "too_short"}
    <div class="row warn">
      <span class="icon" aria-hidden="true">⚠</span>
      <span class="label">No speech detected</span>
    </div>
  {:else if state === "error"}
    <div class="error-block">
      <div class="row">
        <span class="icon" aria-hidden="true">⚠</span>
        <span class="label">{error || "Voice transcription failed"}</span>
      </div>
      <div
        class="row btn-row"
        onmouseenter={() => setPassthrough(false)}
        onmouseleave={() => setPassthrough(true)}
        role="group"
      >
        <button type="button" onclick={onRetry}>Retry</button>
        <button type="button" onclick={onKeepAudio}>Keep audio</button>
        <button type="button" onclick={onClose}>Close</button>
      </div>
    </div>
  {/if}
</div>

<style>
  .overlay-root {
    width: 100vw;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #f8fafc;
    font: 13px/1.4 -apple-system, "Segoe UI", system-ui, sans-serif;
    padding: 8px 14px;
    box-sizing: border-box;
    /* Card */
    background: rgba(15, 23, 42, 0.78);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border-radius: 16px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
  }

  /* Holding state has no card — show only a slim progress strip */
  .overlay-root[data-state="holding"] {
    background: transparent;
    border: 0;
    box-shadow: none;
    padding: 0;
    align-items: flex-start;
  }
  .hold-bar {
    width: 100%;
    height: 6px;
    background: rgba(15, 23, 42, 0.4);
    border-radius: 3px;
    overflow: hidden;
    margin-top: 4px;
  }
  .hold-fill {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, #22c55e);
    transition: width 60ms linear;
  }

  .row { display: flex; align-items: center; gap: 10px; width: 100%; }
  .icon { font-size: 18px; flex: 0 0 auto; }
  .label { flex: 1; }
  .countdown { font-variant-numeric: tabular-nums; font-weight: 600; flex: 0 0 auto; }

  .wave { flex: 1; display: flex; align-items: center; gap: 2px; height: 32px; }
  .bar {
    flex: 1;
    background: linear-gradient(180deg, #60a5fa, #3b82f6);
    border-radius: 2px;
    min-height: 2px;
    transition: height 60ms linear;
  }

  .ctl {
    background: rgba(255, 255, 255, 0.08);
    color: inherit;
    border: 0;
    border-radius: 6px;
    width: 26px;
    height: 26px;
    cursor: pointer;
    font-size: 14px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.12s;
  }
  .ctl:hover { background: rgba(255, 255, 255, 0.18); }
  .ctl.mode { width: auto; padding: 0 8px; }

  .result-row .result-text {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-style: italic;
    opacity: 0.95;
  }

  .spinner {
    width: 16px;
    height: 16px;
    border: 2px solid rgba(255, 255, 255, 0.2);
    border-top-color: #f8fafc;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    flex: 0 0 auto;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .warn { color: #fde68a; }
  .error-block {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .overlay-root[data-state="error"] {
    border-color: #b91c1c;
    background: rgba(127, 29, 29, 0.85);
  }
  .btn-row { justify-content: flex-end; gap: 6px; }
  .btn-row button {
    background: rgba(255, 255, 255, 0.10);
    color: #fff;
    border: 0;
    border-radius: 6px;
    padding: 4px 10px;
    cursor: pointer;
    font: inherit;
  }
  .btn-row button:hover { background: rgba(255, 255, 255, 0.20); }
</style>
