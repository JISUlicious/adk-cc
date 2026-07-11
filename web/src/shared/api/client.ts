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

// "refreshed" → a new access token is now stored, retry the request.
// "dead"      → the refresh token was genuinely rejected (401); sign out.
// "transient" → a network blip / 429 / 5xx; the refresh token is still valid,
//               so DON'T sign out — just surface the original error.
type RefreshResult = "refreshed" | "dead" | "transient"

// One shared in-flight refresh per tab so a burst of concurrent 401s rotates
// the token once; navigator.locks serializes ACROSS tabs (below) so two tabs
// sharing localStorage can't race-rotate and revoke each other's session.
let _refreshing: Promise<RefreshResult> | null = null

async function doRefresh(): Promise<RefreshResult> {
  let rt: string | null
  try {
    rt = getRefresh()
  } catch {
    return "transient" // storage blocked (private mode) — don't force a logout
  }
  if (!rt) return "dead" // no refresh token (opaque/dev token) → 401 means re-login

  const run = async (): Promise<RefreshResult> => {
    // Another tab may have rotated while we waited for the cross-tab lock —
    // if the stored refresh token changed, adopt it instead of racing.
    try {
      if (getRefresh() !== rt) return "refreshed"
    } catch {
      return "transient"
    }
    try {
      const resp = await fetch("/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: rt }),
      })
      if (resp.status === 401) return "dead" // token genuinely rejected
      if (!resp.ok) return "transient" // 429 / 5xx → keep the token, retry later
      const d = (await resp.json()) as {
        access_token: string
        refresh_token?: string
        user?: { id?: string }
      }
      setToken(d.access_token, d.user?.id, d.refresh_token)
      return "refreshed"
    } catch {
      return "transient" // network error → keep the token
    }
  }

  // Web Locks serialize refresh across all tabs of this origin; fall back to a
  // plain call where unavailable (older browsers / non-secure contexts).
  const locks = (navigator as { locks?: LockManager }).locks
  return locks?.request ? locks.request("adk_cc_refresh", run) : run()
}

function tryRefresh(): Promise<RefreshResult> {
  if (!_refreshing) {
    _refreshing = doRefresh()
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
    await tryRefresh() // result ignored — a real 401 (if any) is handled by apiFetch
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
    const r = await tryRefresh()
    if (r === "refreshed") {
      const t = getToken()
      if (t) finalHeaders.set("Authorization", `Bearer ${t}`)
      resp = await fetch(path, { ...rest, headers: finalHeaders })
      if (resp.status === 401) clearToken() // fresh token still rejected → sign out
    } else if (r === "dead") {
      clearToken() // refresh token genuinely rejected → AuthGate re-prompts
    }
    // r === "transient": a valid session hit a blip — keep the tokens and let
    // the original 401 surface as an error the caller can retry, NOT a logout.
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
