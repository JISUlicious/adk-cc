/**
 * Org / team management API (Phase 3).
 *
 * Admin-only, tenant-scoped (`/orgs/*`) — the server derives the org from the
 * caller's token, so there's no tenant in the URL. Plus two public calls
 * (`/auth/invite/*`) used by the accept-invite page before the user has an account.
 */

import { apiFetch } from "./client"

export interface Member {
  id: string
  email: string
  name: string
  roles: string[]
  status: string
  created: string
}

export interface PendingInvite {
  token: string
  email: string
  role: string
  created: string
  expires: number
  status: string
}

export interface CreatedInvite {
  token: string
  url: string
  email: string
  role: string
  expires: number
}

export interface InvitePublic {
  email: string
  org: string
  role: string
}

export interface AcceptResult {
  access_token: string
  token_type: string
  user: { id: string; email: string; name: string; tenant: string; roles: string[] }
}

export function listMembers(): Promise<{ members: Member[] }> {
  return apiFetch("/orgs/members")
}

export function createUser(
  email: string,
  password: string,
  name: string,
  role: string,
): Promise<Member> {
  return apiFetch("/orgs/members", {
    method: "POST",
    body: JSON.stringify({ email, password, name, role }),
  })
}

export function createInvite(email: string, role: string): Promise<CreatedInvite> {
  return apiFetch("/orgs/invites", { method: "POST", body: JSON.stringify({ email, role }) })
}

export function listInvites(): Promise<{ invites: PendingInvite[] }> {
  return apiFetch("/orgs/invites")
}

export function revokeInvite(token: string): Promise<unknown> {
  return apiFetch(`/orgs/invites/${encodeURIComponent(token)}`, { method: "DELETE" })
}

export function setMemberRole(userId: string, role: string): Promise<Member> {
  return apiFetch(`/orgs/members/${encodeURIComponent(userId)}/role`, {
    method: "POST",
    body: JSON.stringify({ role }),
  })
}

export function disableMember(userId: string): Promise<Member> {
  return apiFetch(`/orgs/members/${encodeURIComponent(userId)}/disable`, { method: "POST" })
}

export function enableMember(userId: string): Promise<Member> {
  return apiFetch(`/orgs/members/${encodeURIComponent(userId)}/enable`, { method: "POST" })
}

// --- public (no auth) — used by the accept-invite page ---
export function getInvite(token: string): Promise<InvitePublic> {
  return apiFetch(`/auth/invite/${encodeURIComponent(token)}`, { noAuth: true })
}

export function acceptInvite(token: string, password: string, name?: string): Promise<AcceptResult> {
  return apiFetch(`/auth/invite/${encodeURIComponent(token)}/accept`, {
    method: "POST",
    noAuth: true,
    body: JSON.stringify({ password, name }),
  })
}
