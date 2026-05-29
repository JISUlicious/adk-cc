import { useState, type ReactNode } from "react"
import { ChevronDown, ChevronRight, Layers } from "lucide-react"

/**
 * Accumulates a run of consecutive tool-call rows into a single
 * collapsible annotation. `Thread.tsx` wraps any run of MORE THAN TWO
 * adjacent tool rows (tool_pair cards + orphan function_responses, but
 * never pending interactive widgets) in this component so a long chain
 * of reads/greps/edits collapses to one line — "N tool calls" — instead
 * of flooding the transcript. Runs of one or two render inline as before.
 *
 * Collapsed: the annotation row only (count + a compact name summary).
 * Expanded: the same header plus every wrapped card stacked under a
 * left rail. Purely presentational — the children are the already-built
 * <Row> cards, so each still renders exactly as it would standalone.
 *
 * `defaultOpen` starts the group expanded; Thread passes it for a run
 * that contains a still-running call during streaming, so live tool
 * progress stays visible. Completed historical runs stay collapsed.
 */
export function ToolCallGroup({
  count,
  summary,
  defaultOpen = false,
  children,
}: {
  count: number
  summary?: string
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] w-full">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 rounded-md border border-border bg-card/50 px-3 py-2 text-left text-sm hover:bg-accent"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
          )}
          <Layers className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="font-medium shrink-0">{count} tool calls</span>
          {summary && (
            <span className="font-mono text-xs text-muted-foreground truncate">
              {summary}
            </span>
          )}
        </button>
        {open && (
          <div className="mt-2 ml-2 flex flex-col gap-2 border-l border-border pl-3">
            {children}
          </div>
        )}
      </div>
    </div>
  )
}
