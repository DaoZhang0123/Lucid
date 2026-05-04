<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onDestroy, onMount } from "svelte";
  import { _, locale } from "svelte-i18n";
  import { SUPPORTED_LOCALES, LOCALE_LABELS, saveLocale, type SupportedLocale } from "$lib/i18n";

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

  let autonomy = $state<"full" | "confirm_critical" | "confirm_each">("confirm_critical");
  let maxSteps = $state(25);
  let temperature = $state<number>(0.2);
  let topP = $state<number>(1.0);
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
        autonomy: string;
        max_steps: number;
        temperature: number | null;
        top_p: number | null;
        proxy: { base_url: string; model: string; api_key: string };
        anthropic: { api_key: string; model: string; base_url: string };
        copilot: { model: string };
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
      if (cfg.autonomy === "full" || cfg.autonomy === "confirm_critical" || cfg.autonomy === "confirm_each") {
        autonomy = cfg.autonomy;
      }
      if (cfg.max_steps && cfg.max_steps > 0) maxSteps = cfg.max_steps;
      if (typeof cfg.temperature === "number") temperature = cfg.temperature;
      if (typeof cfg.top_p === "number") topP = cfg.top_p;
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
          autonomy,
          max_steps: maxSteps,
          temperature,
          top_p: topP,
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
    <a href="/">{$_("settings.back_to_chat")}</a>
    <a href="/history">{$_("settings.history_link")}</a>
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
      {$_("settings.default_autonomy_label")}
      <select bind:value={autonomy}>
        <option value="full">full</option>
        <option value="confirm_critical">confirm_critical</option>
        <option value="confirm_each">confirm_each</option>
      </select>
    </label>
    <label>
      {$_("settings.max_steps_label")}
      <input type="number" min="1" max="200" bind:value={maxSteps} />
    </label>
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
