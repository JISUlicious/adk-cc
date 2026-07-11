/**
 * In-house email+password identity API.
 *
 * Talks to the adk-cc identity routes (ADK_CC_AUTH_PASSWORD=1). All three
 * calls are unauthenticated (`noAuth`) — they're how you GET a token. The
 * returned access_token is a normal Bearer JWT, stored via api/auth.ts and
 * sent on every subsequent request, exactly like a pasted/IdP token.
 */

import { apiFetch } from "./client"
import { getRefresh } from "./auth"

export interface AuthConfig {
  /** provider id, e.g. "password" */
  id: string
  /** email+password login is available */
  password: boolean
  /** self-serve signup is available (multi-tenant mode) */
  registration: boolean
  /** user-initiated "request access" is available (admin approves; single mode) */
  access_requests: boolean
  /** SSO buttons available (future OIDC/Keycloak variant) */
  sso: boolean
  /** "single" | "multi" */
  mode: string
}

export interface AuthUser {
  id: string
  email: string
  name: string
  tenant: string
  roles: string[]
}

export interface LoginResult {
  access_token: string
  token_type: string
  user: AuthUser
  /** Long-lived rotating token for POST /auth/refresh (absent on deployments
   * without a refresh store). */
  refresh_token?: string
}

/** Ask the server which login methods are live (so the UI renders the right
 * form). Rejects on deployments with no in-house identity provider — callers
 * treat that as "fall back to token paste". */
export function fetchAuthConfig(): Promise<AuthConfig> {
  return apiFetch<AuthConfig>("/auth/config", { noAuth: true })
}

export function login(email: string, password: string): Promise<LoginResult> {
  return apiFetch<LoginResult>("/auth/login", {
    method: "POST",
    noAuth: true,
    body: JSON.stringify({ email, password }),
  })
}

export function signup(args: {
  email: string
  password: string
  name?: string
  org?: string
}): Promise<LoginResult> {
  return apiFetch<LoginResult>("/auth/signup", {
    method: "POST",
    noAuth: true,
    body: JSON.stringify(args),
  })
}

/** Real logout: revoke the stored refresh token server-side. Uses a raw fetch
 * with `keepalive` (NOT apiFetch) because every caller navigates away
 * (location.assign) on the next line — without keepalive the browser aborts the
 * in-flight request on unload and the token is never actually revoked. */
export function revokeSession(): void {
  const rt = getRefresh()
  if (!rt) return
  try {
    void fetch("/auth/logout", {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    }).catch(() => {})
  } catch {
    /* fetch threw synchronously (very old browsers) — nothing we can do */
  }
}

// --- password reset (public one-time link minted by an admin) ---
export function getReset(token: string): Promise<{ email: string; name: string }> {
  return apiFetch(`/auth/reset/${encodeURIComponent(token)}`, { noAuth: true })
}

/** Consume the reset link: set a new password. Signs the holder in (returns
 * a full token pair) — possession of the link is the proof. */
export function completeReset(token: string, password: string): Promise<LoginResult> {
  return apiFetch<LoginResult>(`/auth/reset/${encodeURIComponent(token)}/complete`, {
    method: "POST",
    noAuth: true,
    body: JSON.stringify({ password }),
  })
}

/** File a pending access request (the mirror of an invite): an org admin must
 * approve it before the account can sign in. No token comes back. */
export function requestAccess(args: {
  email: string
  password: string
  name?: string
  note?: string
}): Promise<{ status: string }> {
  return apiFetch<{ status: string }>("/auth/request-access", {
    method: "POST",
    noAuth: true,
    body: JSON.stringify(args),
  })
}
