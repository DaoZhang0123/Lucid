/**
 * i18n bootstrap for the Tauri Svelte UI.
 *
 * Usage in components:
 *   import { _ } from "svelte-i18n";
 *   <h1>{$_("app.title")}</h1>
 *
 * Adding a new language:
 *   1. Drop `messages/<locale>.json` next to `en.json` (same key shape).
 *   2. Add the locale code to SUPPORTED_LOCALES and LOCALE_LABELS below.
 *   3. Register it via `register(...)`.
 *
 * Locale persistence: chosen language is stored in `localStorage` under
 * `LOCALE_STORAGE_KEY`. Avoids a sidecar round-trip on cold start (no flash of
 * English -> chosen locale). If we later want multi-device sync, mirror this
 * into `config.toml [ui].locale` and prefer the file when present.
 *
 * Keys and English copy are the source of truth; missing keys in other locales
 * fall back to English at runtime.
 */
import { browser } from "$app/environment";
import { init, register, locale, getLocaleFromNavigator } from "svelte-i18n";

export const SUPPORTED_LOCALES = ["en", "zh-CN", "fr-FR"] as const;
export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

export const FALLBACK_LOCALE: SupportedLocale = "en";
export const LOCALE_STORAGE_KEY = "lucid.locale";

/** Human-readable names shown in the language picker. */
export const LOCALE_LABELS: Record<SupportedLocale, string> = {
  "en": "English",
  "zh-CN": "中文 (简体)",
  "fr-FR": "Français",
};

// Lazy-register every supported locale. Vite code-splits each JSON file.
register("en", () => import("./messages/en.json"));
register("zh-CN", () => import("./messages/zh-CN.json"));
register("fr-FR", () => import("./messages/fr-FR.json"));

let initialized = false;

function readStoredLocale(): string | null {
  if (!browser) return null;
  try {
    return window.localStorage.getItem(LOCALE_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Persist the user-selected locale; fail-soft if storage is unavailable.
 *
 * Also pushes the new value into config.toml `[ui].locale` via the sidecar's
 * `write_settings` command so the Python loop can use it inside the system
 * prompt (so the LLM defaults to replying in the user's chosen UI language).
 * Both writes are best-effort; failures are silent (e.g. private browsing
 * disables localStorage; sidecar might still be spawning on first boot —
 * the next change will succeed).
 */
export function saveLocale(value: string): void {
  if (!browser) return;
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, value);
  } catch {
    /* private mode / disabled storage — silently ignore */
  }
  // Mirror to config.toml [ui].locale (async, fire-and-forget).
  // Dynamic import keeps the i18n module independent of Tauri at SSR time.
  void (async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("write_settings", { patch: { ui: { locale: value } } });
      // Hot-reload the sidecar's in-memory cfg so backend-side locale
      // consumers (auto-reply default text, system-prompt identity block,
      // …) pick up the new language immediately. Without this, the sidecar
      // keeps using whatever locale was active when it started.
      try { await invoke("reload_config"); } catch { /* ignore — task running, applies on next launch */ }
    } catch {
      /* sidecar not up yet, or non-Tauri context — ignore */
    }
  })();
}

/** Initialise svelte-i18n once. Safe to call multiple times.
 *
 * Resolution order for the starting locale:
 *   1. Explicit `initialLocale` argument (rare — used by tests).
 *   2. `localStorage[LOCALE_STORAGE_KEY]` — the user's last explicit choice
 *      in the language picker. This wins so returning users never see a
 *      flash of a different language.
 *   3. `config.toml [ui].locale` — read asynchronously after sync init so
 *      that a pre-seeded install (e.g. an admin/distro that ships
 *      `locale = "zh-CN"` in the bundled config) starts in that language
 *      on the very first launch. We can't await this synchronously without
 *      delaying the splash, so we kick it off in the background and call
 *      `locale.set()` once it resolves (typically <50 ms, before the first
 *      message renders). The async swap is a no-op if the config locale
 *      equals what we already picked.
 *   4. Browser/OS locale from `navigator`.
 *   5. `FALLBACK_LOCALE` ("en").
 */
export function setupI18n(initialLocale?: string): void {
  if (initialized) {
    if (initialLocale) {
      void locale.set(initialLocale);
      saveLocale(initialLocale);
    }
    return;
  }
  initialized = true;
  const stored = readStoredLocale();
  const navLocale = browser ? getLocaleFromNavigator() : null;
  const startLocale = initialLocale || stored || navLocale || FALLBACK_LOCALE;
  init({
    fallbackLocale: FALLBACK_LOCALE,
    initialLocale: startLocale,
  });
  // If we picked the locale ourselves (no stored value, no explicit arg),
  // mirror it into config.toml [ui].locale right away so the Python loop's
  // system prompt advertises the right reply language. Without this the
  // backend sees cfg.ui.locale = "auto" and would fall back to English even
  // though the user is reading the app in Chinese (or French).
  if (!initialLocale && !stored && startLocale) {
    saveLocale(startLocale);
  }
  // Async second pass: if the user hasn't made an explicit choice yet
  // (no localStorage), consult config.toml [ui].locale. A pre-seeded
  // config wins over the navigator guess so distribution channels can
  // pin the default language.
  if (!initialLocale && !stored && browser) {
    void (async () => {
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        const settings = (await invoke("read_settings")) as {
          ui?: { locale?: string };
        };
        const cfg = (settings?.ui?.locale || "").trim();
        // Treat "" and "auto" as "no preference" — keep what we already have.
        if (!cfg || cfg.toLowerCase() === "auto") return;
        // Only switch if the config locale is one we actually ship.
        if (!(SUPPORTED_LOCALES as readonly string[]).includes(cfg)) return;
        if (cfg === startLocale) return;
        await locale.set(cfg);
        try {
          window.localStorage.setItem(LOCALE_STORAGE_KEY, cfg);
        } catch {
          /* ignore */
        }
      } catch {
        /* sidecar not up, or non-Tauri context — keep navigator pick */
      }
    })();
  }
}

export { locale };
