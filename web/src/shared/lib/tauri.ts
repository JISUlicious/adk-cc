/**
 * Tauri IPC helpers for the desktop app. `tauriInvoke` returns the global
 * `invoke` when the webview has IPC (desktop app with the capability granted),
 * else null — callers fall back to a typed-path input, since `window.prompt` is a
 * no-op in the macOS WKWebView Tauri uses.
 */
export function tauriInvoke(): ((cmd: string, args?: unknown) => Promise<unknown>) | null {
  const t = (window as unknown as {
    __TAURI__?: { core?: { invoke: (c: string, a?: unknown) => Promise<unknown> } }
  }).__TAURI__
  return t?.core?.invoke ?? null
}

/** Open the native folder picker. Distinguishes the two "no path" cases so
 * callers can react correctly:
 *   - a string  → the chosen absolute path
 *   - `null`    → the user CANCELLED the native dialog (do nothing)
 *   - `undefined` → no native IPC (offer a typed-path input instead) */
export async function pickDirectory(): Promise<string | null | undefined> {
  const invoke = tauriInvoke()
  if (!invoke) return undefined
  const picked = await invoke("plugin:dialog|open", {
    options: { directory: true, multiple: false },
  })
  return typeof picked === "string" ? picked : null
}
