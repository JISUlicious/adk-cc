import { apiFetch } from "./client"

/**
 * Desktop projects — each is a local directory the agent works in, and maps to
 * a distinct ADK user_id (its `id`), so sessions + secrets are per-project.
 * Backed by /desktop/projects (mounted only in desktop mode).
 */
export interface Project {
  id: string
  name: string
  /** Local project root; absent for remote (SSH) projects. */
  repo_path?: string
  /** Remote (SSH) binding; absent for local projects. */
  remote?: { host: string; path: string; port?: number } | null
}

export function listProjects(): Promise<{ projects: Project[] }> {
  return apiFetch("/desktop/projects")
}

export function addProject(path: string): Promise<{ project: Project }> {
  return apiFetch("/desktop/projects", {
    method: "POST",
    body: JSON.stringify({ path }),
  })
}

/** Register a remote (SSH) project. `host` is anything your `ssh` accepts
 * (alias / user@host); `path` is the ABSOLUTE workspace root on the remote.
 * Key/agent auth only — set the host up with `ssh <host>` once first. */
export function addRemoteProject(
  host: string,
  path: string,
  port?: number,
): Promise<{ project: Project }> {
  return apiFetch("/desktop/projects/remote", {
    method: "POST",
    body: JSON.stringify({ host, path, ...(port ? { port } : {}) }),
  })
}

export interface RemoteProbe {
  ok: boolean
  error?: string
  home?: string
  git?: boolean
  uname?: string
  path_exists?: boolean
}
/** Probe a remote host over the SAME transport the agent will use. */
export function testRemote(host: string, path?: string, port?: number): Promise<RemoteProbe> {
  return apiFetch("/desktop/projects/test-remote", {
    method: "POST",
    body: JSON.stringify({ host, ...(path ? { path } : {}), ...(port ? { port } : {}) }),
  })
}

export function removeProject(id: string): Promise<unknown> {
  return apiFetch(`/desktop/projects/${encodeURIComponent(id)}`, { method: "DELETE" })
}

/** Remove a session's git worktree (+ its branch). Call on session delete. */
export function removeSessionWorktree(projectId: string, sessionId: string): Promise<unknown> {
  return apiFetch(
    `/desktop/worktree/${encodeURIComponent(projectId)}/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  )
}
