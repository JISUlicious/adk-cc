/**
 * Account self-service API (Phase 4). All calls are authenticated and act on
 * the signed-in user's own account.
 */

import { apiFetch } from "./client"

export interface Me {
  id: string
  email: string
  name: string
  tenant: string
  roles: string[]
  scopes?: string[]
}

export interface ApiKey {
  id: string
  name: string
  created: string
  last_used: string
  revoked: boolean
}

export interface CreatedApiKey {
  id: string
  name: string
  created: string
  token: string // shown ONCE
}

export function getMe(): Promise<Me> {
  return apiFetch("/auth/me")
}

export function updateProfile(name: string): Promise<Me> {
  return apiFetch("/auth/profile", { method: "PATCH", body: JSON.stringify({ name }) })
}

export function changePassword(current_password: string, new_password: string): Promise<{ status: string }> {
  return apiFetch("/auth/password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  })
}

/** Swap the account email (gated on the current password; immediate). */
export function changeEmail(new_email: string, password: string): Promise<Me> {
  return apiFetch("/auth/email", {
    method: "POST",
    body: JSON.stringify({ new_email, password }),
  })
}

/** Reversible self-deactivation: blocks login + ends sessions; an admin re-enables. */
export function deactivateAccount(password: string): Promise<{ status: string }> {
  return apiFetch("/auth/account/deactivate", {
    method: "POST",
    body: JSON.stringify({ password }),
  })
}

/** Permanent self-deletion: record, credentials, and personal resources removed. */
export function deleteAccount(password: string): Promise<{ status: string }> {
  return apiFetch("/auth/account", {
    method: "DELETE",
    body: JSON.stringify({ password }),
  })
}

export function listApiKeys(): Promise<{ keys: ApiKey[] }> {
  return apiFetch("/auth/api-keys")
}

export function createApiKey(name: string): Promise<CreatedApiKey> {
  return apiFetch("/auth/api-keys", { method: "POST", body: JSON.stringify({ name }) })
}

export function revokeApiKey(id: string): Promise<unknown> {
  return apiFetch(`/auth/api-keys/${encodeURIComponent(id)}`, { method: "DELETE" })
}

/**
 * Per-user variables (skills/MCP credentials + config), grouped by the owning
 * skill / MCP server. `status`: "user" = set personally, "tenant" = provided by
 * the org, "unset" = needs setup. `missing_required` powers the Settings badge.
 *
 * `secret` marks sensitive values: those are write-only and never returned. A
 * manifest may declare an input non-secret (`secret: false`), in which case its
 * current `value` is returned and shown/edited as plain text.
 */
export interface SecretInput {
  key: string
  status: "user" | "tenant" | "unset"
  description: string
  required: boolean
  secret: boolean
  value?: string
}

export interface SecretGroup {
  kind: "skill" | "mcp"
  name: string
  inputs: SecretInput[]
  missing: number
}

export interface SecretsView {
  groups: SecretGroup[]
  other: SecretInput[]
  missing_required: number
}

export function listSecrets(): Promise<SecretsView> {
  return apiFetch("/auth/secrets")
}

export function setSecret(key: string, value: string): Promise<unknown> {
  return apiFetch(`/auth/secrets/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  })
}

export function deleteSecret(key: string): Promise<unknown> {
  return apiFetch(`/auth/secrets/${encodeURIComponent(key)}`, { method: "DELETE" })
}

/**
 * Per-user MCP servers & skills (self-service). Unioned with the org's at
 * session time, the user's shadowing the org's by name. Endpoints are only
 * mounted when the backing roots are configured — callers treat a failed
 * GET as "feature unavailable" and hide the section.
 */
export interface UserMcpServer {
  server_name: string
  transport: string
  url: string
  credential_key?: string | null
  scope?: "user" | "tenant"
}

export async function listUserMcpServers(): Promise<UserMcpServer[]> {
  const r = await apiFetch<{ servers: UserMcpServer[] }>("/auth/mcp-servers")
  return r.servers
}

export function putUserMcpServer(s: UserMcpServer): Promise<unknown> {
  const body = { transport: s.transport, url: s.url, credential_key: s.credential_key || null }
  return apiFetch(`/auth/mcp-servers/${encodeURIComponent(s.server_name)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  })
}

export function deleteUserMcpServer(name: string): Promise<unknown> {
  return apiFetch(`/auth/mcp-servers/${encodeURIComponent(name)}`, { method: "DELETE" })
}

export async function listUserSkills(): Promise<string[]> {
  const r = await apiFetch<{ skills: string[] }>("/auth/skills")
  return r.skills
}

export function uploadUserSkill(name: string, zip: Blob): Promise<unknown> {
  return apiFetch(`/auth/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/zip" },
    body: zip,
  })
}

export function deleteUserSkill(name: string): Promise<unknown> {
  return apiFetch(`/auth/skills/${encodeURIComponent(name)}`, { method: "DELETE" })
}
