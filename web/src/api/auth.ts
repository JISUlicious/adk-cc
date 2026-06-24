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

// Subscribers notified when the token is cleared (e.g. a 401 mid-session),
// so the AuthGate can drop back to the login form. Module-level so apiFetch
// and the gate share one channel without prop-drilling.
const _authClearedSubs = new Set<() => void>()

export function onAuthCleared(fn: () => void): () => void {
  _authClearedSubs.add(fn)
  return () => _authClearedSubs.delete(fn)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(TOKEN_USER_KEY)
  for (const fn of _authClearedSubs) {
    try {
      fn()
    } catch {
      // a bad subscriber must not break token clearing
    }
  }
}

export function getUser(): string {
  return localStorage.getItem(TOKEN_USER_KEY) ?? "alice"
}

// An EXPLICIT sign-out marker (per-tab). Without it, signing out on a no-auth
// dev server bounces straight back to the app: the AuthGate finds no token,
// probes /list-apps anonymously, gets 200, and auto-signs-in again. The marker
// tells the AuthGate to skip that auto-login and show the login form. It's
// cleared on the next successful sign-in. Not set by the 401 auto-clear, so an
// expired token still re-prompts normally.
const SIGNED_OUT_KEY = "adk_cc.signed_out"

export function markSignedOut(): void {
  try {
    sessionStorage.setItem(SIGNED_OUT_KEY, "1")
  } catch {
    /* sessionStorage unavailable — ignore */
  }
}

export function isSignedOut(): boolean {
  try {
    return sessionStorage.getItem(SIGNED_OUT_KEY) === "1"
  } catch {
    return false
  }
}

export function clearSignedOut(): void {
  try {
    sessionStorage.removeItem(SIGNED_OUT_KEY)
  } catch {
    /* ignore */
  }
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

/** Best-effort roles for UX gating ONLY (hiding the admin link). The real
 * gate is server-side: admin routes return 403 regardless of what the UI
 * shows. JWTs expose roles in a claim we can read; opaque dev bearer tokens
 * carry roles only server-side, so we return null = "unknown" and the UI
 * shows the admin entry, letting the API enforce. */
export function roleHints(): string[] | null {
  const token = getToken()
  if (!token) return []
  const payload = decodeJwtPayload(token)
  if (!payload) return null // opaque token → unknown, don't hide
  const claim = (payload["roles"] ?? payload["role"]) as unknown
  if (Array.isArray(claim)) return claim.map(String)
  if (typeof claim === "string") return claim.split(/[\s,]+/).filter(Boolean)
  return [] // JWT without a roles claim → not an admin
}

/** Whether to show the admin entry point. True when roles are unknown
 * (opaque token — let the server decide) or include the admin role. */
export function maybeAdmin(adminRole = "admin"): boolean {
  const roles = roleHints()
  if (roles === null) return true // unknown → show, server enforces
  return roles.includes(adminRole)
}
