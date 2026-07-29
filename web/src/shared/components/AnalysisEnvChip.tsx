import { useEffect, useState } from "react"
import { FlaskConical, Loader2, AlertTriangle } from "lucide-react"
import { analysisEnvStatus, type AnalysisEnvStatus } from "@/shared/api/desktop-settings"
import { cn } from "@/shared/lib/utils"

/**
 * Analysis-runtime chip for the composer meta row (W6.5).
 *
 * W1 provisions a uv-managed Python per project on first use. That first use
 * costs ~20-60s of package installs, and until now nothing said so: a dataset
 * profile or an analysis turn simply appeared to hang, and a FAILED provision
 * (no uv on PATH, a resolver error) surfaced only as a tool error deep inside a
 * turn. Both are legible here instead.
 *
 * Deliberately quiet when there is nothing to say: an env that is `ready` with
 * the base tier, or a deployment using an operator-supplied interpreter, adds
 * no chip — the composer row is not a status dashboard.
 *
 * Polling is slow (10s) and fast only while provisioning (2s), because the
 * status endpoint is a filesystem read that must never trigger the install it
 * is reporting on.
 */
export function AnalysisEnvChip({
  projectId,
  sessionId,
  refreshKey,
}: {
  projectId: string
  sessionId: string
  /** Bumped after each turn — a turn is what usually provisions the env. */
  refreshKey?: number
}) {
  const [st, setSt] = useState<AnalysisEnvStatus | null>(null)

  useEffect(() => {
    if (!projectId || !sessionId) return
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const next = await analysisEnvStatus(projectId, sessionId)
        if (!live) return
        setSt(next)
        timer = setTimeout(tick, next.state === "provisioning" ? 2000 : 10000)
      } catch {
        if (!live) return
        setSt(null)                       // no workspace bound yet — say nothing
        timer = setTimeout(tick, 15000)
      }
    }
    void tick()
    return () => {
      live = false
      if (timer) clearTimeout(timer)
    }
  }, [projectId, sessionId, refreshKey])

  if (!st) return null
  const { state } = st

  // Nothing useful to report: don't spend a chip on it.
  if (state === "external" || state === "off") return null
  if (state === "ready" && (st.tiers ?? []).join() === "base") return null

  const view =
    state === "provisioning"
      ? {
          Icon: Loader2,
          spin: true,
          text: `preparing analysis env${st.seconds ? ` · ${st.seconds}s` : ""}`,
          tone: "text-amber-600 dark:text-amber-400",
        }
      : state === "ready"
        ? {
            Icon: FlaskConical,
            spin: false,
            text: (st.tiers ?? []).join(" + "),
            tone: "text-muted-foreground",
          }
        : state === "absent"
          ? {
              Icon: FlaskConical,
              spin: false,
              text: "analysis env not built yet",
              tone: "text-muted-foreground",
            }
          : {
              Icon: AlertTriangle,
              spin: false,
              text: "analysis env unavailable",
              tone: "text-destructive",
            }

  return (
    <span
      className={cn("adk-env-chip inline-flex items-center gap-1 text-[11px]", view.tone)}
      title={st.detail ?? state}
    >
      <view.Icon className={cn("h-3 w-3", view.spin && "animate-spin")} />
      {view.text}
    </span>
  )
}
