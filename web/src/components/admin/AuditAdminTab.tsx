import { useEffect, useState } from "react"
import { ApiError } from "@/api/client"
import { fetchAudit, type AuditEvent } from "@/api/org"

/**
 * Admin → Audit tab (Phase 6). Most-recent-first log of identity/org/account
 * actions in the caller's org: who did what, when, to which target.
 */
export function AuditAdminTab() {
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchAudit()
      .then((r) => setEvents(r.events))
      .catch((e) => setError(msg(e)))
      .finally(() => setLoading(false))
  }, [])

  if (error) return <p className="text-sm text-destructive">{error}</p>
  if (loading) return <p className="text-sm text-muted-foreground">Loading…</p>

  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <h2 className="border-b border-border px-4 py-2.5 text-sm font-semibold">
        Audit log ({events.length})
      </h2>
      {events.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted-foreground">No activity yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {events.map((e, i) => (
            <li key={i} className="flex items-baseline gap-3 px-4 py-2 text-sm">
              <time className="shrink-0 font-mono text-xs text-muted-foreground">
                {e.ts.replace("T", " ").replace("Z", "")}
              </time>
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{e.action}</span>
              <span className="min-w-0 flex-1 truncate text-muted-foreground">
                <span className="text-foreground">{e.actor}</span>
                {e.target ? <> → {e.target}</> : null}
                {e.detail ? <span className="text-xs"> ({e.detail})</span> : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function msg(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    if (err.status === 403) return "Admin access required."
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
