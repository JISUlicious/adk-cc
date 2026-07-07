import { useEffect, useState } from "react"
import { RotateCcw, X } from "lucide-react"
import {
  listCheckpoints,
  restoreCheckpoint,
  checkpointAgo,
  checkpointReason,
  type Checkpoint,
} from "@/shared/api/desktop-checkpoint"

/**
 * Multi-step rewind picker opened by the `/rewind` slash command (desktop). Lists
 * the session's checkpoints (most recent first) and restores the project files to
 * whichever the user picks — rolling back the FILES and the CONVERSATION to that
 * point (later turns are removed). Self-contained (works regardless of the file
 * panel's open/collapsed state); shares the same routes/labels as the panel's
 * History popover.
 */
export function RewindDialog({
  projectId,
  sessionId,
  open,
  onClose,
  onRestored,
}: {
  projectId: string
  sessionId: string
  open: boolean
  onClose: () => void
  /** Called after a successful restore so the caller can refresh the file panel. */
  onRestored: () => void
}) {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    listCheckpoints(projectId, sessionId)
      .then((r) => !cancelled && setCheckpoints(r.checkpoints))
      .catch(() => !cancelled && setCheckpoints([]))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [open, projectId, sessionId])

  if (!open) return null

  async function restore(sha: string, label: string) {
    if (busy) return
    if (!window.confirm(`Rewind to "${label}"? Files AND the conversation roll back to this point; later turns are removed.`)) {
      return
    }
    setBusy(true)
    try {
      await restoreCheckpoint(projectId, sessionId, sha)
      onRestored()
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/30 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="flex max-h-[70vh] w-full max-w-sm flex-col overflow-hidden rounded-lg border border-border bg-popover shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border/60 px-4 py-3">
          <span className="text-sm font-medium">Rewind — restore to a checkpoint</span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent"
            title="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {loading ? (
            <p className="px-4 py-4 text-xs text-muted-foreground">Loading checkpoints…</p>
          ) : checkpoints.length === 0 ? (
            <p className="px-4 py-4 text-xs text-muted-foreground">
              No checkpoints yet — nothing to rewind. A checkpoint is taken before the agent
              changes files; rewinding rolls back both the files and the conversation.
            </p>
          ) : (
            checkpoints.map((cp, i) => {
              const label = i === 0 ? "the last turn" : checkpointReason(cp.reason)
              return (
                <button
                  key={cp.sha}
                  type="button"
                  disabled={busy}
                  onClick={() => void restore(cp.sha, label)}
                  title={`Restore to ${cp.sha.slice(0, 8)}`}
                  className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-accent disabled:opacity-50"
                >
                  <RotateCcw className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="font-medium">{i === 0 ? "Undo the last turn" : checkpointReason(cp.reason)}</span>
                    <span className="ml-1.5 text-xs text-muted-foreground">· {checkpointAgo(cp.ts)}</span>
                  </span>
                </button>
              )
            })
          )}
        </div>
      </div>
    </div>
  )
}
