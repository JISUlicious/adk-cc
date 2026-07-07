import { apiFetch } from "./client"

/**
 * Checkpoint / undo for the desktop app. In-place desktop mode edits the user's
 * real files, so the agent snapshots the project tree into a shadow git repo
 * before each mutating turn (see service/desktop_checkpoint.py). These routes
 * (mounted only in desktop mode) list those checkpoints and restore one —
 * "Undo last turn". `userId` is the desktop project id.
 */

export interface Checkpoint {
  /** Unique id for this checkpoint — pass to restore. NOT the git sha, which can
   * repeat across turns (a turn that changes no files reuses the previous commit). */
  id: string
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

/** Restore to a checkpoint by its unique id (omit `id` → undo the last turn).
 * Rolls back both files and conversation; snapshots current state first (the file
 * part is reversible). */
export function restoreCheckpoint(
  projectId: string,
  sessionId: string,
  id?: string,
): Promise<RestoreResult> {
  return apiFetch<RestoreResult>(`/desktop/checkpoint/restore`, {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, session_id: sessionId, id }),
  })
}

/** Relative time for a checkpoint (`ts` is epoch seconds, from the backend). */
export function checkpointAgo(ts: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 45) return "just now"
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

// A checkpoint is the snapshot taken BEFORE the tool that triggered it.
const REASON_LABEL: Record<string, string> = {
  run_bash: "before a command",
  write_file: "before a file write",
  edit_file: "before an edit",
  "pre-restore": "before an undo",
}
/** Friendly label for a checkpoint's trigger. */
export function checkpointReason(r: string): string {
  return REASON_LABEL[r] ?? (r ? `before ${r}` : "checkpoint")
}
