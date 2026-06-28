import { apiFetch } from "./client"

/**
 * Desktop projects — each is a local directory the agent works in, and maps to
 * a distinct ADK user_id (its `id`), so sessions + secrets are per-project.
 * Backed by /desktop/projects (mounted only in desktop mode).
 */
export interface Project {
  id: string
  name: string
  repo_path: string
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
