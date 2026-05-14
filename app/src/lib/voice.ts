/**
 * Voice input controller (push-to-talk).
 *
 * Lives in the **main window** (not the overlay window). Owns:
 *   • registering / unregistering the global PTT shortcut via Rust;
 *   • the long-press state machine (Pressed → timer → enterRecording);
 *   • the MediaRecorder lifecycle (getUserMedia, capture, base64 encode);
 *   • driving the overlay window state via voice_overlay_show /
 *     set_state / hide;
 *   • routing the transcribed text:  agent mode → startTask;
 *                                    dictation mode → callback into the page.
 *
 * The overlay window is a thin renderer: it only reads our state-event
 * payloads and emits `voice-overlay-action` for buttons (cancel / retry /
 * mode-toggle).
 *
 * See Docs/voice-input.md §4 §5 §8 for the full state machine.
 */
import { invoke } from "@tauri-apps/api/core";
import { listen, emit, type UnlistenFn } from "@tauri-apps/api/event";
import { startTask, newThread } from "./chatStore.svelte";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type VoiceMode = "agent" | "dictation";
export type StopMode = "release" | "tap_again" | "auto_silence";
export type StartFeedback = "beep" | "vibrate-tray" | "silent";

export interface VoiceConfig {
  enabled: boolean;
  hotkey: string;
  hold_threshold_ms: number;
  stop_mode: StopMode;
  start_feedback: StartFeedback;
  focus_aware: boolean;
  mode: VoiceMode;
  always_new_thread: boolean;
  max_seconds: number;
  overlay_screen: string;
  overlay_y_offset_px: number;
}

export interface TranscribeResult {
  text: string;
  language?: string;
  duration_ms?: number;
  confidence?: number;
  engine?: string;
  model?: string;
  filtered_reason?: string;
}

interface DictationCallback {
  (text: string): void;
}

// ---------------------------------------------------------------------------
// Module-level state (singleton — only ever one PTT controller per process)
// ---------------------------------------------------------------------------

type PtState =
  | "idle"
  | "holding"      // pressed but threshold not met yet
  | "armed"        // ready to record (start_feedback played, recorder starting)
  | "recording"
  | "transcribing"
  | "result"       // showing result, awaiting auto-dismiss
  | "error";

let cfg: VoiceConfig | null = null;
let stateName: PtState = "idle";

// long-press timer
let holdTimer: number | null = null;
let holdStartedAt = 0;

// recording
let mediaStream: MediaStream | null = null;
let mediaRecorder: MediaRecorder | null = null;
let recordedChunks: Blob[] = [];
let recordingStartedAt = 0;
let recordingTickTimer: number | null = null;
let recordingHardStopTimer: number | null = null;

// result auto-dismiss
let resultDismissTimer: number | null = null;
let pendingResult: TranscribeResult | null = null;
let pendingResultMode: VoiceMode = "agent";
let cancelledResult = false;

// listeners
let unlistenHotkey: UnlistenFn | null = null;
let unlistenOverlayAction: UnlistenFn | null = null;

let dictationSink: DictationCallback | null = null;
let lastTickerProgress = 0;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Set the callback that receives transcribed text in dictation mode. */
export function setDictationSink(cb: DictationCallback | null): void {
  dictationSink = cb;
}

/** Initialise: load config, register hotkey, subscribe to events. Idempotent. */
export async function initVoice(): Promise<void> {
  await loadConfigAndApply();
  if (!unlistenHotkey) {
    unlistenHotkey = await listen<{ kind: "pressed" | "released" }>(
      "lucid://voice",
      (ev) => onHotkeyEvent(ev.payload.kind),
    );
  }
  if (!unlistenOverlayAction) {
    unlistenOverlayAction = await listen<{ action: string; mode?: VoiceMode }>(
      "voice-overlay-action",
      (ev) => onOverlayAction(ev.payload),
    );
  }
}

/** Tear down (e.g. on app quit). Currently unused but kept for symmetry. */
export async function teardownVoice(): Promise<void> {
  if (unlistenHotkey) { unlistenHotkey(); unlistenHotkey = null; }
  if (unlistenOverlayAction) { unlistenOverlayAction(); unlistenOverlayAction = null; }
  await unregisterHotkey();
  resetAll();
}

/** Re-read config from sidecar and re-register the hotkey. Call after /settings save. */
export async function reloadVoiceConfig(): Promise<void> {
  await loadConfigAndApply();
}

// ---------------------------------------------------------------------------
// Config & hotkey wiring
// ---------------------------------------------------------------------------

async function loadConfigAndApply(): Promise<void> {
  let next: VoiceConfig | null = null;
  try {
    next = await invoke<VoiceConfig>("voice_config");
  } catch (e) {
    console.warn("voice_config failed:", e);
    return;
  }
  if (!next) return;
  const wasEnabled = cfg?.enabled ?? false;
  const wasHotkey = cfg?.hotkey ?? "";
  cfg = next;
  if (next.enabled) {
    if (!wasEnabled || wasHotkey !== next.hotkey) {
      try {
        await invoke("voice_register_hotkey", {
          hotkey: next.hotkey,
          holdThresholdMs: next.hold_threshold_ms,
        });
      } catch (e) {
        console.warn(`failed to register voice hotkey "${next.hotkey}":`, e);
      }
    }
  } else if (wasEnabled) {
    await unregisterHotkey();
  }
}

async function unregisterHotkey(): Promise<void> {
  try { await invoke("voice_unregister_hotkey"); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Hotkey event handler — long-press detection
// ---------------------------------------------------------------------------

function focusedOnEditable(): boolean {
  if (!cfg?.focus_aware) return false;
  // Bare-Space hotkey on Windows uses an OS-level keyboard hook that already
  // distinguishes tap (passthrough) from hold (PTT). When `pressed` fires, the
  // user definitely held long enough for the grace window to elapse, so we
  // must NOT short-circuit on focus — that would silently drop their PTT.
  const hk = (cfg.hotkey || "").trim().toLowerCase();
  if (hk === "space" || hk === "spacebar") return false;
  const el = document.activeElement;
  if (!el) return false;
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return true;
  if ((el as HTMLElement).isContentEditable) return true;
  return false;
}

function onHotkeyEvent(kind: "pressed" | "released"): void {
  if (!cfg?.enabled) return;
  if (kind === "pressed") onPressed();
  else onReleased();
}

function onPressed(): void {
  // The webview having focus AND the user being on a real input element →
  // pass the keystroke through (don't trigger PTT).
  if (focusedOnEditable()) return;
  // If we're already in recording / result / etc, treat further presses
  // according to stop_mode (tap_again).
  if (stateName === "recording" && cfg!.stop_mode === "tap_again") {
    // Second tap in tap_again mode = stop.
    stopRecording("tap");
    return;
  }
  if (stateName !== "idle") return;
  // Begin holding accumulator.
  stateName = "holding";
  holdStartedAt = performance.now();
  if (cfg!.hold_threshold_ms <= 0) {
    // Classic PTT — record immediately on press.
    void enterRecording();
    return;
  }
  showOverlay("holding");
  // Drive the holding progress bar from a tick timer.
  lastTickerProgress = 0;
  if (holdTimer !== null) clearInterval(holdTimer);
  holdTimer = window.setInterval(() => {
    const elapsed = performance.now() - holdStartedAt;
    const progress = Math.min(1, elapsed / cfg!.hold_threshold_ms);
    if (progress !== lastTickerProgress) {
      lastTickerProgress = progress;
      setOverlayState({ holdProgress: progress });
    }
    if (progress >= 1) {
      if (holdTimer !== null) { clearInterval(holdTimer); holdTimer = null; }
      void enterRecording();
    }
  }, 60);
}

function onReleased(): void {
  if (stateName === "holding") {
    // Released before threshold: cancel.
    if (holdTimer !== null) { clearInterval(holdTimer); holdTimer = null; }
    stateName = "idle";
    void invoke("voice_overlay_hide");
    return;
  }
  if (stateName === "recording" && cfg!.stop_mode === "release") {
    stopRecording("release");
  }
  // tap_again / auto_silence ignore release while recording.
}

// ---------------------------------------------------------------------------
// Recording lifecycle
// ---------------------------------------------------------------------------

async function enterRecording(): Promise<void> {
  stateName = "armed";
  // start feedback (best-effort, non-blocking)
  if (cfg!.start_feedback === "beep") playBeep(880, 80);

  try {
    if (!mediaStream) {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    }
  } catch (e) {
    console.error("getUserMedia failed:", e);
    showError("Microphone permission denied or unavailable");
    return;
  }
  recordedChunks = [];
  // Pick the most compact mime the browser supports.
  const mimeCandidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4",
  ];
  let chosenMime = "";
  for (const m of mimeCandidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(m)) {
      chosenMime = m;
      break;
    }
  }
  try {
    mediaRecorder = chosenMime
      ? new MediaRecorder(mediaStream, { mimeType: chosenMime, audioBitsPerSecond: 64_000 })
      : new MediaRecorder(mediaStream, { audioBitsPerSecond: 64_000 });
  } catch (e) {
    console.error("MediaRecorder init failed:", e);
    showError(`MediaRecorder error: ${e}`);
    return;
  }
  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) recordedChunks.push(e.data);
  };
  mediaRecorder.onstop = () => {
    void onRecorderStop();
  };
  mediaRecorder.onerror = (e) => {
    console.error("MediaRecorder.onerror:", e);
    showError("MediaRecorder error");
  };
  recordingStartedAt = performance.now();
  mediaRecorder.start();

  stateName = "recording";
  setOverlayState({
    state: "recording",
    recordingMs: 0,
    maxMs: cfg!.max_seconds * 1000,
    mode: cfg!.mode,
  });

  // Tick the overlay countdown.
  if (recordingTickTimer !== null) clearInterval(recordingTickTimer);
  recordingTickTimer = window.setInterval(() => {
    const elapsed = performance.now() - recordingStartedAt;
    setOverlayState({ recordingMs: elapsed });
  }, 200);

  // Hard stop at max_seconds.
  if (recordingHardStopTimer !== null) clearTimeout(recordingHardStopTimer);
  recordingHardStopTimer = window.setTimeout(() => {
    if (stateName === "recording") stopRecording("max_seconds");
  }, cfg!.max_seconds * 1000);
}

function stopRecording(reason: string): void {
  void reason;
  if (recordingTickTimer !== null) { clearInterval(recordingTickTimer); recordingTickTimer = null; }
  if (recordingHardStopTimer !== null) { clearTimeout(recordingHardStopTimer); recordingHardStopTimer = null; }
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    try { mediaRecorder.stop(); } catch { /* ignore */ }
  } else {
    // No recorder running → just bail.
    resetAll();
    void invoke("voice_overlay_hide");
  }
}

async function onRecorderStop(): Promise<void> {
  // Release mic so the OS indicator goes away promptly.
  if (mediaStream) {
    try { mediaStream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
    mediaStream = null;
  }
  const mime = mediaRecorder?.mimeType || "audio/webm";
  mediaRecorder = null;
  const blob = new Blob(recordedChunks, { type: mime });
  recordedChunks = [];
  if (blob.size === 0) {
    showError("No audio captured");
    return;
  }

  stateName = "transcribing";
  setOverlayState({ state: "transcribing" });

  // Encode to base64 (chunked to avoid stack overflow on large blobs).
  const buf = new Uint8Array(await blob.arrayBuffer());
  const b64 = uint8ToBase64(buf);

  let result: TranscribeResult;
  try {
    result = await invoke<TranscribeResult>("sidecar_transcribe", {
      args: { audioB64: b64, mime },
    });
  } catch (e) {
    showError(typeof e === "string" ? e : `Transcription failed: ${e}`);
    return;
  }
  if (!result || !result.text || result.filtered_reason) {
    // Brief "no speech" toast then auto-dismiss.
    stateName = "result";
    setOverlayState({ state: "too_short" });
    if (resultDismissTimer !== null) clearTimeout(resultDismissTimer);
    resultDismissTimer = window.setTimeout(() => {
      void invoke("voice_overlay_hide");
      resetAll();
    }, 1200);
    return;
  }

  pendingResult = result;
  pendingResultMode = cfg!.mode;
  cancelledResult = false;

  stateName = "result";
  setOverlayState({
    state: "result",
    text: result.text,
    mode: pendingResultMode,
  });

  // Reflection / undo window (1.5s for agent, 0.8s for dictation).
  const dwellMs = pendingResultMode === "agent" ? 1500 : 800;
  if (resultDismissTimer !== null) clearTimeout(resultDismissTimer);
  resultDismissTimer = window.setTimeout(() => commitResult(), dwellMs);
}

function commitResult(): void {
  if (cancelledResult || !pendingResult) {
    resetAll();
    void invoke("voice_overlay_hide");
    return;
  }
  const text = pendingResult.text;
  const mode = pendingResultMode;
  pendingResult = null;
  void invoke("voice_overlay_hide");
  resetAll();

  if (mode === "dictation" && dictationSink) {
    dictationSink(text);
  } else if (mode === "agent") {
    // always_new_thread is enforced server-side via thread switching, but the
    // simplest path here is to call startTask which appends to the active
    // thread (or creates one). If the user wants always_new, we open a new
    // thread first, then start the task.
    if (cfg?.always_new_thread) {
      void (async () => {
        try {
          await newThread();
        } catch { /* ignore */ }
        void startTask(text);
      })();
    } else {
      void startTask(text);
    }
  }
}

// ---------------------------------------------------------------------------
// Overlay action handler — buttons clicked inside the overlay window
// ---------------------------------------------------------------------------

function onOverlayAction(payload: { action: string; mode?: VoiceMode }): void {
  switch (payload.action) {
    case "cancel":
      // User changed their mind. Stop recording (if any), suppress result.
      cancelledResult = true;
      if (stateName === "recording") {
        stopRecording("cancel");
      } else {
        if (resultDismissTimer !== null) { clearTimeout(resultDismissTimer); resultDismissTimer = null; }
        resetAll();
        void invoke("voice_overlay_hide");
      }
      break;
    case "set_mode":
      if (payload.mode === "agent" || payload.mode === "dictation") {
        pendingResultMode = payload.mode;
      }
      break;
    case "retry":
      // Discard current error, hide overlay; user can press hotkey again.
      resetAll();
      void invoke("voice_overlay_hide");
      break;
    case "keep_audio":
      // Audio already kept by sidecar if cfg.keep_audio=true; otherwise
      // there's nothing to do here. Treat as close.
      resetAll();
      void invoke("voice_overlay_hide");
      break;
    case "close":
      resetAll();
      void invoke("voice_overlay_hide");
      break;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function showOverlay(initial: string): void {
  void invoke("voice_overlay_show", {
    args: {
      screen: cfg?.overlay_screen ?? "cursor",
      yOffsetPx: cfg?.overlay_y_offset_px ?? 8,
      state: initial,
    },
  });
}

function setOverlayState(payload: Record<string, unknown>): void {
  // Bypasses Rust round-trip when overlay is in same process; emit directly.
  void emit("voice-overlay-state", payload);
}

function showError(reason: string): void {
  stateName = "error";
  setOverlayState({ state: "error", error: reason });
}

function resetAll(): void {
  stateName = "idle";
  if (holdTimer !== null) { clearInterval(holdTimer); holdTimer = null; }
  if (recordingTickTimer !== null) { clearInterval(recordingTickTimer); recordingTickTimer = null; }
  if (recordingHardStopTimer !== null) { clearTimeout(recordingHardStopTimer); recordingHardStopTimer = null; }
  if (resultDismissTimer !== null) { clearTimeout(resultDismissTimer); resultDismissTimer = null; }
  if (mediaStream) {
    try { mediaStream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
    mediaStream = null;
  }
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    try { mediaRecorder.stop(); } catch { /* ignore */ }
  }
  mediaRecorder = null;
  recordedChunks = [];
  pendingResult = null;
  cancelledResult = false;
}

function uint8ToBase64(arr: Uint8Array): string {
  // Process in chunks so we don't hit the call-stack ceiling on large blobs.
  const CHUNK = 0x8000;
  let s = "";
  for (let i = 0; i < arr.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, Array.from(arr.subarray(i, i + CHUNK)) as number[]);
  }
  return btoa(s);
}

function playBeep(freq: number, durMs: number): void {
  try {
    const Ctx: typeof AudioContext | undefined =
      window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
    osc.type = "sine";
    gain.gain.value = 0.08;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    setTimeout(() => {
      try {
        osc.stop();
        ctx.close();
      } catch { /* ignore */ }
    }, durMs);
  } catch { /* ignore */ }
}
