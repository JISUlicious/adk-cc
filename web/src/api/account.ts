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
