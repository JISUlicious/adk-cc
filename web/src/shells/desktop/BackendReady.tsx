import { useEffect, useState, type ReactNode } from "react"
import { apiFetch } from "@/shared/api/client"
import { getUser, getToken, setToken } from "@/shared/api/auth"

/**
 * Desktop gate — no login. The Tauri shell runs the backend with
 * ADK_CC_ALLOW_NO_AUTH=1, so any non-empty token is accepted. We probe
 * `/list-apps` until the local sidecar answers (it may still be booting),
 * mint a placeholder token, then render the app. Replaces the web AuthGate.
 */
export function BackendReady({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function probe() {
      try {
        await apiFetch<string[]>("/list-apps", { noAuth: true })
        if (cancelled) return
        if (!getToken()) setToken("dev", getUser())
        setReady(true)
      } catch {
        if (!cancelled) timer = setTimeout(probe, 500)
      }
    }
    probe()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [])

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Starting adk-cc…</p>
      </div>
    )
  }
  return <>{children}</>
}
