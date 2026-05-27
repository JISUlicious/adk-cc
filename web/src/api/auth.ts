/**
 * Token storage + retrieval. JWT (production) or Bearer (dev).
 *
 * Production OIDC flow: redirects to the IdP at /authorize, receives
 * the access token at the callback page, persists here. v1 simplification:
 * the user pastes a token into the login form; we treat it as already-issued.
 * Full OIDC redirect dance is a Phase 2+ task.
 */

const TOKEN_KEY = "adk_cc.token"
const TOKEN_USER_KEY = "adk_cc.user"

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string, user?: string): void {
  localStorage.setItem(TOKEN_KEY, token)
  if (user) localStorage.setItem(TOKEN_USER_KEY, user)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(TOKEN_USER_KEY)
}

export function getUser(): string {
  return localStorage.getItem(TOKEN_USER_KEY) ?? "alice"
}

/** Decode the JWT payload for display purposes only. Does NOT verify
 * signatures — the FastAPI side does that via JwtAuthExtractor. */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".")
    if (parts.length !== 3) return null
    const payload = parts[1]
    const decoded = atob(payload.replace(/-/g, "+").replace(/_/g, "/"))
    return JSON.parse(decoded)
  } catch {
    return null
  }
}
