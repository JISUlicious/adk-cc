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
/** Every skill the agent can SEE — built-in, project, installed — with its
 * source and on/off state. `listDesktopSkills` above lists only what is
 * installed into this store, which is a strict subset: built-ins ship in the
 * wheel and can't be uninstalled, so a switch is the only way to stop them. */
export type SkillCatalogEntry = {
  name: string
  description: string
  source: string
  path: string
  enabled: boolean
  disabled_by: "org" | "user" | null
  shadows: { source: string; path: string }[]
}
export function getDesktopSkillCatalog(
  scope: Scope,
  projectId?: string,
): Promise<{ skills: SkillCatalogEntry[] }> {
  return apiFetch(`/desktop/settings/skills/catalog?${qs(scope, projectId)}`)
}
export function setDesktopSkillEnabled(
  name: string,
  enabled: boolean,
  scope: Scope,
  projectId?: string,
) {
  return apiFetch(
    `/desktop/settings/skills/${encodeURIComponent(name)}/enabled?${qs(scope, projectId)}`,
    { method: "PATCH", body: JSON.stringify({ enabled }) },
  )
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
  // The ACTUAL api key ("" = keyless local server). Write-only: list
  // responses never return it — send it only when adding/replacing a key.
  api_key?: string
  api_key_env?: string // legacy env-var indirection (existing endpoints)
  max_tokens?: number | null
  api_key_present?: boolean
  key_source?: "inline" | "env" | "keyless"
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
      // Omit api_key entirely when the caller doesn't provide one — the
      // server then KEEPS the stored key (write-only field). "" = keyless.
      ...(m.api_key !== undefined ? { api_key: m.api_key } : {}),
      api_key_env: m.api_key_env ?? "",
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
// Pass the actual api key ("" probes keyless — local model servers).
export function discoverModels(api_base: string, api_key: string): Promise<{ models: string[] }> {
  return apiFetch("/desktop/settings/models/discover", {
    method: "POST",
    body: JSON.stringify({ api_base, api_key }),
  })
}

// ---- container sandbox (desktop-local Docker/Podman) ----
export interface SandboxStatus {
  mode: "host" | "container"
  network: boolean
  image: string
  require: boolean
  available: boolean
  runtime: { name: string; version: string } | null
  image_present: boolean
  pulling: boolean
  /** Which fields are pinned by an env var (can't be changed from the UI). */
  env_pinned: { mode: boolean; network: boolean; image: boolean; require: boolean }
}
export function getSandbox(): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox")
}
/** Per-session RESOLVED backend — the truth the composer badge shows.
 * `source="live"` once the session ran a turn (the actual backend object);
 * `source="config"` before that (what a new chat would get). Distinct from
 * getSandbox(), which reports the GLOBAL setting and can diverge from a
 * session's reality (per-session overrides, container→host fallback,
 * per-project SSH). */
export interface SessionBackend {
  source: "live" | "config"
  backend: string // "noop" | "container" | "ssh" | "docker" | "daytona" | …
  detail?: string | null // human hint, e.g. the ssh host
  isolated: boolean
  /** container config-mode only: false when no runtime → host fallback. */
  available?: boolean
}
export function getSessionBackend(
  sessionId: string,
  projectId?: string | null,
): Promise<SessionBackend> {
  const p = new URLSearchParams({ session_id: sessionId })
  // project_id lets the config-source prediction know a REMOTE project
  // resolves to ssh before the first turn runs.
  if (projectId) p.set("project_id", projectId)
  return apiFetch(`/desktop/sessions/backend?${p.toString()}`)
}
export function setSandbox(
  patch: Partial<Pick<SandboxStatus, "mode" | "network" | "image">>,
): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox", { method: "PUT", body: JSON.stringify(patch) })
}
export function pullSandboxImage(): Promise<SandboxStatus> {
  return apiFetch("/desktop/settings/sandbox/pull", { method: "POST" })
}

// ---- datasets (W5 ingestion) ----
// Datasets live in `data/` under the session's workspace — the same directory
// the agent reads — so project_id + session_id are required, not optional.
export type Dataset = {
  name: string
  path: string
  bytes: number
  modified: number
  format: string
}
function dsq(projectId: string, sessionId: string) {
  return `project_id=${encodeURIComponent(projectId)}&session_id=${encodeURIComponent(sessionId)}`
}
export function listDatasets(
  projectId: string,
  sessionId: string,
): Promise<{ datasets: Dataset[]; location: string; supported: string[]; max_bytes: number }> {
  return apiFetch(`/desktop/datasets?${dsq(projectId, sessionId)}`)
}
export function addDatasetFromPath(
  path: string,
  projectId: string,
  sessionId: string,
): Promise<{ status: string; dataset: Dataset }> {
  return apiFetch(`/desktop/datasets/from-path?${dsq(projectId, sessionId)}`, {
    method: "POST",
    body: JSON.stringify({ path }),
  })
}
export function deleteDataset(name: string, projectId: string, sessionId: string) {
  return apiFetch(
    `/desktop/datasets/${encodeURIComponent(name)}?${dsq(projectId, sessionId)}`,
    { method: "DELETE" },
  )
}

export type DatasetProfile = {
  rows: number
  rows_exact: boolean
  sampled: number
  bytes: number
  columns: { name: string; dtype: string; nulls: number; null_pct: number }[]
  head: { columns: string[]; rows: string[][] }
  error?: string
}
/** Shape/dtypes/nulls/head, computed in the same analysis env the agent uses. */
export function profileDataset(
  name: string,
  projectId: string,
  sessionId: string,
): Promise<{ status: string; profile: DatasetProfile; cached: boolean }> {
  return apiFetch(
    `/desktop/datasets/${encodeURIComponent(name)}/profile?${dsq(projectId, sessionId)}`,
  )
}
