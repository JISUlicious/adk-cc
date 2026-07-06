import { apiFetch } from "./client"

/**
 * Read-only view of a session's in-place workspace (the project root), for the
 * desktop file panel. Backed by /desktop/files/* (mounted only in desktop mode).
 * Both routes are scoped + path-guarded server-side; `userId` here is the
 * desktop project id (ChatPage repurposes userId as the project id in the
 * desktop shell).
 */

export interface DirEntry {
  name: string
  type: "dir" | "file"
  size: number | null
}

export interface DirListing {
  root_exists: boolean
  path: string
  entries: DirEntry[]
  truncated: boolean
}

export interface FileContent {
  path: string
  mime: string
  size: number
  truncated: boolean
  text: string | null
  binary: boolean
}

function qs(projectId: string, sessionId: string, path: string): string {
  return new URLSearchParams({
    project_id: projectId,
    session_id: sessionId,
    path,
  }).toString()
}

/** List one directory of the worktree (path "" = root). Lazy — call again per
 * expanded directory. `root_exists=false` when the session has no worktree yet. */
export function listDir(
  projectId: string,
  sessionId: string,
  path = "",
): Promise<DirListing> {
  return apiFetch<DirListing>(`/desktop/files/tree?${qs(projectId, sessionId, path)}`)
}

/** Read one file (capped at 1 MiB server-side; `binary`/`truncated` flag the
 * fallback cases). */
export function readFile(
  projectId: string,
  sessionId: string,
  path: string,
): Promise<FileContent> {
  return apiFetch<FileContent>(`/desktop/files/read?${qs(projectId, sessionId, path)}`)
}
