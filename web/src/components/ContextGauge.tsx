import { type ContextLimits } from "@/api/context"

/**
 * Context-fullness gauge (compaction-indicator P2). Shows how full the model's
 * context window is, so the user can see compaction coming. `current` is the
 * latest reported prompt_token_count; `limits` is the server's resolved ladder.
 *
 * Renders nothing when the guard is disabled (no effective window) or no usage
 * has been reported yet. Color shifts amber at the WARN ratio, red at REJECT —
 * the same ladder the server enforces.
 */
export function ContextGauge({
  current,
  limits,
}: {
  current: number
  limits: ContextLimits | null
}) {
  const effective = limits?.effective
  if (!effective || effective <= 0 || current <= 0) return null

  const ratio = Math.min(1, current / effective)
  const pct = Math.round(ratio * 100)
  const warn = limits?.warn ?? effective * 0.75
  const reject = limits?.reject ?? effective * 0.95

  const color =
    current >= reject
      ? "bg-destructive"
      : current >= warn
        ? "bg-amber-500"
        : "bg-emerald-500"
  const label =
    current >= reject ? "context full" : current >= warn ? "context low" : ""

  return (
    <div
      className="hidden sm:flex items-center gap-1.5"
      title={`Context: ~${current.toLocaleString()} / ${effective.toLocaleString()} tokens (${pct}%)`}
    >
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[11px] tabular-nums text-muted-foreground">
        {pct}%{label ? ` · ${label}` : ""}
      </span>
    </div>
  )
}
