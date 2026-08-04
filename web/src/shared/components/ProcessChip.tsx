import { useCallback, useEffect, useState } from "react"
import { Terminal } from "lucide-react"
import { apiFetch } from "@/shared/api/client"
import type { ProcessRow } from "./ProcessDock"

/**
 * "2 running" beside the model chip — the ONLY always-visible surface for
 * background processes (#108).
 *
 * The dock lives in the right panel, which can be collapsed or scrolled past;
 * the whole point of the feature is that a dev server you forgot about stops
 * being invisible, so it needs one place that is always on screen. Renders
 * nothing when nothing is running, like the analysis-env chip beside it.
 */
export function ProcessChip({
  projectId,
  onClick,
}: {
  projectId: string
  onClick?: () => void
}) {
  const [running, setRunning] = useState(0)
  const [failed, setFailed] = useState(0)

  const reload = useCallback(() => {
    if (!projectId) return
    apiFetch<{ processes: ProcessRow[] }>(
      `/api/processes?project_id=${encodeURIComponent(projectId)}`,
    )
      .then((r) => {
        const rows = r.processes ?? []
        setRunning(rows.filter((p) => p.status === "running" || p.status === "starting").length)
        // A process that DIED is worth a glance even though it is not
        // running — "my server is gone" is exactly when you look here.
        setFailed(rows.filter((p) => p.status === "failed").length)
      })
      .catch(() => {})
  }, [projectId])

  useEffect(() => {
    reload()
    const iv = setInterval(reload, running ? 4000 : 15000)
    return () => clearInterval(iv)
  }, [reload, running])

  if (!running && !failed) return null

  return (
    <button
      type="button"
      onClick={onClick}
      title={
        running
          ? `${running} background process${running === 1 ? "" : "es"} running`
          : "a background process failed"
      }
      aria-label="Background processes"
      className="ml-2 flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
    >
      <Terminal className="h-3 w-3" />
      {running > 0 && (
        <span className="flex items-center gap-1">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          {running} running
        </span>
      )}
      {failed > 0 && (
        <span className="text-destructive">
          {running > 0 ? " · " : ""}
          {failed} failed
        </span>
      )}
    </button>
  )
}
