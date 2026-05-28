import { useEffect, useState } from "react"
import { Plus, Trash2 } from "lucide-react"
import {
  createSession,
  deleteSession,
  listApps,
  listSessions,
  type Session,
} from "@/api/sessions"
import { Button } from "./ui/button"
import { cn } from "@/lib/utils"

/**
 * Left rail: app picker (when more than one is registered) + the
 * current user's session list + new/delete session controls.
 *
 * The rail owns its own data fetching for session list. ChatPage owns
 * the *currently displayed* session — the rail just notifies it via
 * `onSelect` when the user clicks one.
 */
export function SessionRail({
  userId,
  appName,
  onAppChange,
  sessionId,
  onSelect,
  /** Bumped by ChatPage when a new turn lands so the rail can refresh
   * lastUpdateTime + ordering. */
  refreshTick,
}: {
  userId: string
  appName: string | null
  onAppChange: (app: string) => void
  sessionId: string | null
  onSelect: (s: Session | null) => void
  refreshTick: number
}) {
  const [apps, setApps] = useState<string[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Load app list once. /list-apps is cheap, returns all agents
  // registered under AGENTS_DIR.
  useEffect(() => {
    let cancelled = false
    listApps()
      .then((xs) => {
        if (cancelled) return
        setApps(xs)
        if (appName === null && xs.length > 0) {
          onAppChange(xs[0])
        }
      })
      .catch((e) => {
        if (!cancelled) setError(`Failed to load apps: ${e.message}`)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Reload sessions whenever the selected app changes or the parent
  // signals a turn-landed refresh.
  useEffect(() => {
    if (!appName) return
    let cancelled = false
    setLoading(true)
    listSessions(appName, userId)
      .then((xs) => {
        if (cancelled) return
        // Newest first by lastUpdateTime.
        xs.sort((a, b) => (b.lastUpdateTime || 0) - (a.lastUpdateTime || 0))
        setSessions(xs)
      })
      .catch((e) => {
        if (!cancelled) setError(`Failed to load sessions: ${e.message}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [appName, userId, refreshTick])

  async function handleNew() {
    if (!appName) return
    try {
      const s = await createSession(appName, userId, {})
      setSessions((prev) => [s, ...prev])
      onSelect(s)
    } catch (e) {
      setError(`Failed to create session: ${(e as Error).message}`)
    }
  }

  async function handleDelete(s: Session) {
    if (!appName) return
    if (!confirm(`Delete session ${s.id.slice(0, 8)}…?`)) return
    try {
      await deleteSession(appName, userId, s.id)
      setSessions((prev) => prev.filter((x) => x.id !== s.id))
      if (sessionId === s.id) onSelect(null)
    } catch (e) {
      setError(`Failed to delete: ${(e as Error).message}`)
    }
  }

  return (
    <aside className="flex w-72 flex-col bg-muted/40 border-r border-border/60">
      <div className="px-4 py-3">
        <label className="text-xs font-medium text-muted-foreground">
          Agent
        </label>
        <select
          value={appName ?? ""}
          onChange={(e) => onAppChange(e.target.value)}
          disabled={apps.length === 0}
          className="mt-1 block w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
        >
          {apps.length === 0 && <option value="">— none —</option>}
          {apps.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>
      <div className="flex items-center justify-between px-4 py-3">
        <span className="text-xs font-medium text-muted-foreground">
          Sessions
        </span>
        <Button
          size="sm"
          variant="outline"
          onClick={handleNew}
          disabled={!appName}
          title="New session"
        >
          <Plus className="h-3.5 w-3.5" />
          New
        </Button>
      </div>
      {error && (
        <p className="px-4 py-2 text-xs text-destructive">{error}</p>
      )}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <p className="px-4 py-3 text-xs text-muted-foreground">
            Loading…
          </p>
        )}
        {!loading && sessions.length === 0 && (
          <p className="px-4 py-3 text-xs text-muted-foreground">
            No sessions yet. Click <span className="font-mono">+ New</span>{" "}
            to start one.
          </p>
        )}
        <ul className="flex flex-col">
          {sessions.map((s) => (
            <li
              key={s.id}
              className={cn(
                "group flex items-center gap-2 px-4 py-2 cursor-pointer hover:bg-accent",
                // Selected uses brand-tint so the active row is
                // distinguishable from a row the cursor is just
                // passing over (which gets the warm hover above).
                s.id === sessionId &&
                  "bg-brand-tint hover:bg-brand-tint border-l-2 border-l-primary",
              )}
              onClick={() => onSelect(s)}
            >
              <div className="flex-1 min-w-0">
                <div className="font-mono text-xs truncate">
                  {s.id.slice(0, 18)}
                </div>
                <div className="text-[10px] text-muted-foreground">
                  {s.events.length} event{s.events.length === 1 ? "" : "s"}
                </div>
              </div>
              <button
                type="button"
                className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"
                onClick={(e) => {
                  e.stopPropagation()
                  handleDelete(s)
                }}
                title="Delete"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  )
}
