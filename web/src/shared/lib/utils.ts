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

/**
 * Width of one thread row's content.
 *
 * Every card in the thread used to set its own: seven were
 * `max-w-[80%] w-full`, three (confirmation / plan / exit-plan) were plain
 * `w-full` so they ran to 100%, the aggregated-outputs card had no wrapper at
 * all, and a model message had `max-w-[80%]` WITHOUT `w-full` so it hugged its
 * text and ended at a different place on every message. The result was a
 * ragged right edge down the conversation.
 *
 * One token so they line up. `w-full` is the part that matters — without it a
 * max-width only caps, and short content ends early.
 */
export const THREAD_ROW_WIDTH = "w-full max-w-[80%]"
