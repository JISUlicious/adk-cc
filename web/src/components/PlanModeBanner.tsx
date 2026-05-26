import { ClipboardList } from "lucide-react"

/**
 * Sticky banner at the top of the thread when the session is in PLAN
 * permission mode. `permission_mode` lives at `session.state.permission_mode`
 * and takes the string values defined in
 * `adk_cc/permissions/modes.py::PermissionMode` (default / plan /
 * acceptEdits / bypassPermissions / dontAsk).
 *
 * We render only the plan-mode case since it has the strongest UX
 * impact (the agent cannot act — only plan), and a quiet pill for
 * other non-default modes might land in a polish pass.
 */
export function PlanModeBanner({ mode }: { mode: string | undefined }) {
  if (mode !== "plan") return null
  return (
    <div className="border-b bg-violet-50/60 dark:bg-violet-950/30 px-6 py-2 text-sm flex items-center gap-2">
      <ClipboardList className="h-4 w-4 text-violet-600 dark:text-violet-400" />
      <span className="font-medium text-violet-900 dark:text-violet-200">
        Plan mode
      </span>
      <span className="text-xs text-violet-700 dark:text-violet-300">
        — the agent can investigate and plan but will not execute
        destructive tools until you exit plan mode.
      </span>
    </div>
  )
}
