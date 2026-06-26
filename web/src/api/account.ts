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
 * Per-user secrets (skills/MCP credentials), grouped by the owning skill / MCP
 * server. The API returns names + status only — values are write-only and
 * never returned. `status`: "user" = set personally, "tenant" = provided by the
 * org, "unset" = needs setup. `missing_required` powers the Settings badge.
 */
export interface SecretInput {
  key: string
  status: "user" | "tenant" | "unset"
  description: string
  required: boolean
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
