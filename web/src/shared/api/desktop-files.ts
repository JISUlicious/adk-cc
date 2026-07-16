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

/** Coarse git working-tree status of a file, for the file-panel change markers. */
export type FileStatus = "new" | "modified" | "deleted" | "renamed"

export interface WorkspaceStatus {
  /** false when the workspace root isn't a git work tree → no markers. */
  is_repo: boolean
  /** workspace-relative path (POSIX) → status; only changed files are present. */
  statuses: Record<string, FileStatus>
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

/** Whole-workspace git working-tree status → change markers on the file tree.
 * One call per reload/turn (git status is repo-wide); the panel looks each
 * entry up in the returned map. `is_repo=false` (empty map) when the workspace
 * root isn't a git work tree. */
export function getFileStatus(
  projectId: string,
  sessionId: string,
): Promise<WorkspaceStatus> {
  return apiFetch<WorkspaceStatus>(
    `/desktop/files/status?${qs(projectId, sessionId, "")}`,
  )
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
