import { apiFetch } from "./client"
import { IS_DESKTOP } from "@/shared/lib/platform"

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

// One client for both shells: desktop hits /desktop/files/* keyed by
// project_id; web hits /api/files/* keyed by user_id (the auth principal
// wins server-side). The first arg is the scope id — project id on
// desktop, user id on web (ChatPage's userId is both, by construction).
const BASE = IS_DESKTOP ? "/desktop/files" : "/api/files"

function qs(scopeId: string, sessionId: string, path: string): string {
  return new URLSearchParams({
    ...(IS_DESKTOP ? { project_id: scopeId } : { user_id: scopeId }),
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
  return apiFetch<DirListing>(`${BASE}/tree?${qs(projectId, sessionId, path)}`)
}

/** Whole-workspace git working-tree status → change markers on the file tree.
 * One call per reload/turn (git status is repo-wide); the panel looks each
 * entry up in the returned map. `is_repo=false` (empty map) when the workspace
 * root isn't a git work tree. */
export function getFileStatus(
  projectId: string,
  sessionId: string,
): Promise<WorkspaceStatus> {
  if (!IS_DESKTOP) {
    // No status route in web mode (tenant workspaces are rarely git repos);
    // an empty map just means no change markers.
    return Promise.resolve({ is_repo: false, statuses: {} })
  }
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
  return apiFetch<FileContent>(`${BASE}/read?${qs(projectId, sessionId, path)}`)
}

/** URL for the raw-bytes route (real Content-Type; 25 MB cap) — used as
 * <img>/<iframe>/<video> src by the viewers and, with `download`, by the
 * Download button. A plain URL works because desktop is no-auth loopback. */
export function rawFileUrl(
  projectId: string,
  sessionId: string,
  path: string,
  download = false,
): string {
  return `${BASE}/raw?${qs(projectId, sessionId, path)}${download ? "&download=1" : ""}`
}
