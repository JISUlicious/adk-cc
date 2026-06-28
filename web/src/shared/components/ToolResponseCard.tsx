import { useState } from "react"
import { ChevronDown, ChevronRight, CornerDownRight } from "lucide-react"

/**
 * Tool response sibling of `ToolCallCard`. The two are paired by callId
 * but we render them as separate rows in the linear thread; Phase 2's
 * polish pass can collapse adjacent (call, response) pairs into a
 * single card if the UX wins.
 */
export function ToolResponseCard({
  callId,
  name,
  response,
}: {
  callId: string
  name: string
  response: unknown
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] w-full rounded-md border border-border bg-card/30 text-sm">
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
          <CornerDownRight className="h-4 w-4 text-muted-foreground" />
          <span className="font-mono text-xs text-muted-foreground">
            {name} result
          </span>
          {callId && (
            <span className="ml-auto font-mono text-[10px] text-muted-foreground">
              {callId.slice(0, 12)}
            </span>
          )}
        </button>
        {open && (
          <pre className="px-3 pb-3 text-xs overflow-auto max-h-64 text-muted-foreground">
            {safeJson(response)}
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
