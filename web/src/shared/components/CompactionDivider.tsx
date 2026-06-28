import { useState } from "react"
import { ChevronDown, ChevronRight, FoldVertical } from "lucide-react"

/**
 * Marks where ADK's session-context compaction summarized older events.
 *
 * When the conversation grows past `ADK_CC_COMPACTION_TOKEN_THRESHOLD`, ADK runs
 * its event summarizer (post-turn) and records the result as an Event carrying
 * `actions.compaction = {startTimestamp, endTimestamp, compactedContent}`. That
 * event reaches the client over SSE; Thread.tsx turns it into a `compaction`
 * row and renders this divider in place — so the otherwise-silent compaction is
 * visible. Collapsed: a one-line marker. Expanded: the summary text ADK kept in
 * place of the older messages.
 */
export function CompactionDivider({
  summary,
  startTs,
  endTs,
}: {
  summary: string
  startTs?: number
  endTs?: number
}) {
  const [open, setOpen] = useState(false)
  const span = formatSpan(startTs, endTs)

  return (
    <div className="flex justify-center py-1">
      <div className="w-full max-w-[80%]">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
          title="Older messages were summarized to fit the context window"
        >
          <span className="h-px flex-1 bg-border" />
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          )}
          <FoldVertical className="h-3.5 w-3.5 shrink-0" />
          <span className="shrink-0 font-medium">Context compacted</span>
          {span && <span className="shrink-0 opacity-70">· {span}</span>}
          <span className="h-px flex-1 bg-border" />
        </button>
        {open && (
          <div className="mt-2 rounded-md border border-border bg-card/50 px-3 py-2 text-xs text-muted-foreground">
            <p className="mb-1 font-medium text-foreground/80">
              Summary kept in place of the older messages
            </p>
            {summary ? (
              <pre className="whitespace-pre-wrap break-words font-sans">
                {summary}
              </pre>
            ) : (
              <p className="italic opacity-70">(no summary text)</p>
            )}
            <p className="mt-2 border-t border-border pt-2 text-[11px] opacity-70">
              This summary stands in for the older messages in the model's
              working context.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

/** Render the compacted time span compactly, when timestamps are present.
 * ADK timestamps are epoch SECONDS (floats). */
function formatSpan(startTs?: number, endTs?: number): string | null {
  if (typeof endTs !== "number") return null
  try {
    const end = new Date(endTs * 1000)
    const t = end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    if (typeof startTs === "number" && endTs > startTs) {
      const secs = Math.round(endTs - startTs)
      return `${secs}s of history → ${t}`
    }
    return `up to ${t}`
  } catch {
    return null
  }
}
