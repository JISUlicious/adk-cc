import { useEffect, useRef, useState } from "react"
import { Server, ShieldCheck, ShieldAlert } from "lucide-react"
import { IS_DESKTOP } from "@/shared/lib/platform"
import { getSessionBackend, type SessionBackend } from "@/shared/api/desktop-settings"

/**
 * Tiny composer chip telling the user WHERE this chat's commands run —
 * the SESSION's resolved backend (per-session truth endpoint), not the
 * global setting, which can diverge from it (container→host fallback,
 * per-session overrides, per-project SSH):
 *   · container (sandbox up)   → "Sandboxed" (green shield)
 *   · container, no runtime    → "Host (sandbox off)" (amber) — fallback
 *   · ssh                      → "SSH: <host>" (blue server) — remote, NOT
 *                                 containerized; the title says so plainly
 *   · other isolated backends  → "Sandboxed" (green shield)
 *   · host / noop (default)    → nothing (the norm; no chrome)
 *
 * Desktop-only + self-gating: renders null outside a desktop build, so it's
 * safe in the shared Composer. Refetches when `sessionId` changes; source
 * flips config→live automatically after the first turn seeds the backend.
 */
export function SandboxBadge({ sessionId }: { sessionId?: string | null }) {
  const [s, setS] = useState<SessionBackend | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      if (!IS_DESKTOP || !sessionId) {
        if (!cancelled && alive.current) setS(null)
        return
      }
      try {
        const v = await getSessionBackend(sessionId)
        if (!cancelled && alive.current) setS(v)
      } catch {
        if (!cancelled && alive.current) setS(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId])

  if (!IS_DESKTOP || !s) return null

  const chip = (cls: string, icon: React.ReactNode, label: string, title: string) => (
    <span
      className={
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium " + cls
      }
      title={title}
    >
      {icon}
      {label}
    </span>
  )

  if (s.backend === "ssh") {
    return chip(
      "bg-sky-500/10 text-sky-700 dark:text-sky-400",
      <Server className="h-3 w-3" />,
      `SSH: ${s.detail || "remote"}`,
      "Commands run on the remote device over SSH as your remote account — " +
        "remote, but NOT containerized",
    )
  }
  if (s.backend === "container") {
    const ok = s.available !== false // live source has no `available`; trust it
    return chip(
      ok
        ? "bg-green-500/10 text-green-700 dark:text-green-400"
        : "bg-amber-500/10 text-amber-700 dark:text-amber-400",
      ok ? <ShieldCheck className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />,
      ok ? "Sandboxed" : "Host (sandbox off)",
      ok
        ? "This chat's commands run inside a container sandbox — host isolated"
        : "Sandbox is enabled but no runtime is available — commands run on the host",
    )
  }
  if (s.isolated) {
    return chip(
      "bg-green-500/10 text-green-700 dark:text-green-400",
      <ShieldCheck className="h-3 w-3" />,
      "Sandboxed",
      `This chat's commands run in an isolated ${s.backend} sandbox`,
    )
  }
  return null // host exec — the norm, no chrome
}
