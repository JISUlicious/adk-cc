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
/** Ingest a skill from a LOCAL directory (desktop only) — the server reads the
 * path and copies the folder into the skill store. `name` defaults to the folder. */
export function addDesktopSkillFromDir(
  path: string,
  scope: Scope,
  projectId?: string,
  name?: string,
): Promise<{ status: string; skill_name: string }> {
  return apiFetch(`/desktop/settings/skills/from-dir?${qs(scope, projectId)}`, {
    method: "POST",
    body: JSON.stringify({ path, name }),
  })
}

// ---- working directories (persistent granted dirs, per project) ----
// Directories the desktop agent may read/write in besides the bound project
// (Claude Code's additionalDirectories). Always project-scoped; folded into the
// sandbox scope for every session of the project.
export interface WorkingDirs {
  project_root: string | null
  dirs: string[]
}
export function listWorkingDirs(projectId: string): Promise<WorkingDirs> {
  return apiFetch(`/desktop/working-dirs?${qs("project", projectId)}`)
}
export function addWorkingDir(
  path: string,
  projectId: string,
): Promise<{ status: string; dirs: string[] }> {
  return apiFetch(`/desktop/working-dirs?${qs("project", projectId)}`, {
    method: "POST",
    body: JSON.stringify({ path }),
  })
}
export function removeWorkingDir(
  path: string,
  projectId: string,
): Promise<{ status: string; dirs: string[] }> {
  return apiFetch(`/desktop/working-dirs?${qs("project", projectId)}`, {
    method: "DELETE",
    body: JSON.stringify({ path }),
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
  models?: string[] // full ids this provider offers (discovered)
  reasoning_effort?: string | null
}
export function listDesktopModels(): Promise<{ endpoints: DesktopModel[]; active: string | null }> {
  return apiFetch("/desktop/settings/models")
}
// Set a provider's active model (a full id it offers) and activate the provider.
export function selectModel(name: string, model: string) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}/select-model`, {
    method: "POST",
    body: JSON.stringify({ model }),
  })
}
// Re-discover a provider's models (GET api_base/models).
export function refreshModels(name: string): Promise<DesktopModel> {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}/refresh-models`, { method: "POST" })
}
export function setDesktopModel(m: DesktopModel) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(m.name)}`, {
    method: "PUT",
    body: JSON.stringify({
      model: m.model,
      api_base: m.api_base,
      api_key_env: m.api_key_env,
      max_tokens: m.max_tokens ?? null,
      reasoning_effort: m.reasoning_effort ?? null,
      models: m.models ?? [],
    }),
  })
}
export function activateDesktopModel(name: string) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}/activate`, { method: "POST" })
}
export function deleteDesktopModel(name: string) {
  return apiFetch(`/desktop/settings/models/${encodeURIComponent(name)}`, { method: "DELETE" })
}

// ---- ChatGPT subscription (Codex OAuth) ----
export interface CodexStatus {
  connected: boolean
  plan?: string | null
  account_id_tail?: string | null
  expires_at?: number | null
  expired?: boolean
  registered?: boolean
  active?: boolean
  model?: string | null
  mode?: string | null // "own" (our login) | "cli" (Codex CLI) | "file"
}
export function getCodexStatus(): Promise<CodexStatus> {
  return apiFetch("/desktop/settings/codex")
}
// Omit `model` to let the server default to the first discovered model.
export function connectCodex(model?: string, reasoning_effort = "medium"): Promise<CodexStatus> {
  return apiFetch("/desktop/settings/codex/connect", {
    method: "POST",
    body: JSON.stringify({ ...(model ? { model } : {}), reasoning_effort }),
  })
}
export function disconnectCodex(): Promise<{ status: string }> {
  return apiFetch("/desktop/settings/codex/disconnect", { method: "POST" })
}
export function startCodexLogin(): Promise<{ auth_url: string }> {
  return apiFetch("/desktop/settings/codex/login/start", { method: "POST" })
}
export function getCodexLoginStatus(): Promise<{ state: string; error?: string | null }> {
  return apiFetch("/desktop/settings/codex/login/status")
}
export function codexSignout(): Promise<CodexStatus> {
  return apiFetch("/desktop/settings/codex/signout", { method: "POST" })
}
export function getCodexModels(): Promise<{ models: string[] }> {
  return apiFetch("/desktop/settings/codex/models")
}
// Discover a provider's models via its OpenAI-compatible /models endpoint.
export function discoverModels(api_base: string, api_key_env: string): Promise<{ models: string[] }> {
  return apiFetch("/desktop/settings/models/discover", {
    method: "POST",
    body: JSON.stringify({ api_base, api_key_env }),
  })
}

// ---- container sandbox (desktop-local Docker/Podman) ----
export interface SandboxStatus {
  mode: "host" | "container"
  network: boolean
  image: string
  available: boolean
  runtime: { name: string; version: string } | null
  image_present: boolean
}
export function getSandbox(): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox")
}
export function setSandbox(
  patch: Partial<Pick<SandboxStatus, "mode" | "network" | "image">>,
): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox", { method: "PUT", body: JSON.stringify(patch) })
}
export function pullSandboxImage(): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox/pull", { method: "POST" })
}
