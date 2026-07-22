import { Rocket, Ban, Info } from "lucide-react"

/**
 * Renders the RESOLVED outcome of an `exit_plan_mode` call.
 *
 *   args:     { plan_summary: str }
 *   response: { status, plan_summary?, user_comment?, message? }
 *     status ∈ "awaiting_user_confirmation" | "approved" | "denied" | "noop"
 *
 * A dedicated, NON-groupable card (see `isGroupableToolRow` in Thread.tsx) so
 * the plan-mode hand-off never folds into a "N tool calls" group and hides the
 * adjacent `write_plan` plan.
 *
 * While the approval is still pending (`awaiting_user_confirmation`, or no
 * response yet) this renders NOTHING — the separate "Exit plan mode?"
 * ConfirmationCard owns that state (it carries the Approve/Deny buttons + the
 * summary). This card only shows the settled result, so there's no redundant
 * or wrongly-labelled card next to the confirmation.
 */

interface ExitResp {
  status?: string
  plan_summary?: string
  user_comment?: string
  message?: string
}

export function ExitPlanCard({
  args,
  response,
  callId,
}: {
  args: unknown
  response: unknown
  callId: string
}) {
  const a = (args ?? {}) as { plan_summary?: string }
  const r = response ? (response as ExitResp) : null
  const status = r?.status

  // Pending approval → the ConfirmationCard is the UI; render nothing here.
  if (r === null || status === "awaiting_user_confirmation") return null

  const summary = r?.plan_summary ?? a.plan_summary ?? ""
  const approved = status === "approved"
  const denied = status === "denied"

  const { Icon, label, accent, badge, badgeTone } = approved
    ? {
        Icon: Rocket,
        label: "Plan approved — exiting plan mode",
        accent: "text-primary",
        badge: "approved",
        badgeTone: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
      }
    : denied
      ? {
          Icon: Ban,
          label: "Plan not approved — staying in plan mode",
          accent: "text-amber-600 dark:text-amber-400",
          badge: "declined",
          badgeTone: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
        }
      : {
          // "noop" (not in plan mode) or any unexpected status.
          Icon: Info,
          label: r?.message ?? "Not in plan mode — nothing to exit",
          accent: "text-muted-foreground",
          badge: "no-op",
          badgeTone: "bg-muted text-muted-foreground",
        }

  return (
    <div className="flex justify-start">
      <div className="w-full rounded-md border border-primary/40 bg-brand-tint text-sm">
        <div className="flex items-center gap-2 px-3 py-2">
          <Icon className={`h-4 w-4 ${accent}`} />
          <span className="text-xs font-medium flex-1">{label}</span>
          <span className={`rounded-sm px-1.5 py-0.5 text-[10px] font-medium ${badgeTone}`}>
            {badge}
          </span>
          {callId && (
            <span className="font-mono text-[10px] text-muted-foreground shrink-0">
              {callId.slice(0, 8)}
            </span>
          )}
        </div>
        {(summary || r?.user_comment) && (
          <div className="px-3 pb-3 space-y-2">
            {summary && (
              <div className="rounded bg-background/60 px-2 py-1.5 text-xs leading-relaxed text-muted-foreground whitespace-pre-wrap">
                {summary}
              </div>
            )}
            {r?.user_comment && (
              <div className="rounded bg-emerald-500/10 px-2 py-1.5 text-xs text-emerald-800 dark:text-emerald-200">
                <span className="font-medium">Your note:</span> {r.user_comment}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
