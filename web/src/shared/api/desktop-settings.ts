import { apiFetch } from "./client"

/**
 * Desktop layered settings — global (shared across all projects) + per-project
 * overrides for MCP servers, skills, and secrets; model endpoints are global.
 * Backed by /desktop/settings/* (mounted only in desktop mode). `scope=project`
 * passes the project id; the agent unions global ∪ project, project winning.
 */
export type Scope = "global" | "project"

function qs(scope: Scope, projectId?: string): string {
  const p = new URLSearchParams({ scope })
  if (scope === "project" && projectId) p.set("project_id", projectId)
  return p.toString()
}

// ---- secrets / variables ----
export function listDesktopSecrets(
  scope: Scope,
  projectId?: string,
): Promise<{ keys: string[]; inherited: string[] }> {
  return apiFetch(`/desktop/settings/secrets?${qs(scope, projectId)}`)
}
export function setDesktopSecret(key: string, value: string, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/secrets/${encodeURIComponent(key)}?${qs(scope, projectId)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  })
}
export function deleteDesktopSecret(key: string, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/secrets/${encodeURIComponent(key)}?${qs(scope, projectId)}`, {
    method: "DELETE",
  })
}

// ---- MCP servers ----
export interface DesktopMcpServer {
  server_name: string
  transport: string
  url: string
  credential_key?: string | null
}
export function listDesktopMcp(scope: Scope, projectId?: string): Promise<{ servers: DesktopMcpServer[] }> {
  return apiFetch(`/desktop/settings/mcp?${qs(scope, projectId)}`)
}
export function setDesktopMcp(s: DesktopMcpServer, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/mcp/${encodeURIComponent(s.server_name)}?${qs(scope, projectId)}`, {
    method: "PUT",
    body: JSON.stringify({ transport: s.transport, url: s.url, credential_key: s.credential_key || null }),
  })
}
export function deleteDesktopMcp(name: string, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/mcp/${encodeURIComponent(name)}?${qs(scope, projectId)}`, {
    method: "DELETE",
  })
}

// ---- skills ----
export function listDesktopSkills(scope: Scope, projectId?: string): Promise<{ skills: string[] }> {
  return apiFetch(`/desktop/settings/skills?${qs(scope, projectId)}`)
}
export function uploadDesktopSkill(name: string, zip: ArrayBuffer, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/skills/${encodeURIComponent(name)}?${qs(scope, projectId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/zip" },
    body: zip,
  })
}
export function deleteDesktopSkill(name: string, scope: Scope, projectId?: string) {
  return apiFetch(`/desktop/settings/skills/${encodeURIComponent(name)}?${qs(scope, projectId)}`, {
    method: "DELETE",
  })
}

// ---- model endpoints (global only) ----
export interface DesktopModel {
  name: string
  model: string
  api_base: string
  api_key_env: string
  max_tokens?: number | null
  api_key_present?: boolean
}
export function listDesktopModels(): Promise<{ endpoints: DesktopModel[]; active: string | null }> {
  return apiFetch("/desktop/settings/models")
}
export function setDesktopModel(m: DesktopModel) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(m.name)}`, {
    method: "PUT",
    body: JSON.stringify({
      model: m.model,
      api_base: m.api_base,
      api_key_env: m.api_key_env,
      max_tokens: m.max_tokens ?? null,
    }),
  })
}
export function activateDesktopModel(name: string) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}/activate`, { method: "POST" })
}
export function deleteDesktopModel(name: string) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}`, { method: "DELETE" })
}
