import { Trash2 } from "lucide-react"
import { type Session } from "@/shared/api/sessions"
import { cn } from "@/shared/lib/utils"

/**
 * Shared session-row list: title/id, selection highlight, hover-delete.
 * Pure presentation — the caller owns fetching. Reused by the web flat rail
 * and the desktop project rail (level 2 of the project → sessions tree).
 */
export function SessionList({
  sessions,
  loading,
  selectedId,
  onSelect,
  onDelete,
  emptyHint,
}: {
  sessions: Session[]
  loading?: boolean
  selectedId: string | null
  onSelect: (s: Session) => void
  onDelete: (s: Session) => void
  emptyHint?: React.ReactNode
}) {
  return (
    <>
      {loading && (
        <p className="px-4 py-3 text-xs text-muted-foreground">Loading…</p>
      )}
      {!loading && sessions.length === 0 && (
        <p className="px-4 py-3 text-xs text-muted-foreground">
          {emptyHint ?? "No sessions yet."}
        </p>
      )}
      <ul className="flex flex-col">
        {sessions.map((s) => (
          <li
            key={s.id}
            className={cn(
              "group flex items-center gap-2 px-4 py-2 cursor-pointer hover:bg-accent",
              s.id === selectedId &&
                "bg-brand-tint hover:bg-brand-tint border-l-2 border-l-primary",
            )}
            onClick={() => onSelect(s)}
          >
            <div className="flex-1 min-w-0">
              {sessionTitle(s) ? (
                <>
                  <div className="text-xs truncate">{sessionTitle(s)}</div>
                  <div className="font-mono text-[10px] text-muted-foreground truncate">
                    {s.id.slice(0, 18)}
                  </div>
                </>
              ) : (
                <>
                  <div className="font-mono text-xs truncate">{s.id.slice(0, 18)}</div>
                  <div className="text-[10px] text-muted-foreground">
                    {s.events.length} event{s.events.length === 1 ? "" : "s"}
                  </div>
                </>
              )}
            </div>
            <button
              type="button"
              className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"
              onClick={(e) => {
                e.stopPropagation()
                onDelete(s)
              }}
              title="Delete"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </li>
        ))}
      </ul>
    </>
  )
}

/** Model-set session title (set_session_title → state.session_title), if any. */
export function sessionTitle(s: Session): string | undefined {
  const t = (s.state as Record<string, unknown> | undefined)?.["session_title"]
  return typeof t === "string" && t.trim() ? t.trim() : undefined
}
