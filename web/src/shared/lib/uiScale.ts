/**
 * UI scale — how large everything renders.
 *
 * Implemented as the root font size rather than a zoom or a per-component
 * size prop: Tailwind's spacing, text and radius scales are all rem-based, so
 * moving `html { font-size }` scales type, padding, gaps and icons together and
 * keeps the layout proportional. A `transform: scale()` would blur text and
 * break fixed positioning; overriding individual text classes would leave the
 * spacing behind and make dense views collide.
 *
 * Stored in localStorage under `adk_cc.ui_scale` — a device preference, not an
 * account one: the same user on a laptop and a 4K monitor wants different
 * answers, and syncing it to the server would fight that.
 *
 * Follows theme.ts deliberately (get / set / apply / init / hook) so there is
 * one shape for "local UI preference" rather than two.
 */

import { useEffect, useState } from "react"

export type UiScale = "compact" | "default" | "comfortable" | "large"

const KEY = "adk_cc.ui_scale"

/** Root font size per step. 16px is the browser default and stays "default"
 *  so an untouched install renders exactly as it always has. */
const PX: Record<UiScale, number> = {
  compact: 14,
  default: 16,
  comfortable: 18,
  large: 20,
}

export const UI_SCALES: { value: UiScale; label: string; hint: string }[] = [
  { value: "compact", label: "Compact", hint: "14px" },
  { value: "default", label: "Default", hint: "16px" },
  { value: "comfortable", label: "Comfortable", hint: "18px" },
  { value: "large", label: "Large", hint: "20px" },
]

export function getStoredScale(): UiScale {
  const raw = localStorage.getItem(KEY)
  return raw === "compact" || raw === "default" || raw === "comfortable" || raw === "large"
    ? raw
    : "default"
}

export function setStoredScale(scale: UiScale): void {
  localStorage.setItem(KEY, scale)
  applyScale(scale)
}

/** Apply immediately. Clearing the property (rather than writing 16px) at the
 *  default step means a stylesheet or a user's own browser font setting still
 *  wins when they have not chosen a scale here. */
export function applyScale(scale: UiScale): void {
  const root = document.documentElement
  if (scale === "default") root.style.removeProperty("font-size")
  else root.style.fontSize = `${PX[scale]}px`
}

/** Call once at boot, before first paint, so the app does not render at one
 *  size and jump to another. */
export function initUiScale(): void {
  applyScale(getStoredScale())
}

/** React hook for the control. */
export function useUiScale(): [UiScale, (s: UiScale) => void] {
  const [scale, setScale] = useState<UiScale>(() => getStoredScale())
  useEffect(() => {
    setStoredScale(scale)
  }, [scale])
  return [scale, setScale]
}
