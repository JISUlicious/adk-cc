import { useEffect, useState } from "react"
import { ApiError } from "@/api/client"
import { fetchUsage, type UsageRow } from "@/api/org"

/**
 * Admin → Usage tab (Phase 6). Per-user activity for the caller's org, derived
 * from the audit log (event count + last active), joined with the member list.
 */
export function UsageAdminTab() {
  const [rows, setRows] = useState<UsageRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetchUsage()
      .then((r) => setRows(r.users))
      .catch((e) => setError(msg(e)))
      .finally(() => setLoading(false))
  }, [])

  if (error) return <p className="text-sm text-destructive">{error}</p>
  if (loading) return <p className="text-sm text-muted-foreground">Loading…</p>

  return (
    <section className="overflow-hidden rounded-lg border border-border">
      <h2 className="border-b border-border px-4 py-2.5 text-sm font-semibold">
        Activity by user ({rows.length})
      </h2>
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th className="px-4 py-2 text-left font-medium">User</th>
            <th className="px-4 py-2 text-left font-medium">Role</th>
            <th className="px-4 py-2 text-left font-medium">Status</th>
            <th className="px-4 py-2 text-right font-medium">Events</th>
            <th className="px-4 py-2 text-left font-medium">Last active</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((u) => (
            <tr key={u.id}>
              <td className="px-4 py-2">{u.email}</td>
              <td className="px-4 py-2 text-muted-foreground">{u.roles.join(", ") || "member"}</td>
              <td className="px-4 py-2">
                {u.status === "disabled"
                  ? <span className="text-amber-600">disabled</span>
                  : <span className="text-muted-foreground">active</span>}
              </td>
              <td className="px-4 py-2 text-right tabular-nums">{u.events}</td>
              <td className="px-4 py-2 text-muted-foreground">
                {u.last_active ? u.last_active.replace("T", " ").replace("Z", " UTC") : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
