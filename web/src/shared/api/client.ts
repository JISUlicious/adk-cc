/**
 * Typed fetch wrapper. Adds Bearer header from auth storage,
 * normalizes errors, JSON-handles bodies.
 *
 * URLs are relative — Vite proxies in dev, FastAPI serves in prod.
 */

import { getToken, clearToken } from "./auth"

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

  const resp = await fetch(path, { ...rest, headers: finalHeaders })

  if (resp.status === 401 && !noAuth) {
    // Token rejected — drop it so the AuthGate re-prompts.
    clearToken()
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
