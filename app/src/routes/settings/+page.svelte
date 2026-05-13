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

  // ---- voice ---------------------------------------------------------
  let vEnabled = $state(false);
  let vEngine = $state("faster-whisper");
  let vModelSize = $state("distil-small.en");
  let vLanguage = $state("");
  let vHotkey = $state("Space");
  let vHoldThresholdMs = $state(5000);
  let vStopMode = $state("tap_again");
  let vStartFeedback = $state("beep");
  let vMode = $state("agent");
  let vAlwaysNewThread = $state(false);
  let vMaxSeconds = $state(30);
  let vOverlayScreen = $state("cursor");
  let vHfEndpoint = $state("");
  let voiceSaving = $state(false);
  let voiceSavedAt = $state("");
  let voiceError = $state("");
  let saving = $state(false);
  let savedAt = $state("");
  let error = $state("");

  let pingMs = $state<number | null>(null);
  let pingErr = $state("");

  let selfcheckOut = $state<unknown>(null);
  let selfcheckRunning = $state(false);

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
          always_new_thread: boolean | null;
          max_seconds: number | null;
          overlay_screen: string;
          hf_endpoint: string;
        };
      };
      path = cfg.path;
      if (cfg.provider === "anthropic" || cfg.provider === "copilot" || cfg.provider === "proxy") {
        provider = cfg.provider;
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
        if (typeof v.language === "string") vLanguage = v.language;
        if (v.hotkey) vHotkey = v.hotkey;
        if (typeof v.hold_threshold_ms === "number") vHoldThresholdMs = v.hold_threshold_ms;
        if (v.stop_mode) vStopMode = v.stop_mode;
        if (v.start_feedback) vStartFeedback = v.start_feedback;
        if (v.mode) vMode = v.mode;
        if (typeof v.always_new_thread === "boolean") vAlwaysNewThread = v.always_new_thread;
        if (typeof v.max_seconds === "number") vMaxSeconds = v.max_seconds;
        if (v.overlay_screen) vOverlayScreen = v.overlay_screen;
        if (typeof v.hf_endpoint === "string") vHfEndpoint = v.hf_endpoint;
      }
    } catch (e) {
      error = String(e);
    }
    await refreshCopilotStatus();
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

  async function ping() {
    pingErr = "";
    pingMs = null;
    const t0 = performance.now();
    try {
      await invoke("sidecar_ping");
      pingMs = Math.round(performance.now() - t0);
    } catch (e) {
      pingErr = String(e);
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
            always_new_thread: vAlwaysNewThread,
            max_seconds: vMaxSeconds,
            overlay_screen: vOverlayScreen,
            hf_endpoint: vHfEndpoint,
          },
        },
      });
      try { await invoke("reload_config"); } catch { /* ignore — applies on next launch */ }
      // Re-register hotkey + push fresh cfg into the long-press controller.
      try { await reloadVoiceConfig(); } catch (e) { console.warn("reloadVoiceConfig failed:", e); }
      voiceSavedAt = new Date().toLocaleTimeString();
    } catch (e) {
      voiceError = String(e);
    } finally {
      voiceSaving = false;
    }
  }

  async function runSelfcheck(what: "monitors" | "winr" | "click" | "all") {
    selfcheckRunning = true;
    selfcheckOut = null;
    try {
      selfcheckOut = await invoke("run_selfcheck", { what });
    } catch (e) {
      selfcheckOut = { error: String(e) };
    } finally {
      selfcheckRunning = false;
    }
  }

  // --- GitHub Copilot OAuth ---

  async function refreshCopilotStatus() {
    try {
      copStatus = (await invoke("copilot_status")) as CopStatus;
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

  <section class="card">
    <h2>{$_("settings.llm_section_title")}</h2>
    <p class="path">{$_("settings.config_path_label")} <code>{path || $_("settings.config_path_unloaded")}</code></p>
    <label>
      {$_("settings.provider_label")}
      <select bind:value={provider}>
        <option value="anthropic">{$_("settings.provider_anthropic")}</option>
        <option value="copilot">{$_("settings.provider_copilot")}</option>
        <option value="proxy">{$_("settings.provider_proxy")}</option>
      </select>
    </label>

    {#if provider === "proxy"}
      <h3>{$_("settings.proxy_section_title")}</h3>
      <label>
        base_url
        <input type="text" bind:value={baseUrl} placeholder={$_("settings.proxy_base_url_placeholder")} />
      </label>
      <label>
        model
        <input type="text" bind:value={model} placeholder={$_("settings.proxy_model_placeholder")} />
      </label>
      <label>
        api_key
        <input type="password" bind:value={apiKey} placeholder={$_("settings.proxy_api_key_placeholder")} />
      </label>
    {:else if provider === "anthropic"}
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

    <h3>{$_("settings.general_section_title")}</h3>
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
      {$_("settings.temperature_label")}
      <input type="number" min="0" max="2" step="0.05" bind:value={temperature} />
    </label>
    <label>
      {$_("settings.top_p_label")}
      <input type="number" min="0" max="1" step="0.05" bind:value={topP} />
    </label>
    <p class="hint">{$_("settings.sampling_hint")}</p>
    <label>
      {$_("settings.emergency_hotkey_label")}
      <input type="text" bind:value={emergencyHotkey} placeholder="ctrl+alt+esc" />
    </label>
    <p class="hint">{$_("settings.emergency_hotkey_hint")}</p>
    <div class="row">
      <button onclick={save} disabled={saving}>{saving ? $_("settings.saving_button") : $_("settings.save_button")}</button>
      {#if savedAt}<span class="ok">{$_("settings.saved_at", { values: { at: savedAt } })}</span>{/if}
      {#if error}<span class="err">{error}</span>{/if}
    </div>
    <p class="hint">{$_("settings.hot_reload_hint")}</p>
  </section>

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
        <option value="sherpa-onnx">sherpa-onnx (experimental)</option>
        <option value="vosk">vosk (experimental)</option>
      </select>
    </label>
    <label>
      {$_("settings.voice_model_label")}
      <input type="text" bind:value={vModelSize} placeholder="distil-small.en | small | medium | large-v3" />
    </label>
    <label>
      {$_("settings.voice_language_label")}
      <input type="text" bind:value={vLanguage} placeholder={$_("settings.voice_language_placeholder")} />
    </label>

    <label>
      {$_("settings.voice_hotkey_label")}
      <input type="text" bind:value={vHotkey} placeholder="Space | ctrl+shift+v" />
    </label>
    <label>
      {$_("settings.voice_hold_threshold_label")}
      <input type="number" min="0" max="20000" step="100" bind:value={vHoldThresholdMs} />
    </label>
    {#if vHoldThresholdMs < 1000 && vHotkey.toLowerCase() === "space"}
      <p class="err">{$_("settings.voice_hold_threshold_warning")}</p>
    {/if}

    <label>
      {$_("settings.voice_stop_mode_label")}
      <select bind:value={vStopMode}>
        <option value="release">release</option>
        <option value="tap_again">tap_again</option>
        <option value="auto_silence">auto_silence</option>
      </select>
    </label>
    <label>
      {$_("settings.voice_start_feedback_label")}
      <select bind:value={vStartFeedback}>
        <option value="beep">beep</option>
        <option value="vibrate-tray">vibrate-tray</option>
        <option value="silent">silent</option>
      </select>
    </label>

    <label>
      {$_("settings.voice_mode_label")}
      <select bind:value={vMode}>
        <option value="agent">{$_("settings.voice_mode_agent")}</option>
        <option value="dictation">{$_("settings.voice_mode_dictation")}</option>
      </select>
    </label>
    <label class="check">
      <input type="checkbox" bind:checked={vAlwaysNewThread} />
      {$_("settings.voice_always_new_thread_label")}
    </label>

    <label>
      {$_("settings.voice_max_seconds_label")}
      <input type="number" min="3" max="120" step="1" bind:value={vMaxSeconds} />
    </label>
    <label>
      {$_("settings.voice_overlay_screen_label")}
      <select bind:value={vOverlayScreen}>
        <option value="cursor">cursor</option>
        <option value="primary">primary</option>
      </select>
    </label>
    <label>
      {$_("settings.voice_hf_endpoint_label")}
      <input type="text" bind:value={vHfEndpoint} placeholder="https://hf-mirror.com" />
    </label>

    <div class="row">
      <button onclick={saveVoice} disabled={voiceSaving}>{voiceSaving ? $_("settings.saving_button") : $_("settings.save_button")}</button>
      {#if voiceSavedAt}<span class="ok">{$_("settings.saved_at", { values: { at: voiceSavedAt } })}</span>{/if}
      {#if voiceError}<span class="err">{voiceError}</span>{/if}
    </div>
  </section>

  <section class="card">
    <h2>{$_("settings.ping_section_title")}</h2>
    <button onclick={ping}>{$_("settings.ping_button")}</button>
    {#if pingMs !== null}<span class="ok">{pingMs}ms</span>{/if}
    {#if pingErr}<span class="err">{pingErr}</span>{/if}
  </section>

  <section class="card">
    <h2>{$_("settings.selfcheck_section_title")}</h2>
    <div class="row">
      <button onclick={() => runSelfcheck("monitors")} disabled={selfcheckRunning}>{$_("settings.selfcheck_monitors_button")}</button>
      <button onclick={() => runSelfcheck("winr")} disabled={selfcheckRunning}>{$_("settings.selfcheck_winr_button")}</button>
      <button onclick={() => runSelfcheck("click")} disabled={selfcheckRunning}>{$_("settings.selfcheck_click_button")}</button>
      <button onclick={() => runSelfcheck("all")} disabled={selfcheckRunning}>{$_("settings.selfcheck_all_button")}</button>
    </div>
    {#if selfcheckRunning}
      <p>{$_("settings.selfcheck_running_hint")}</p>
    {/if}
    {#if selfcheckOut}
      <pre>{JSON.stringify(selfcheckOut, null, 2)}</pre>
    {/if}
  </section>
</main>

<style>
  main {
    max-width: 760px;
    margin: 0 auto;
    padding: 16px 24px 64px;
    font: 14px/1.5 -apple-system, "Segoe UI", sans-serif;
    color: #222;
  }
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 12px;
  }
  header a {
    color: #2563eb;
    text-decoration: none;
  }
  .back { font-size: 0.9rem; }
  h1 { margin-left: auto; font-size: 18px; }
  h2 { font-size: 15px; margin: 0 0 8px; color: #111; }
  h3 { font-size: 13px; margin: 16px 0 4px; color: #334155; text-transform: uppercase; letter-spacing: 0.04em; }
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
</style>
