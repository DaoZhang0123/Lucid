/**
 * Disable SSR for the voice overlay — it's a Tauri WebviewWindow that wants
 * to call `invoke()` and `listen()` immediately on mount; static prerendering
 * would error out trying to import @tauri-apps/api at build time.
 */
export const prerender = false;
export const ssr = false;
