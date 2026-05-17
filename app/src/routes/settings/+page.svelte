<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onDestroy, onMount } from "svelte";
  import { _, locale } from "svelte-i18n";
  import { SUPPORTED_LOCALES, LOCALE_LABELS, saveLocale, type SupportedLocale } from "$lib/i18n";
  import { reloadVoiceConfig } from "$lib/voice";

  type Provider = "anthropic" | "copilot" | "proxy";

  // Mirror current i18n locale into a local $state so <select bind:value> works.
  // The store is also read directly in the change handler to persist + apply.
  let uiLocale = $state<string>($locale ?? "en");
  $effect(() => { uiLocale = $locale ?? uiLocale; });

  function onLocaleChange() {
    void locale.set(uiLocale);
    saveLocale(uiLocale);
  }

  let path = $state("");
  let provider = $state<Provider>("anthropic");
  // proxy
  let baseUrl = $state("http://localhost:4000");
  let model = $state("claude-opus-4.6");
  let apiKey = $state("");
  // anthropic
  let anthApiKey = $state("");
  let anthModel = $state("claude-opus-4-5-20250929");
  let anthBaseUrl = $state("https://api.anthropic.com");
  // copilot
  let copModel = $state("claude-opus-4-6");

  let temperature = $state<number>(0.2);
  let topP = $state<number>(1.0);
  let emergencyHotkey = $state("ctrl+alt+esc");

  // ---- hotkey capture -----------------------------------------------
  // Tracks which hotkey input is currently capturing key presses, so the
  // shared keydown handler knows whether to write into emergencyHotkey or
  // vHotkey. Set on focus, cleared on blur. We don't try to be a full
  // global recorder — the user clicks the field and presses the combo.
  let capturingHotkeyFor = $state<"emergency" | "voice" | null>(null);

  // Normalise a Tauri-compatible accelerator string from a KeyboardEvent.
  // Format: lower-case modifiers joined by '+', then the main key.
  // Returns null if the event is just a modifier (Shift / Ctrl / Alt /
  // Meta) on its own — we wait for a real key.
  function eventToHotkey(e: KeyboardEvent): string | null {
    const code = e.code; // physical key, locale-independent
    const key = e.key;
    // Skip pure-modifier presses.
    if (
      key === "Control" || key === "Shift" || key === "Alt" ||
      key === "Meta" || key === "OS" || key === "Hyper" || key === "Super" ||
      key === "AltGraph"
    ) {
      return null;
    }
    const mods: string[] = [];
    if (e.ctrlKey) mods.push("ctrl");
    if (e.altKey) mods.push("alt");
    if (e.shiftKey) mods.push("shift");
    if (e.metaKey) mods.push("meta");

    let main = "";
    if (code.startsWith("Key")) {
      main = code.slice(3).toLowerCase();          // KeyA → a
    } else if (code.startsWith("Digit")) {
      main = code.slice(5);                         // Digit1 → 1
    } else if (code.startsWith("Numpad")) {
      main = "num" + code.slice(6).toLowerCase();   // NumpadEnter → numenter
    } else if (code.startsWith("Arrow")) {
      main = code.slice(5).toLowerCase();           // ArrowLeft → left
    } else if (/^F\d{1,2}$/.test(code)) {
      main = code.toLowerCase();                    // F9 → f9
    } else {
      // Common named keys → tauri global-shortcut accelerator names.
      const named: Record<string, string> = {
        "Space": "Space",
        "Enter": "Return",
        "Escape": "Escape",
        "Backspace": "Backspace",
        "Tab": "Tab",
        "Delete": "Delete",
        "Home": "Home",
        "End": "End",
        "PageUp": "PageUp",
        "PageDown": "PageDown",
        "Insert": "Insert",
        "Minus": "-",
        "Equal": "=",
        "BracketLeft": "[",
        "BracketRight": "]",
        "Backslash": "\\",
        "Semicolon": ";",
        "Quote": "'",
        "Comma": ",",
        "Period": ".",
        "Slash": "/",
        "Backquote": "`",
      };
      main = named[code] ?? key;
    }
    if (!main) return null;
    return mods.length ? mods.join("+") + "+" + main : main;
  }

  function onHotkeyKeydown(target: "emergency" | "voice", e: KeyboardEvent): void {
    if (capturingHotkeyFor !== target) return;
    e.preventDefault();
    e.stopPropagation();
    const combo = eventToHotkey(e);
    if (!combo) return; // pure modifier, keep waiting
    if (target === "emergency") emergencyHotkey = combo;
    else vHotkey = combo;
    // Drop focus so the user can immediately Save without re-pressing.
    (e.currentTarget as HTMLInputElement | null)?.blur();
  }

  // ---- voice ---------------------------------------------------------
  let vEnabled = $state(false);
  let vEngine = $state("faster-whisper");
  let vModelSize = $state("tiny");
  let vLanguage = $state("auto");
  let vHotkey = $state("Space");
  let vHoldThresholdMs = $state(5000);
  let vStopMode = $state("tap_again");
  let vStartFeedback = $state("beep");
  let vMode = $state("auto");
  let vAutoSend = $state(false);
  let vMaxSeconds = $state(30);
  let vHfEndpoint = $state("");
  // Preset selection for the HF endpoint dropdown. Stays in sync with
  // `vHfEndpoint` (saved value): empty string means "auto fallback",
  // a known mirror URL selects that preset, anything else picks
  // "custom" and reveals the URL field.
  const HF_ENDPOINT_PRESETS = new Set(["", "https://hf-mirror.com", "https://huggingface.tuna.tsinghua.edu.cn", "https://huggingface.co"]);
  let vHfEndpointPreset = $state("");
  let voiceSaving = $state(false);
  let voiceSavedAt = $state("");
  let voiceError = $state("");
  // model download/cache state
  let vModelLocation = $state(""); // "" | "user" | "bundled"
  let vModelDownloading = $state(false);
  let vModelDownloadResult = $state(""); // success message
  let vModelDownloadError = $state("");
  let vModelPickerOpen = $state(false);
  let vModelPickerChoice = $state("small");

  // Catalogue used by the "Download model" picker. Sizes are approximate
  // on-disk size of the INT8 quantised faster-whisper variants from
  // Systran/faster-whisper-* on HuggingFace.
  const VOICE_MODEL_OPTIONS: Array<{
    id: string;
    sizeMb: number;
    multilingual: boolean;
    accuracy: "low" | "medium" | "high" | "very-high";
  }> = [
    { id: "tiny",             sizeMb: 75,   multilingual: true,  accuracy: "low" },
    { id: "tiny.en",          sizeMb: 75,   multilingual: false, accuracy: "low" },
    { id: "base",             sizeMb: 145,  multilingual: true,  accuracy: "medium" },
    { id: "base.en",          sizeMb: 145,  multilingual: false, accuracy: "medium" },
    { id: "small",            sizeMb: 488,  multilingual: true,  accuracy: "high" },
    { id: "distil-small.en",  sizeMb: 166,  multilingual: false, accuracy: "high" },
    { id: "medium",           sizeMb: 1500, multilingual: true,  accuracy: "high" },
    { id: "distil-large-v3",  sizeMb: 800,  multilingual: false, accuracy: "very-high" },
    { id: "large-v3",         sizeMb: 3000, multilingual: true,  accuracy: "very-high" },
  ];
  let saving = $state(false);
  let savedAt = $state("");
  let error = $state("");

  // Active tab in the left sidebar nav.
  type Tab = "general" | "llm" | "voice" | "about";
  let activeTab = $state<Tab>("general");

  // GitHub Copilot OAuth state
  type CopStatus = { logged_in: boolean; github_user?: string | null; copilot_expires_at?: number | null; state_file?: string };
  let copStatus = $state<CopStatus>({ logged_in: false });
  let copBusy = $state(false);
  let copError = $state("");
  type CopDevice = { device_code: string; user_code: string; verification_uri: string; interval: number; expires_in: number };
  let copDevice = $state<CopDevice | null>(null);
  let copPollTimer: ReturnType<typeof setInterval> | null = null;

  onMount(async () => {
    try {
      const cfg = (await invoke("read_settings")) as {
        path: string;
        provider: string;
        temperature: number | null;
        top_p: number | null;
        emergency_hotkey?: string;
        proxy: { base_url: string; model: string; api_key: string };
        anthropic: { api_key: string; model: string; base_url: string };
        copilot: { model: string };
        voice?: {
          enabled: boolean | null;
          engine: string;
          model_size: string;
          language: string;
          hotkey: string;
          hold_threshold_ms: number | null;
          stop_mode: string;
          start_feedback: string;
          mode: string;
          auto_send: boolean | null;
          max_seconds: number | null;
          overlay_screen: string;
          hf_endpoint: string;
        };
      };
      path = cfg.path;
      if (cfg.provider === "anthropic" || cfg.provider === "copilot") {
        provider = cfg.provider;
      } else if (cfg.provider === "proxy") {
        // Proxy provider has been removed from the settings UI; map any
        // legacy stored value to anthropic so the form has a valid selection.
        // The on-disk value stays untouched until the user explicitly saves.
        provider = "anthropic";
      }
      if (cfg.proxy?.base_url) baseUrl = cfg.proxy.base_url;
      if (cfg.proxy?.model) model = cfg.proxy.model;
      if (cfg.proxy?.api_key) apiKey = cfg.proxy.api_key;
      if (cfg.anthropic?.api_key) anthApiKey = cfg.anthropic.api_key;
      if (cfg.anthropic?.model) anthModel = cfg.anthropic.model;
      if (cfg.anthropic?.base_url) anthBaseUrl = cfg.anthropic.base_url;
      if (cfg.copilot?.model) copModel = cfg.copilot.model;
      if (typeof cfg.temperature === "number") temperature = cfg.temperature;
      if (typeof cfg.top_p === "number") topP = cfg.top_p;
      if (cfg.emergency_hotkey) emergencyHotkey = cfg.emergency_hotkey;
      // voice
      const v = cfg.voice;
      if (v) {
        if (typeof v.enabled === "boolean") vEnabled = v.enabled;
        if (v.engine) vEngine = v.engine;
        if (v.model_size) vModelSize = v.model_size;
        if (typeof v.language === "string") {
          const lang = v.language.trim().toLowerCase();
          // Only en / zh / fr are supported transcription targets (matches
          // sidecar's lucid.voice.SUPPORTED_LANGS). Any other value
          // ("", "system", "detect", legacy two-letter codes) collapses
          // to "auto" so the dropdown displays a valid option.
          vLanguage = (lang === "en" || lang === "zh" || lang === "fr") ? lang : "auto";
        }
        if (v.hotkey) vHotkey = v.hotkey;
        if (typeof v.hold_threshold_ms === "number") vHoldThresholdMs = v.hold_threshold_ms;
        if (v.stop_mode) vStopMode = v.stop_mode;
        if (v.start_feedback) vStartFeedback = v.start_feedback;
        if (v.mode) {
          // Back-compat: rename the legacy hard-mode values.
          const m = v.mode === "agent" ? "thread_new"
                  : v.mode === "dictation" ? "dictation_append"
                  : v.mode;
          vMode = m;
        }
        if (typeof v.auto_send === "boolean") vAutoSend = v.auto_send;
        if (typeof v.max_seconds === "number") vMaxSeconds = v.max_seconds;
        if (typeof v.hf_endpoint === "string") {
          vHfEndpoint = v.hf_endpoint;
          vHfEndpointPreset = HF_ENDPOINT_PRESETS.has(v.hf_endpoint) ? v.hf_endpoint : "custom";
        }
      }
    } catch (e) {
      error = String(e);
    }
    await refreshCopilotStatus();
    void refreshModelStatus();
  });

  onDestroy(() => {
    if (copPollTimer) clearInterval(copPollTimer);
  });

  async function save() {
    saving = true;
    error = "";
    savedAt = "";
    try {
      await invoke("write_settings", {
        patch: {
          provider,
          temperature,
          top_p: topP,
          emergency_hotkey: emergencyHotkey,
          proxy: { base_url: baseUrl, model, api_key: apiKey },
          anthropic: { api_key: anthApiKey, model: anthModel, base_url: anthBaseUrl },
          copilot: { model: copModel },
        },
      });
      // Try a hot reload; if a task is running it'll be rejected — user needs to wait.
      try {
        await invoke("reload_config");
      } catch {
        /* ignore — settings still take effect on next sidecar restart */
      }
      savedAt = new Date().toLocaleTimeString();
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }

  function onHfPresetChange() {
    // Built-in presets overwrite the saved URL; "custom" keeps whatever
    // the user previously typed (or starts blank if they had a preset).
    if (vHfEndpointPreset !== "custom") {
      vHfEndpoint = vHfEndpointPreset;
    } else if (HF_ENDPOINT_PRESETS.has(vHfEndpoint)) {
      vHfEndpoint = "";
    }
  }

  async function saveVoice() {
    voiceSaving = true;
    voiceError = "";
    voiceSavedAt = "";
    try {
      await invoke("write_settings", {
        patch: {
          voice: {
            enabled: vEnabled,
            engine: vEngine,
            model_size: vModelSize,
            language: vLanguage,
            hotkey: vHotkey,
            hold_threshold_ms: vHoldThresholdMs,
            stop_mode: vStopMode,
            start_feedback: vStartFeedback,
            mode: vMode,
            auto_send: vAutoSend,
            max_seconds: vMaxSeconds,
            hf_endpoint: vHfEndpoint,
          },
        },
      });
      try { await invoke("reload_config"); } catch { /* ignore — applies on next launch */ }
      // Re-register hotkey + push fresh cfg into the long-press controller.
      try { await reloadVoiceConfig(); } catch (e) { console.warn("reloadVoiceConfig failed:", e); }
      voiceSavedAt = new Date().toLocaleTimeString();
      // Refresh model location after save (model_size may have changed).
      void refreshModelStatus();
    } catch (e) {
      voiceError = String(e);
    } finally {
      voiceSaving = false;
    }
  }

  async function refreshModelStatus() {
    try {
      const r = (await invoke("voice_model_status", { args: { modelSize: vModelSize } })) as { location: string };
      vModelLocation = r?.location ?? "";
    } catch (e) {
      console.warn("voice_model_status failed:", e);
      vModelLocation = "";
    }
  }

  async function downloadModel(size?: string) {
    const target = (size || vModelPickerChoice || vModelSize || "small").trim();
    vModelDownloading = true;
    vModelDownloadError = "";
    vModelDownloadResult = "";
    vModelPickerOpen = false;
    try {
      const r = (await invoke("voice_download_model", {
        args: { modelSize: target, hfEndpoint: vHfEndpoint || null },
      })) as { ok: boolean; size_mb?: number; error?: string };
      if (r?.ok) {
        vModelDownloadResult = $_("settings.voice_model_download_done", {
          values: { size: target, mb: r.size_mb ?? 0 },
        });
        // After a successful download, switch the active model to what the
        // user just downloaded — that's almost always what they want.
        vModelSize = target;
        await refreshModelStatus();
      } else {
        vModelDownloadError = r?.error || "unknown error";
      }
    } catch (e) {
      vModelDownloadError = String(e);
    } finally {
      vModelDownloading = false;
    }
  }

  function openModelPicker() {
    // Default the picker to the first option that's not the currently active
    // model (since "download what I'm already using" is rarely the goal).
    const cur = (vModelSize || "tiny").trim();
    const first = VOICE_MODEL_OPTIONS.find(o => o.id !== cur);
    vModelPickerChoice = (first?.id) || "small";
    vModelDownloadResult = "";
    vModelDownloadError = "";
    vModelPickerOpen = true;
  }

  // --- GitHub Copilot OAuth ---

  async function refreshCopilotStatus() {
    try {
      copStatus = (await invoke("copilot_status")) as CopStatus;
      // Stale-error clear: if the backend now reports a valid login (whether
      // because the just-finished poll succeeded, or because a sidecar restart
      // dropped an in-flight `_pending` device flow that the frontend then
      // wrongly read as "no pending login"), wipe any leftover copError so the
      // UI doesn't keep showing a contradictory red banner next to "✓ 已登录".
      if (copStatus.logged_in) {
        copError = "";
      }
    } catch (e) {
      copError = String(e);
    }
  }

  async function copilotLogin() {
    copError = "";
    copBusy = true;
    copDevice = null;
    if (copPollTimer) { clearInterval(copPollTimer); copPollTimer = null; }
    try {
      copDevice = (await invoke("copilot_login_begin")) as CopDevice;
      if (!copDevice) throw new Error("empty device-code response");
      const intervalMs = Math.max(1000, copDevice.interval * 1000);
      copPollTimer = setInterval(async () => {
        try {
          const r = (await invoke("copilot_login_poll", { deviceCode: copDevice!.device_code })) as { status: string; error?: string };
          if (r.status === "ok") {
            if (copPollTimer) { clearInterval(copPollTimer); copPollTimer = null; }
            copDevice = null;
            copBusy = false;
            await refreshCopilotStatus();
            // 自动切到 copilot provider 并保存
            provider = "copilot";
            await save();
          } else if (r.status === "error") {
            if (copPollTimer) { clearInterval(copPollTimer); copPollTimer = null; }
            // If sidecar was restarted mid-flow (typical trigger: user hit
            // Save in this same Settings page, which hot-reloads sidecar and
            // wipes the in-memory `_pending`), poll will start returning
            // "no pending login; call begin_login first" forever. Before
            // surfacing it, check whether we're actually already logged in —
            // if so, this error is stale; just clear and refresh status.
            const isStalePending = (r.error || "").indexOf("no pending login") >= 0;
            if (isStalePending) {
              await refreshCopilotStatus();
              if (copStatus.logged_in) {
                copDevice = null;
                copBusy = false;
                copError = "";
                return;
              }
            }
            copError = r.error || $_("settings.copilot_login_failed_default");
            copDevice = null;
            copBusy = false;
          }
          // pending / slow_down -> 继续轮询
        } catch (e) {
          if (copPollTimer) { clearInterval(copPollTimer); copPollTimer = null; }
          copError = String(e);
          copDevice = null;
          copBusy = false;
        }
      }, intervalMs);
    } catch (e) {
      copError = String(e);
      copBusy = false;
    }
  }

  function copilotCancel() {
    if (copPollTimer) { clearInterval(copPollTimer); copPollTimer = null; }
    copDevice = null;
    copBusy = false;
  }

  async function copilotLogout() {
    try {
      await invoke("copilot_logout");
      await refreshCopilotStatus();
    } catch (e) {
      copError = String(e);
    }
  }

  function fmtExpiry(ts: number | null | undefined): string {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  }
</script>

<svelte:head>
  <title>{$_("settings.page_title")}</title>
</svelte:head>

<main>
  <header>
    <a class="back" href="/">{$_("common.back")}</a>
    <h1>{$_("settings.heading")}</h1>
  </header>

  <div class="layout">
    <nav class="tabs" aria-label="Settings sections">
      <button class="tab" class:active={activeTab === "general"} onclick={() => (activeTab = "general")}>
        {$_("settings.general_section_title")}
      </button>
      <button class="tab" class:active={activeTab === "llm"} onclick={() => (activeTab = "llm")}>
        {$_("settings.llm_section_title")}
      </button>
      <button class="tab" class:active={activeTab === "voice"} onclick={() => (activeTab = "voice")}>
        {$_("settings.voice_section_title")}
      </button>
      <button class="tab" class:active={activeTab === "about"} onclick={() => (activeTab = "about")}>
        {$_("settings.about_section_title")}
      </button>
    </nav>

    <div class="panel">
      {#if activeTab === "general"}
        <section class="card">
          <h2>{$_("settings.general_section_title")}</h2>
          <p class="path">{$_("settings.config_path_label")} <code>{path || $_("settings.config_path_unloaded")}</code></p>
          <label>
            {$_("lang.picker_label")}
            <select bind:value={uiLocale} onchange={onLocaleChange}>
              {#each SUPPORTED_LOCALES as code (code)}
                <option value={code}>{LOCALE_LABELS[code as SupportedLocale]}</option>
              {/each}
            </select>
          </label>
          <p class="hint">{$_("settings.language_hint")}</p>
          <label>
            {$_("settings.emergency_hotkey_label")}
            <input
              type="text"
              readonly
              bind:value={emergencyHotkey}
              placeholder={capturingHotkeyFor === "emergency" ? $_("settings.hotkey_capture_listening") : "ctrl+alt+esc"}
              onfocus={() => { capturingHotkeyFor = "emergency"; }}
              onblur={() => { if (capturingHotkeyFor === "emergency") capturingHotkeyFor = null; }}
              onkeydown={(e) => onHotkeyKeydown("emergency", e)}
              title={$_("settings.hotkey_capture_hint")}
            />
          </label>
          <p class="hint">{$_("settings.emergency_hotkey_hint")}</p>
          <div class="row">
            <button onclick={save} disabled={saving}>{saving ? $_("settings.saving_button") : $_("settings.save_button")}</button>
            {#if savedAt}<span class="ok">{$_("settings.saved_at", { values: { at: savedAt } })}</span>{/if}
            {#if error}<span class="err">{error}</span>{/if}
          </div>
          <p class="hint">{$_("settings.hot_reload_hint")}</p>
        </section>
      {:else if activeTab === "llm"}
        <section class="card">
          <h2>{$_("settings.llm_section_title")}</h2>
          <label>
            {$_("settings.provider_label")}
            <select bind:value={provider}>
              <option value="anthropic">{$_("settings.provider_anthropic")}</option>
              <option value="copilot">{$_("settings.provider_copilot")}</option>
            </select>
          </label>

          {#if provider === "anthropic"}
            <h3>{$_("settings.anthropic_section_title")}</h3>
            <label>
              api_key
              <input type="password" bind:value={anthApiKey} placeholder={$_("settings.anthropic_api_key_placeholder")} />
            </label>
            <label>
              model
              <input type="text" bind:value={anthModel} placeholder={$_("settings.anthropic_model_placeholder")} />
            </label>
            <label>
              base_url
              <input type="text" bind:value={anthBaseUrl} placeholder={$_("settings.anthropic_base_url_placeholder")} />
            </label>
          {:else if provider === "copilot"}
            <h3>{$_("settings.copilot_section_title")}</h3>
            <label>
              model
              <input type="text" bind:value={copModel} placeholder={$_("settings.copilot_model_placeholder")} />
            </label>
            <div class="copilot-status">
              {#if copStatus.logged_in}
                <p class="ok">{$_("settings.copilot_logged_in_as")} <b>{copStatus.github_user || $_("settings.copilot_user_unknown")}</b></p>
                {#if copStatus.copilot_expires_at}
                  <p class="hint">{$_("settings.copilot_token_expires")} {fmtExpiry(copStatus.copilot_expires_at)}</p>
                {/if}
                <button onclick={copilotLogout}>{$_("settings.copilot_logout_button")}</button>
              {:else if copDevice}
                <p>{$_("settings.copilot_device_step_open")}
                  <a href={copDevice.verification_uri} target="_blank" rel="noreferrer">{copDevice.verification_uri}</a>
                  {$_("settings.copilot_device_step_enter_code")}</p>
                <pre class="usercode">{copDevice.user_code}</pre>
                <p class="hint">{$_("settings.copilot_device_polling_hint", { values: { interval: copDevice.interval, minutes: Math.round(copDevice.expires_in / 60) } })}</p>
                <button onclick={copilotCancel}>{$_("settings.copilot_device_cancel_button")}</button>
              {:else}
                <p>{$_("settings.copilot_not_logged_in")}</p>
                <button onclick={copilotLogin} disabled={copBusy}>{copBusy ? $_("settings.copilot_login_busy") : $_("settings.copilot_login_button")}</button>
              {/if}
              {#if copError}<p class="err">{copError}</p>{/if}
            </div>
          {/if}

          <h3>{$_("settings.sampling_section_title")}</h3>
          <label>
            {$_("settings.temperature_label")}
            <input type="number" min="0" max="2" step="0.05" bind:value={temperature} />
          </label>
          <label>
            {$_("settings.top_p_label")}
            <input type="number" min="0" max="1" step="0.05" bind:value={topP} />
          </label>
          <p class="hint">{$_("settings.sampling_hint")}</p>

          <div class="row">
            <button onclick={save} disabled={saving}>{saving ? $_("settings.saving_button") : $_("settings.save_button")}</button>
            {#if savedAt}<span class="ok">{$_("settings.saved_at", { values: { at: savedAt } })}</span>{/if}
            {#if error}<span class="err">{error}</span>{/if}
          </div>
          <p class="hint">{$_("settings.hot_reload_hint")}</p>
        </section>
      {:else if activeTab === "voice"}
        <section class="card">
          <h2>{$_("settings.voice_section_title")}</h2>
          <label class="check">
            <input type="checkbox" bind:checked={vEnabled} />
            {$_("settings.voice_enabled_label")}
          </label>
          <p class="hint">{$_("settings.voice_enabled_hint")}</p>

          <label>
            {$_("settings.voice_engine_label")}
            <select bind:value={vEngine}>
              <option value="faster-whisper">faster-whisper (CPU/GPU)</option>
            </select>
          </label>
          <label>
            {$_("settings.voice_model_label")}
            <input type="text" bind:value={vModelSize} placeholder="tiny | small | medium | large-v3" oninput={() => { vModelDownloadResult = ""; vModelDownloadError = ""; void refreshModelStatus(); }} />
          </label>
          <div class="row">
            <button type="button" onclick={openModelPicker} disabled={vModelDownloading}>
              {vModelDownloading ? $_("settings.voice_model_downloading") : $_("settings.voice_model_download_button")}
            </button>
            {#if vModelLocation === "bundled"}
              <span class="hint">{$_("settings.voice_model_loc_bundled")}</span>
            {:else if vModelLocation === "user"}
              <span class="ok">{$_("settings.voice_model_loc_user")}</span>
            {:else}
              <span class="hint">{$_("settings.voice_model_loc_missing")}</span>
            {/if}
          </div>
          {#if vModelDownloadResult}
            <p class="ok">{vModelDownloadResult}</p>
          {/if}
          {#if vModelDownloadError}
            <p class="err">{vModelDownloadError}</p>
          {/if}
          <p class="hint">{$_("settings.voice_model_download_hint")}</p>
          <label>
            {$_("settings.voice_language_label")}
            <select bind:value={vLanguage}>
              <option value="auto">{$_("settings.voice_language_opt_auto")}</option>
              <option value="en">{$_("settings.voice_language_opt_en")}</option>
              <option value="zh">{$_("settings.voice_language_opt_zh")}</option>
              <option value="fr">{$_("settings.voice_language_opt_fr")}</option>
            </select>
          </label>
          <p class="hint">{$_("settings.voice_language_hint")}</p>

          <label>
            {$_("settings.voice_hotkey_label")}
            <input
              type="text"
              readonly
              bind:value={vHotkey}
              placeholder={capturingHotkeyFor === "voice" ? $_("settings.hotkey_capture_listening") : "Space | ctrl+shift+v"}
              onfocus={() => { capturingHotkeyFor = "voice"; }}
              onblur={() => { if (capturingHotkeyFor === "voice") capturingHotkeyFor = null; }}
              onkeydown={(e) => onHotkeyKeydown("voice", e)}
              title={$_("settings.hotkey_capture_hint")}
            />
          </label>
          <p class="hint">{$_("settings.hotkey_capture_hint")}</p>
          {#if vHotkey.trim().toLowerCase() === "space" || vHotkey.trim().toLowerCase() === "spacebar"}
            <p class="hint">{$_("settings.voice_hotkey_space_hint")}</p>
          {/if}
          <label>
            {$_("settings.voice_hold_threshold_label")}
            <input type="number" min="0" max="20000" step="100" bind:value={vHoldThresholdMs} />
          </label>
          {#if vHoldThresholdMs < 300 && vHotkey.toLowerCase() === "space"}
            <p class="err">{$_("settings.voice_hold_threshold_warning")}</p>
          {/if}

          <label>
            {$_("settings.voice_stop_mode_label")}
            <select bind:value={vStopMode}>
              <option value="release">{$_("settings.voice_stop_mode_release")}</option>
              <option value="tap_again">{$_("settings.voice_stop_mode_tap_again")}</option>
              <option value="auto_silence">{$_("settings.voice_stop_mode_auto_silence")}</option>
            </select>
          </label>
          <label>
            {$_("settings.voice_start_feedback_label")}
            <select bind:value={vStartFeedback}>
              <option value="beep">{$_("settings.voice_start_feedback_beep")}</option>
              <option value="silent">{$_("settings.voice_start_feedback_silent")}</option>
            </select>
          </label>

          <label>
            {$_("settings.voice_mode_label")}
            <select bind:value={vMode}>
              <option value="auto">{$_("settings.voice_mode_auto")}</option>
              <option value="thread_new">{$_("settings.voice_mode_thread_new")}</option>
              <option value="dictation_append">{$_("settings.voice_mode_dictation_append")}</option>
            </select>
          </label>
          <p class="hint">{$_("settings.voice_mode_hint")}</p>
          <label class="check">
            <input type="checkbox" bind:checked={vAutoSend} />
            {$_("settings.voice_auto_send_label")}
          </label>
          <p class="hint">{$_("settings.voice_auto_send_hint")}</p>

          <label>
            {$_("settings.voice_max_seconds_label")}
            <input type="number" min="3" max="120" step="1" bind:value={vMaxSeconds} />
          </label>
          <label>
            {$_("settings.voice_hf_endpoint_label")}
            <select bind:value={vHfEndpointPreset} onchange={onHfPresetChange}>
              <option value="">{$_("settings.voice_hf_endpoint_preset_auto")}</option>
              <option value="https://hf-mirror.com">{$_("settings.voice_hf_endpoint_preset_hfmirror")}</option>
              <option value="https://huggingface.tuna.tsinghua.edu.cn">{$_("settings.voice_hf_endpoint_preset_tuna")}</option>
              <option value="https://huggingface.co">{$_("settings.voice_hf_endpoint_preset_official")}</option>
              <option value="custom">{$_("settings.voice_hf_endpoint_preset_custom")}</option>
            </select>
          </label>
          {#if vHfEndpointPreset === "custom"}
            <label>
              <input type="text" bind:value={vHfEndpoint} placeholder="https://your-hf-mirror.example.com" />
            </label>
          {/if}
          <p class="hint">{$_("settings.voice_hf_endpoint_hint")}</p>

          <div class="row">
            <button onclick={saveVoice} disabled={voiceSaving}>{voiceSaving ? $_("settings.saving_button") : $_("settings.save_button")}</button>
            {#if voiceSavedAt}<span class="ok">{$_("settings.saved_at", { values: { at: voiceSavedAt } })}</span>{/if}
            {#if voiceError}<span class="err">{voiceError}</span>{/if}
          </div>
        </section>
      {:else if activeTab === "about"}
        <section class="card">
          <h2>{$_("settings.about_section_title")}</h2>
          <p class="hint">{$_("settings.about_intro")}</p>
          <div class="row about-row">
            <a class="btn-link" href="https://github.com/DaoZhang0123/Lucid" target="_blank" rel="noreferrer">
              ⭐ {$_("settings.about_star_button")}
            </a>
            <a class="btn-link btn-link-ghost" href="https://github.com/DaoZhang0123" target="_blank" rel="noreferrer">
              👤 {$_("settings.about_follow_button")}
            </a>
          </div>
          <ul class="contact">
            <li>
              <span class="contact-label">{$_("settings.about_email_label")}:</span>
              <a href="mailto:zhangdao@buaa.edu.cn">zhangdao@buaa.edu.cn</a>
            </li>
            <li>
              <span class="contact-label">{$_("settings.about_x_label")}:</span>
              <a href="https://x.com/zhangdao439566" target="_blank" rel="noreferrer">@zhangdao439566</a>
            </li>
            <li>
              <span class="contact-label">{$_("settings.about_zhihu_label")}:</span>
              <a href="https://www.zhihu.com/people/zhang-dao-68" target="_blank" rel="noreferrer">@zhang-dao-68</a>
            </li>
          </ul>
        </section>
      {/if}
    </div>
  </div>
</main>

{#if vModelPickerOpen}
  <div class="modal-backdrop" onclick={() => (vModelPickerOpen = false)} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="model-picker-title">
      <h3 id="model-picker-title">{$_("settings.voice_model_picker_title")}</h3>
      <p class="hint">{$_("settings.voice_model_picker_subtitle")}</p>
      <div class="model-list">
        {#each VOICE_MODEL_OPTIONS as opt (opt.id)}
          {@const sizeStr = opt.sizeMb >= 1000 ? `${(opt.sizeMb/1000).toFixed(1)} GB` : `${opt.sizeMb} MB`}
          <label class="model-opt" class:active={vModelPickerChoice === opt.id}>
            <input type="radio" name="model-pick" value={opt.id} bind:group={vModelPickerChoice} />
            <div class="model-opt-body">
              <div class="model-opt-head">
                <span class="model-opt-name">{opt.id}</span>
                <span class="model-opt-size">{sizeStr}</span>
              </div>
              <div class="model-opt-meta">
                <span class="badge {opt.accuracy}">{$_("settings.voice_model_accuracy_" + opt.accuracy)}</span>
                <span class="badge {opt.multilingual ? 'multi' : 'mono'}">
                  {opt.multilingual ? $_("settings.voice_model_multilingual") : $_("settings.voice_model_english_only")}
                </span>
              </div>
            </div>
          </label>
        {/each}
      </div>
      <div class="modal-actions">
        <button type="button" class="secondary" onclick={() => (vModelPickerOpen = false)}>
          {$_("settings.cancel_button")}
        </button>
        <button type="button" onclick={() => downloadModel(vModelPickerChoice)}>
          {$_("settings.voice_model_picker_confirm")}
        </button>
      </div>
    </div>
  </div>
{/if}
<style>
  main {
    max-width: 56rem;
    margin: 0;
    padding: 1rem 1.5rem;
    font: 14px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    color: #222;
  }
  header {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    margin-bottom: 0.5rem;
  }
  header a {
    color: #2563eb;
    text-decoration: none;
  }
  .back { font-size: 0.9rem; }
  h1 { margin: 0.5rem 0; }
  h2 { font-size: 15px; margin: 0 0 8px; color: #111; }
  h3 { font-size: 13px; margin: 16px 0 4px; color: #334155; text-transform: uppercase; letter-spacing: 0.04em; }

  /* ---- Two-pane layout: left tab nav + right content panel ---- */
  .layout { display: flex; gap: 1rem; align-items: flex-start; margin-top: 0.75rem; }
  .tabs {
    flex: none;
    width: 10rem;
    display: flex;
    flex-direction: column;
    gap: 2px;
    border: 1px solid #e2e8f0;
    background: #fff;
    border-radius: 8px;
    padding: 6px;
  }
  .tab {
    appearance: none;
    background: transparent;
    color: #334155;
    border: 0;
    text-align: left;
    padding: 8px 10px;
    border-radius: 4px;
    cursor: pointer;
    font: inherit;
  }
  .tab:hover { background: #f1f5f9; }
  .tab.active { background: #2563eb; color: #fff; }
  .panel { flex: 1; min-width: 0; }

  /* About / contact tab */
  .btn-link {
    display: inline-flex; align-items: center; gap: 0.4rem;
    padding: 6px 14px; background: #2563eb; color: #fff;
    border: 0; border-radius: 4px; text-decoration: none; font: inherit;
  }
  .btn-link:hover { background: #1d4ed8; }
  .btn-link-ghost { background: #fff; color: #2563eb; border: 1px solid #93c5fd; }
  .btn-link-ghost:hover { background: #eff6ff; }
  .about-row { margin: 0.6rem 0 0.4rem; }
  .contact { list-style: none; padding: 0; margin: 0.4rem 0 0; }
  .contact li { padding: 0.3rem 0; }
  .contact-label { display: inline-block; width: 5.5rem; color: #475569; font-weight: 600; }
  .contact a { color: #2563eb; text-decoration: none; }
  .contact a:hover { text-decoration: underline; }
  .copilot-status { padding: 10px 12px; background: #f8fafc; border-radius: 6px; margin-top: 8px; }
  .copilot-status p { margin: 4px 0; }
  .usercode { font: bold 22px/1.2 Consolas, monospace; letter-spacing: 4px; background: #fff; color: #0f172a; padding: 12px 16px; border: 1px dashed #94a3b8; border-radius: 6px; text-align: center; user-select: all; }
  .card {
    border: 1px solid #e2e8f0;
    background: #fff;
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 16px;
  }
  .path { font-size: 12px; color: #475569; margin: 0 0 12px; }
  label { display: block; margin: 8px 0; }
  label > input, label > select {
    display: block;
    width: 100%;
    margin-top: 2px;
    padding: 6px 8px;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    font: inherit;
  }
  label.check { display: flex; align-items: center; gap: 8px; }
  label.check > input[type="checkbox"] {
    display: inline-block;
    width: auto;
    margin: 0;
    padding: 0;
    border: 0;
    flex: 0 0 auto;
  }
  .row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  button {
    padding: 6px 14px;
    background: #2563eb;
    color: #fff;
    border: 0;
    border-radius: 4px;
    cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: wait; }
  .ok { color: #16a34a; }
  .err { color: #dc2626; }
  .hint { font-size: 12px; color: #64748b; margin-top: 8px; }
  pre {
    background: #0f172a;
    color: #e2e8f0;
    padding: 12px;
    border-radius: 4px;
    overflow: auto;
    font: 12px/1.4 Consolas, monospace;
    max-height: 360px;
  }
  code {
    background: #f1f5f9;
    padding: 1px 4px;
    border-radius: 3px;
    font-family: Consolas, monospace;
  }

  /* ----- model picker modal ----- */
  .modal-backdrop {
    position: fixed; inset: 0;
    background: rgba(15, 23, 42, 0.55);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
    padding: 24px;
    backdrop-filter: blur(2px);
  }
  .modal {
    background: #fff;
    border-radius: 12px;
    padding: 20px 22px 16px;
    width: 100%;
    max-width: 520px;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0,0,0,0.25);
  }
  .modal h3 { margin: 0 0 6px; font-size: 16px; }
  .modal .hint { margin: 0 0 14px; }
  .model-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
  .model-opt {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 10px 12px;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .model-opt:hover { background: #f8fafc; }
  .model-opt.active { border-color: #2563eb; background: #eff6ff; }
  .model-opt > input[type="radio"] {
    width: auto; margin: 4px 0 0; padding: 0;
    border: 0;
    flex: 0 0 auto;
  }
  .model-opt-body { flex: 1; min-width: 0; }
  .model-opt-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .model-opt-name { font-weight: 600; font-family: Consolas, monospace; }
  .model-opt-size { font-size: 12px; color: #64748b; font-variant-numeric: tabular-nums; }
  .model-opt-meta { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 999px;
    background: #f1f5f9;
    color: #475569;
  }
  .badge.low        { background: #fef3c7; color: #92400e; }
  .badge.medium     { background: #dbeafe; color: #1d4ed8; }
  .badge.high       { background: #dcfce7; color: #166534; }
  .badge.very-high  { background: #ede9fe; color: #6d28d9; }
  .badge.multi      { background: #ecfeff; color: #0e7490; }
  .badge.mono       { background: #f1f5f9; color: #475569; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
  .modal-actions button.secondary {
    background: transparent;
    color: #475569;
    border: 1px solid #cbd5e1;
  }
  .modal-actions button.secondary:hover { background: #f1f5f9; }
</style>
