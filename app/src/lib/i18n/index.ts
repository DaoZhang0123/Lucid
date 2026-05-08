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
export const LOCALE_STORAGE_KEY = "klawbot.locale";

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

/** Persist the user-selected locale; fail-soft if storage is unavailable. */
export function saveLocale(value: string): void {
  if (!browser) return;
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, value);
  } catch {
    /* private mode / disabled storage — silently ignore */
  }
}

/** Initialise svelte-i18n once. Safe to call multiple times. */
export function setupI18n(initialLocale?: string): void {
  if (initialized) {
    if (initialLocale) {
      void locale.set(initialLocale);
      saveLocale(initialLocale);
    }
    return;
  }
  initialized = true;
  const startLocale =
    initialLocale ||
    readStoredLocale() ||
    (browser ? getLocaleFromNavigator() : null) ||
    FALLBACK_LOCALE;
  init({
    fallbackLocale: FALLBACK_LOCALE,
    initialLocale: startLocale,
  });
}

export { locale };
