import { useCallback, useEffect, useState } from "react"
import { Square, ExternalLink, X, Terminal } from "lucide-react"
import { apiFetch } from "@/shared/api/client"

/**
 * Long-running background processes for THIS PROJECT (#108).
 *
 * Scoped to the project, not the session, on purpose: a dev server started
 * in one session is still what occupies :5173 in the next, and per-session
 * scoping is precisely how a forgotten process stays forgotten.
 *
 * Sits in `RightPanelShell`'s `footer` slot, stacked with the sub-agents
 * dock — the two answer different questions ("is the agent still thinking"
 * vs "what is running on my machine"), and unlike sub-agents these persist
 * across turns because the processes do.
 */

export type ProcessRow = {
  id: string
  label: string
  command: string
  status: "starting" | "running" | "exited" | "killed" | "failed" | "unknown"
  exit_code: number | null
  port: number | null
  elapsed_s: number
  can_terminate: boolean
  adopted: boolean
}

const DOT: Record<ProcessRow["status"], string> = {
  starting: "bg-amber-500",
  running: "bg-emerald-500",
  exited: "bg-muted-foreground",
  killed: "bg-muted-foreground",
  failed: "bg-destructive",
  // Found alive at boot from an earlier backend generation, or lost track of.
  unknown: "bg-amber-600",
}

function fmt(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function statusText(p: ProcessRow): string {
  if (p.status === "running" || p.status === "starting") return fmt(p.elapsed_s)
  if (p.status === "killed") return "stopped"          // NOT "crashed": the
  // user pressed Stop; asking them to debug their own action is hostile.
  if (p.status === "failed") return `exit ${p.exit_code ?? "?"}`
  if (p.status === "unknown") return "from an earlier session"
  return `exit ${p.exit_code ?? 0}`
}

export function ProcessDock({
  projectId,
  onOpenLog,
}: {
  projectId: string
  onOpenLog?: (p: ProcessRow) => void
}) {
  const [rows, setRows] = useState<ProcessRow[]>([])
  const [busy, setBusy] = useState<string | null>(null)

  const reload = useCallback(() => {
    if (!projectId) return
    apiFetch<{ processes: ProcessRow[] }>(
      `/api/processes?project_id=${encodeURIComponent(projectId)}`,
    )
      .then((r) => setRows(r.processes ?? []))
      .catch(() => {})
  }, [projectId])

  const live = rows.filter((r) => r.status === "running" || r.status === "starting")
  useEffect(() => {
    reload()
    // Faster while something is live (a starting server changes state and
    // discovers its port); slow otherwise so an idle project costs nothing.
    const iv = setInterval(reload, live.length ? 2500 : 15000)
    return () => clearInterval(iv)
  }, [reload, live.length])

  async function stop(id: string) {
    setBusy(id)
    try {
      await apiFetch(`/api/processes/${encodeURIComponent(id)}/terminate`, {
        method: "POST",
      })
      reload()
    } catch {
      /* the row's status is the source of truth; a failed stop shows there */
    } finally {
      setBusy(null)
    }
  }

  async function forget(id: string) {
    try {
      await apiFetch(`/api/processes/${encodeURIComponent(id)}/forget`, {
        method: "POST",
      })
      reload()
    } catch {
      /* ignore */
    }
  }

  if (rows.length === 0) return null

  return (
    <div className="border-t border-border bg-card/30 px-3 py-2" data-process-dock>
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
        <Terminal className="h-3.5 w-3.5" />
        Processes
        <span className="font-normal">
          {live.length ? `${live.length} running` : "none running"}
        </span>
      </div>
      <div className="space-y-0.5">
        {rows.slice(0, 6).map((p) => {
          const isLive = p.status === "running" || p.status === "starting"
          return (
            <div key={p.id} className="flex items-center gap-1.5 text-[11px]"
                 data-process={p.id}>
              <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[p.status]}`} />
              <button
                type="button"
                onClick={() => onOpenLog?.(p)}
                title={p.command}
                aria-label={`Open log for ${p.label}`}
                className="min-w-0 flex-1 truncate text-left text-muted-foreground hover:text-foreground"
              >
                {p.label}
              </button>
              {p.port && isLive && (
                <a
                  href={`http://localhost:${p.port}`}
                  target="_blank"
                  rel="noreferrer"
                  className="shrink-0 text-primary hover:underline"
                  title={`Open http://localhost:${p.port}`}
                >
                  :{p.port} <ExternalLink className="inline h-2.5 w-2.5" />
                </a>
              )}
              <span className="shrink-0 tabular-nums text-muted-foreground/80">
                {statusText(p)}
              </span>
              {/* A backend that cannot stop a process shows NO button: a dead
                  control is worse than an absent one. */}
              {isLive && p.can_terminate && (
                <button
                  type="button"
                  onClick={() => stop(p.id)}
                  disabled={busy === p.id || p.status === "starting"}
                  className="shrink-0 text-muted-foreground hover:text-destructive disabled:opacity-40"
                  title="Stop this process"
                  aria-label={`Stop ${p.label}`}
                >
                  <Square className="h-3 w-3" />
                </button>
              )}
              {!isLive && (
                <button
                  type="button"
                  onClick={() => forget(p.id)}
                  className="shrink-0 text-muted-foreground hover:text-foreground"
                  title="Remove from this list (the log file stays)"
                  aria-label={`Dismiss ${p.label}`}
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
