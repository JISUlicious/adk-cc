import { useState } from "react"
import {
  ChevronDown,
  ChevronRight,
  Wrench,
  Check,
  X,
  Loader,
} from "lucide-react"

/**
 * Unified renderer for tool calls without a specialized paired card.
 * Merges function_call + function_response (paired by ADK call id)
 * into one collapsible row with a status marker:
 *
 *   called    — waiting for the response (response === null)
 *   finished  — response landed, no error signal in the payload
 *   error     — response shape signals failure (status/error fields)
 *
 * Collapsed shows only the header. Expanded shows the args (always)
 * + either the response or the error block. Used by `Thread.tsx` for
 * any tool name not in `PAIRED_RENDERERS` (the specialized
 * renderers — BashTerminalCard, FileEditCard, PlanCard).
 */
export function ToolCard({
  name,
  callId,
  args,
  response,
}: {
  name: string
  callId: string
  args: unknown
  response: unknown
}) {
  const [open, setOpen] = useState(false)
  const status = deriveStatus(response)
  const error = status === "error" ? extractError(response) : null
  const hasArgs = Boolean(
    args && (typeof args !== "object" || Object.keys(args as object).length > 0),
  )
  const hasResponse = response !== null && response !== undefined

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] w-full rounded-md border border-border bg-card/50 text-sm">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent rounded-md"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <Wrench className="h-4 w-4 text-muted-foreground" />
          <span className="font-mono text-xs truncate flex-1">{name}</span>
          <StatusChip status={status} />
          {callId && (
            <span className="font-mono text-[10px] text-muted-foreground shrink-0">
              {callId.slice(0, 8)}
            </span>
          )}
        </button>
        {open && (
          <div className="px-3 pb-3 space-y-2">
            {hasArgs && (
              <JsonBlock label="args" value={args} />
            )}
            {error && (
              <div className="rounded bg-destructive/10 text-destructive px-2 py-1 text-xs">
                {error}
              </div>
            )}
            {!error && hasResponse && (
              <JsonBlock label="response" value={response} />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

type Status = "called" | "finished" | "error"

function StatusChip({ status }: { status: Status }) {
  if (status === "called") {
    return (
      <span className="flex items-center gap-1 rounded-sm bg-secondary text-secondary-foreground px-1.5 py-0.5 text-[10px] font-medium">
        <Loader className="h-3 w-3 animate-spin" />
        called
      </span>
    )
  }
  if (status === "error") {
    return (
      <span className="flex items-center gap-1 rounded-sm bg-destructive/15 text-destructive px-1.5 py-0.5 text-[10px] font-medium">
        <X className="h-3 w-3" />
        error
      </span>
    )
  }
  return (
    <span className="flex items-center gap-1 rounded-sm bg-brand-tint text-primary px-1.5 py-0.5 text-[10px] font-medium">
      <Check className="h-3 w-3" />
      finished
    </span>
  )
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
        {label}
      </div>
      <pre className="rounded bg-muted p-2 text-xs font-mono overflow-x-auto max-h-64">
        {safeJson(value)}
      </pre>
    </div>
  )
}

/** Heuristic status read.
 *
 * Tools in adk-cc don't share a single response schema — most return
 * `{status: "ok"|"error"|"sandbox_denied"|"not_found"|...}` but
 * conventions drift. We recognize the common shapes:
 *
 *   - `response === null/undefined`            → called (still pending)
 *   - `response.error` is a non-empty string   → error
 *   - `response.status` matches an error word  → error
 *   - everything else                          → finished
 *
 * Liberal default: a tool that returns `null` or anything we don't
 * recognize as an error is shown as finished, not error — the
 * payload is still visible if the user expands the row.
 */
function deriveStatus(response: unknown): Status {
  if (response === null || response === undefined) return "called"
  if (typeof response === "object") {
    const r = response as Record<string, unknown>
    if (typeof r.error === "string" && r.error.length > 0) return "error"
    if (typeof r.status === "string") {
      const s = r.status.toLowerCase()
      if (
        s === "error" ||
        s === "failure" ||
        s === "denied" ||
        s === "timeout" ||
        s === "not_found" ||
        s.startsWith("sandbox_denied")
      ) {
        return "error"
      }
    }
  }
  return "finished"
}

function extractError(response: unknown): string | null {
  if (!response || typeof response !== "object") return null
  const r = response as Record<string, unknown>
  if (typeof r.error === "string" && r.error.length > 0) return r.error
  if (typeof r.status === "string") return r.status
  return null
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
