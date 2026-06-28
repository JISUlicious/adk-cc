import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/** Tailwind-aware class concatenator. shadcn/ui's canonical cn helper. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * Model-written display label for a tool call. `ToolTitlePlugin` injects an
 * optional `title` arg into every tool declaration and the recorded
 * functionCall args keep it; cards show it in their headers. Returns
 * undefined when absent/blank so cards fall back to their existing headers.
 * (Task tools have a NATIVE `title` arg with the same display-friendly
 * semantics — showing it is equally correct there.)
 */
export function toolCallTitle(args: unknown): string | undefined {
  if (!args || typeof args !== "object") return undefined
  const t = (args as Record<string, unknown>)["title"]
  return typeof t === "string" && t.trim() ? t.trim() : undefined
}
