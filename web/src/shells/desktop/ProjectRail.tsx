import { useEffect, useState } from "react"
import { Plus, X, Settings as SettingsIcon } from "lucide-react"
import {
  createSession, deleteSession, listApps, listSessions, type Session,
} from "@/shared/api/sessions"
import { Button } from "@/shared/components/ui/button"
import { cn } from "@/shared/lib/utils"
import { SessionList } from "@/shared/sessions/SessionList"
import { type RailProps } from "@/shared/components/SessionRail"

/**
 * Desktop rail. Phase 1: a flat session list (no projects yet) with a Settings
 * gear footer — no account identity / sign-out (single local user).
 * Phase 2 turns this into the two-level Projects → Sessions tree.
 */
export function ProjectRail({
  userId, appName, onAppChange, sessionId, onSelect, refreshTick,
  open, onClose, onOpenSettings, secretsMissing = 0,
}: RailProps) {
  const [apps, setApps] = useState<string[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listApps()
      .then((xs) => {
        if (cancelled) return
        setApps(xs)
        if (appName === null && xs.length > 0) onAppChange(xs[0])
      })
      .catch((e) => { if (!cancelled) setError(`Failed to load apps: ${e.message}`) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!appName) return
    let cancelled = false
    setLoading(true)
    listSessions(appName, userId)
      .then((xs) => {
        if (cancelled) return
        xs.sort((a, b) => (b.lastUpdateTime || 0) - (a.lastUpdateTime || 0))
        setSessions(xs)
      })
      .catch((e) => { if (!cancelled) setError(`Failed to load sessions: ${e.message}`) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
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
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-foreground/30 lg:hidden" aria-hidden onClick={onClose} />
      )}
      <aside
        className={cn(
          "flex w-72 max-w-[85vw] flex-col border-r border-border/60",
          "bg-muted shadow-xl lg:bg-muted/40 lg:shadow-none",
          "fixed inset-y-0 left-0 z-40 transform transition-transform duration-200 ease-out",
          "lg:static lg:z-auto lg:max-w-none lg:translate-x-0 lg:transition-none",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <button
          type="button" onClick={onClose} title="Close"
          className="absolute right-2 top-2 z-10 rounded-md p-1.5 text-muted-foreground hover:bg-accent lg:hidden"
        >
          <X className="h-4 w-4" />
        </button>
        <div className="flex items-center gap-2 px-4 py-3.5">
          <img src="/favicon.svg" alt="" className="h-6 w-6 shrink-0" />
          <span className="text-base font-semibold tracking-tight">adk-cc</span>
          {apps.length > 1 && (
            <select
              value={appName ?? ""} onChange={(e) => onAppChange(e.target.value)} title="Agent"
              className="ml-auto rounded-md border border-input bg-background px-1.5 py-1 text-xs"
            >
              {apps.map((a) => (<option key={a} value={a}>{a}</option>))}
            </select>
          )}
        </div>
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-xs font-medium text-muted-foreground">Sessions</span>
          <Button size="sm" variant="outline" onClick={handleNew} disabled={!appName} title="New session">
            <Plus className="h-3.5 w-3.5" /> New
          </Button>
        </div>
        {error && <p className="px-4 py-2 text-xs text-destructive">{error}</p>}
        <div className="flex-1 overflow-y-auto">
          <SessionList
            sessions={sessions}
            loading={loading}
            selectedId={sessionId}
            onSelect={(s) => onSelect(s)}
            onDelete={handleDelete}
            emptyHint={<>No sessions yet. Click <span className="font-mono">+ New</span> to start one.</>}
          />
        </div>
        {/* Footer: just the Settings gear — no identity / sign-out on desktop. */}
        <div className="border-t border-border/60 p-2">
          <button
            type="button" onClick={onOpenSettings}
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm text-muted-foreground hover:bg-accent"
            title={secretsMissing > 0 ? `Settings — ${secretsMissing} value(s) need setup` : "Settings"}
          >
            <span className="relative">
              <SettingsIcon className="h-4 w-4" />
              {secretsMissing > 0 && (
                <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[9px] font-medium text-white">
                  {secretsMissing}
                </span>
              )}
            </span>
            Settings
          </button>
        </div>
      </aside>
    </>
  )
}
