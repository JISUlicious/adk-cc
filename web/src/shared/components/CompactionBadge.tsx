import { FoldVertical } from "lucide-react"

/**
 * At-a-glance compaction history for the current session (compaction-indicator
 * P3). Shows how many times the context was compacted this session — derived
 * live from the event stream (each compaction is an event with
 * actions.compaction), so it updates the moment one lands, with no backend.
 *
 * Why not a global "compacting now" spinner / status endpoint: compaction is a
 * single fast post-turn call (rarely observable live), and the audit events
 * carry no session_id (a global endpoint would show OTHER sessions' compactions
 * in multi-user). The per-session count + the inline CompactionDivider markers
 * are the accurate, useful signals; operator-level observability stays in the
 * audit log.
 *
 * Renders nothing until the first compaction.
 */
export function CompactionBadge({
  count,
  lastEndTs,
}: {
  count: number
  lastEndTs?: number
}) {
  if (count <= 0) return null
  const when =
    typeof lastEndTs === "number"
      ? new Date(lastEndTs * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        })
      : null
  return (
    <span
      className="hidden sm:inline-flex items-center gap-1 rounded-full border border-border bg-card/50 px-2 py-0.5 text-[11px] text-muted-foreground"
      title={`Context compacted ${count}× this session${when ? ` · last ${when}` : ""}`}
    >
      <FoldVertical className="h-3 w-3" />
      {count}×
    </span>
  )
}
