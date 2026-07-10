/**
 * Typed fetch wrapper. Adds Bearer header from auth storage,
 * normalizes errors, JSON-handles bodies.
 *
 * URLs are relative — Vite proxies in dev, FastAPI serves in prod.
 */

import { getToken, clearToken, getRefresh, setToken, decodeJwtPayload } from "./auth"

export class ApiError extends Error {
  status: number
  body?: unknown

  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

interface FetchOptions extends RequestInit {
  /** Skip the auth header (for login form etc.). */
  noAuth?: boolean
}

// One shared in-flight refresh so a burst of concurrent 401s rotates the
// refresh token exactly once (rotation makes a second parallel attempt fail).
let _refreshing: Promise<boolean> | null = null

function tryRefresh(): Promise<boolean> {
  if (!_refreshing) {
    _refreshing = (async () => {
      const rt = getRefresh()
      if (!rt) return false
      try {
        const resp = await fetch("/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: rt }),
        })
        if (!resp.ok) return false
        const d = (await resp.json()) as {
          access_token: string
          refresh_token?: string
          user?: { id?: string }
        }
        setToken(d.access_token, d.user?.id, d.refresh_token)
        return true
      } catch {
        return false
      }
    })()
    void _refreshing.finally(() => {
      _refreshing = null
    })
  }
  return _refreshing
}

/** Proactively refresh when the stored access JWT is expired or about to be
 * (<60s left). For callers that bypass apiFetch (the SSE stream) and so can't
 * rely on the 401-retry below. No-op for opaque dev tokens / no refresh token. */
export async function ensureFreshAccess(): Promise<void> {
  const tok = getToken()
  if (!tok || !getRefresh()) return
  const exp = decodeJwtPayload(tok)?.exp
  if (typeof exp === "number" && exp - Date.now() / 1000 < 60) {
    await tryRefresh()
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  opts: FetchOptions = {},
): Promise<T> {
  const { noAuth, headers, ...rest } = opts
  const finalHeaders = new Headers(headers)
  if (!finalHeaders.has("Content-Type") && rest.body) {
    finalHeaders.set("Content-Type", "application/json")
  }
  if (!noAuth) {
    const token = getToken()
    if (token) finalHeaders.set("Authorization", `Bearer ${token}`)
  }

  let resp = await fetch(path, { ...rest, headers: finalHeaders })

  if (resp.status === 401 && !noAuth) {
    // Access token expired? Silently refresh once and replay the request.
    // (Bodies here are strings/ArrayBuffers, never streams, so replay is safe.)
    if (await tryRefresh()) {
      const t = getToken()
      if (t) finalHeaders.set("Authorization", `Bearer ${t}`)
      resp = await fetch(path, { ...rest, headers: finalHeaders })
    }
    if (resp.status === 401) {
      // Still rejected — drop the tokens so the AuthGate re-prompts.
      clearToken()
    }
  }

  if (!resp.ok) {
    let body: unknown = undefined
    try {
      body = await resp.json()
    } catch {
      // ignore — server returned non-JSON error
    }
    throw new ApiError(
      resp.status,
      `${resp.status} ${resp.statusText}`,
      body,
    )
  }

  // 204 No Content
  if (resp.status === 204) return undefined as T
  const ct = resp.headers.get("Content-Type") ?? ""
  if (ct.includes("application/json")) return resp.json() as Promise<T>
  return resp.text() as unknown as T
}
