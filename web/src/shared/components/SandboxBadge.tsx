import { useEffect, useRef, useState } from "react"
import { ShieldCheck, ShieldAlert } from "lucide-react"
import { IS_DESKTOP } from "@/shared/lib/platform"
import { getSandbox, type SandboxStatus } from "@/shared/api/desktop-settings"

/**
 * Tiny composer chip telling the user WHERE run_bash runs for this chat:
 *   · container sandbox active  → "Sandboxed" (green shield)
 *   · sandbox on but no runtime → "Host (sandbox off)" (amber) — the fallback
 *   · host execution (default)  → nothing (the norm; no chrome)
 *
 * Desktop-only + self-gating: renders null outside a desktop build, so it's safe
 * in the shared Composer. Refreshes when `sessionId` changes (a new chat picks up
 * the current setting) since the mode applies per new chat.
 */
export function SandboxBadge({ sessionId }: { sessionId?: string | null }) {
  const [s, setS] = useState<SandboxStatus | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  useEffect(() => {
    if (!IS_DESKTOP) return
    getSandbox().then((v) => alive.current && setS(v)).catch(() => {})
  }, [sessionId])

  if (!IS_DESKTOP || !s || s.mode !== "container") return null
  const ok = s.available
  return (
    <span
      className={
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium " +
        (ok ? "bg-green-500/10 text-green-700 dark:text-green-400"
            : "bg-amber-500/10 text-amber-700 dark:text-amber-400")
      }
      title={ok
        ? `run_bash runs inside a ${s.runtime?.name ?? "container"} sandbox — host isolated`
        : "Sandbox is enabled but no runtime is available — commands run on the host"}
    >
      {ok ? <ShieldCheck className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />}
      {ok ? "Sandboxed" : "Host (sandbox off)"}
    </span>
  )
}
