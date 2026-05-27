import { useState } from "react"
import { ChevronDown, ChevronRight, Wrench } from "lucide-react"

/**
 * Renders one agent-issued tool call. Collapsed by default — argument
 * blobs can be enormous (think 200-line bash scripts or big JSON). Click
 * the row to expand the args; the response lands in a separate
 * `ToolResponseCard` row directly below.
 *
 * Phase 2 will swap this for typed renderers per tool name (Bash
 * terminal, file diff, etc.). For Phase 1 we keep a single JSON
 * fallback so every tool call is at least visible.
 */
export function ToolCallCard({
  callId,
  name,
  args,
}: {
  callId: string
  name: string
  args: unknown
}) {
  const [open, setOpen] = useState(false)
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
          <span className="font-mono text-xs">{name}</span>
          {callId && (
            <span className="ml-auto font-mono text-[10px] text-muted-foreground">
              {callId.slice(0, 12)}
            </span>
          )}
        </button>
        {open && (
          <pre className="px-3 pb-3 text-xs overflow-x-auto text-muted-foreground">
            {safeJson(args)}
          </pre>
        )}
      </div>
    </div>
  )
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
