/**
 * Theme management. Three modes:
 *   - "light"  → force light
 *   - "dark"   → force dark
 *   - "system" → follow prefers-color-scheme (default)
 *
 * We toggle the `dark` class on <html> rather than swapping a CSS file
 * because Tailwind v4 reads dark variants off `.dark` on any ancestor.
 *
 * Persisted in localStorage under `adk_cc.theme`. Anything else is
 * ignored so we don't break on legacy/malformed values.
 */

import { useEffect, useState } from "react"

export type ThemeMode = "light" | "dark" | "system"
const KEY = "adk_cc.theme"

export function getStoredTheme(): ThemeMode {
  const raw = localStorage.getItem(KEY)
  if (raw === "light" || raw === "dark" || raw === "system") return raw
  return "system"
}

export function setStoredTheme(mode: ThemeMode): void {
  localStorage.setItem(KEY, mode)
  applyTheme(mode)
}

/** Resolve the effective dark/light scheme for a mode. */
function isDark(mode: ThemeMode): boolean {
  if (mode === "dark") return true
  if (mode === "light") return false
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false
}

/** Apply the mode to the document immediately. Safe to call at any
 * time — picks the right class state for the current window media. */
export function applyTheme(mode: ThemeMode): void {
  const root = document.documentElement
  if (isDark(mode)) root.classList.add("dark")
  else root.classList.remove("dark")
}

/** Call once at boot so the initial paint respects the persisted mode
 * (and so we don't get a light flash on dark-preferring browsers).
 * Also wires up a listener for OS-level scheme changes when in
 * "system" mode, so the theme follows the user's settings live. */
export function initTheme(): () => void {
  const mode = getStoredTheme()
  applyTheme(mode)
  const mql = window.matchMedia?.("(prefers-color-scheme: dark)")
  if (!mql) return () => {}
  const onChange = () => {
    if (getStoredTheme() === "system") applyTheme("system")
  }
  mql.addEventListener("change", onChange)
  return () => mql.removeEventListener("change", onChange)
}

/** React hook around the theme mode for UI controls. */
export function useTheme(): [ThemeMode, (m: ThemeMode) => void] {
  const [mode, setMode] = useState<ThemeMode>(() => getStoredTheme())
  useEffect(() => {
    setStoredTheme(mode)
  }, [mode])
  return [mode, setMode]
}
