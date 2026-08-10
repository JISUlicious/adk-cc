import { useState } from "react"
import { Hash, Trash2 } from "lucide-react"
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
  // Reveal-session-ids toggle (minimal, per user request): one localStorage
  // flag, no settings plumbing. Lives HERE because this list is the one
  // shared surface both shells render sessions through.
  const [showIds, setShowIds] = useState(
    () => localStorage.getItem("adk.showSessionIds") === "1",
  )
  const toggleIds = () => {
    const v = !showIds
    setShowIds(v)
    localStorage.setItem("adk.showSessionIds", v ? "1" : "0")
  }
  return (
    <>
      <div className="flex justify-end px-2 pt-1">
        <button
          type="button"
          onClick={toggleIds}
          title={showIds ? "Hide session ids" : "Show session ids"}
          className={cn(
            "adk-toggle-session-ids rounded p-1 hover:bg-accent",
            showIds ? "text-primary" : "text-muted-foreground/60",
          )}
        >
          <Hash className="h-3 w-3" />
        </button>
      </div>
      {loading && (
        <p className="px-4 py-3 text-xs text-muted-foreground">Loading…</p>
      )}
      {!loading && sessions.length === 0 && (
        <p className="px-4 py-3 text-xs text-muted-foreground">
          {emptyHint ?? "No sessions yet."}
        </p>
      )}
      <ul className="adk-session-list flex flex-col">
        {sessions.map((s) => (
          <li
            key={s.id}
            className={cn(
              "adk-session-row group flex items-center gap-2 px-4 py-2 cursor-pointer hover:bg-accent",
              s.id === selectedId &&
                "bg-brand-tint hover:bg-brand-tint border-l-2 border-l-primary",
            )}
            onClick={() => onSelect(s)}
          >
            <div className="flex-1 min-w-0">
              <div className="adk-session-title truncate text-xs">{sessionTitle(s) ?? "New Chat"}</div>
              <div className="adk-session-meta truncate text-[10px] text-muted-foreground">{fmtWhen(s.lastUpdateTime)}</div>
              {showIds && (
                <div
                  className="adk-session-id truncate font-mono text-[10px] text-muted-foreground/80 select-all"
                  title={s.id}
                  onClick={(e) => e.stopPropagation()}
                >
                  {s.id}
                </div>
              )}
            </div>
            <button
              type="button"
              className="opacity-50 transition-opacity group-hover:opacity-100 text-muted-foreground hover:text-destructive"
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

/** Compact "last updated" label from the Unix-seconds timestamp. */
function fmtWhen(ts?: number): string {
  if (!ts) return ""
  const d = new Date(ts * 1000)
  const diff = Date.now() - d.getTime()
  const m = 60_000, h = 3.6e6, day = 8.64e7
  if (diff < m) return "just now"
  if (diff < h) return `${Math.floor(diff / m)}m ago`
  if (diff < day) return `${Math.floor(diff / h)}h ago`
  if (diff < 7 * day) return `${Math.floor(diff / day)}d ago`
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
}
