import { apiFetch } from "./client"

/**
 * Checkpoint / undo for the desktop app. In-place desktop mode edits the user's
 * real files, so the agent snapshots the project tree into a shadow git repo
 * before each mutating turn (see service/desktop_checkpoint.py). These routes
 * (mounted only in desktop mode) list those checkpoints and restore one —
 * "Undo last turn". `userId` is the desktop project id.
 */

export interface Checkpoint {
  sha: string
  reason: string
  ts: number
}

export interface RestoreResult {
  status: "ok" | "no_checkpoints" | "error"
  restored_to?: string
  pre_restore?: string | null
  error?: string
}

/** Most-recent-first checkpoints for the session. */
export function listCheckpoints(
  projectId: string,
  sessionId: string,
): Promise<{ checkpoints: Checkpoint[] }> {
  const q = new URLSearchParams({ project_id: projectId, session_id: sessionId }).toString()
  return apiFetch<{ checkpoints: Checkpoint[] }>(`/desktop/checkpoint/list?${q}`)
}

/** Restore the working tree to a checkpoint (omit `sha` → undo the last turn).
 * Snapshots the current state first, so the restore is itself reversible. */
export function restoreCheckpoint(
  projectId: string,
  sessionId: string,
  sha?: string,
): Promise<RestoreResult> {
  return apiFetch<RestoreResult>(`/desktop/checkpoint/restore`, {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, session_id: sessionId, sha }),
  })
}
