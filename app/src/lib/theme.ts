/**
 * Light/dark theme bootstrap.
 *
 * Persists choice in localStorage["lucid.theme"] (cold-start with no flash:
 * applied synchronously in setupTheme() before first paint of children).
 * Default = follow OS via `prefers-color-scheme`.
 *
 * Application: sets `data-theme="light"|"dark"` on <html>. The actual color
 * remap lives in +layout.svelte's :global(...) CSS so individual pages don't
 * need to be re-styled.
 */
import { browser } from "$app/environment";
import { writable, type Writable } from "svelte/store";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";

export type Theme = "light" | "dark";
const STORAGE_KEY = "lucid.theme";

// Match the header/sidebar palette in +page.svelte (header bg #1f2937,
// light header bg #f3f4f6) so the OS title bar visually fuses with the menu.
const CAPTION_COLOR: Record<Theme, [number, number, number]> = {
  dark: [0x1f, 0x29, 0x37],
  light: [0xf3, 0xf4, 0xf6],
};

export const theme: Writable<Theme> = writable<Theme>("light");

function readStored(): Theme | null {
  if (!browser) return null;
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function osPrefersDark(): boolean {
  if (!browser) return false;
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

function applyTheme(t: Theme): void {
  if (!browser) return;
  document.documentElement.setAttribute("data-theme", t);
  // Propagate to the Tauri native window so the Windows non-client
  // (title bar / window controls) area follows the chosen theme.
  try {
    void getCurrentWindow().setTheme(t);
  } catch {
    /* not running inside Tauri (browser dev) */
  }
  // Paint the title bar to match the in-app header background so they
  // visually merge into one strip (Win11 22H2+ — older Windows silently
  // ignores the DwmSetWindowAttribute call).
  const [r, g, b] = CAPTION_COLOR[t];
  invoke("set_caption_color", { r, g, b }).catch(() => {
    /* command unavailable (browser dev / older Windows) */
  });
}

export function setupTheme(): void {
  const initial: Theme = readStored() ?? (osPrefersDark() ? "dark" : "light");
  applyTheme(initial);
  theme.set(initial);
  // Subsequent updates (toggle button) propagate to <html> + storage.
  theme.subscribe((t) => {
    applyTheme(t);
    if (!browser) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, t);
    } catch {
      /* ignore */
    }
  });
}

export function toggleTheme(): void {
  theme.update((t) => (t === "dark" ? "light" : "dark"));
}
