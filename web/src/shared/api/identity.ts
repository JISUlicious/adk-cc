/**
 * In-house email+password identity API.
 *
 * Talks to the adk-cc identity routes (ADK_CC_AUTH_PASSWORD=1). All three
 * calls are unauthenticated (`noAuth`) — they're how you GET a token. The
 * returned access_token is a normal Bearer JWT, stored via api/auth.ts and
 * sent on every subsequent request, exactly like a pasted/IdP token.
 */

import { apiFetch } from "./client"

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
