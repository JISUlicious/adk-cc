import { useEffect, useState } from "react"
import { ChevronRight, Play, FileWarning } from "lucide-react"
import { downloadArtifact, isHtmlArtifact } from "@/shared/api/artifacts"
import { HtmlArtifactPreview } from "./HtmlArtifactPreview"
import { recentRuns, runLabel, type Run } from "@/shared/lib/runs"
import { cn } from "@/shared/lib/utils"

/**
 * "What did that analysis produce?" — runs, newest first (W6.3).
 *
 * Sits above the file tree because it answers a question the tree cannot: the
 * tree knows `analysis/dashboard.html` exists, not that it came from the turn
 * where you asked about revenue by region. Labels come from the user's own
 * message, so the list reads like the conversation rather than like a folder.
 *
 * `unlinked` counts files sitting in the outputs directory that belong to no
 * run — over the artifact size cap, or written somewhere the artifact plugin's
 * candidate scan never named. Surfaced as a count rather than guessed into a
 * run: the tree API returns no mtime, so any pairing would be invented.
 */
export function RunsSection({
  appName,
  userId,
  sessionId,
  events,
  unlinkedCount = 0,
  onShowFiles,
}: {
  appName: string
  userId: string
  sessionId: string
  events: unknown[]
  unlinkedCount?: number
  onShowFiles?: () => void
}) {
  const runs = recentRuns(events as never[])
  const [open, setOpen] = useState<string | null>(null)

  // Newest run opens itself: after a turn finishes, its outputs are what the
  // user is looking for, and one click to see them is one too many.
  useEffect(() => {
    setOpen(runs[0]?.invocationId ?? null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs[0]?.invocationId, runs[0]?.outputs.length])

  if (runs.length === 0 && unlinkedCount === 0) return null

  return (
    <div className="mb-2 border-b border-border/50 pb-2">
      <div className="flex items-center gap-1.5 px-1">
        <Play className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">Runs</span>
        <span className="text-[10px] text-muted-foreground">
          {runs.length ? `${runs.length} with output` : "none yet"}
        </span>
      </div>

      <div className="mt-1 space-y-0.5 px-1">
        {runs.slice(0, 8).map((r) => (
          <RunRow
            key={r.invocationId}
            run={r}
            expanded={open === r.invocationId}
            onToggle={() => setOpen(open === r.invocationId ? null : r.invocationId)}
            appName={appName}
            userId={userId}
            sessionId={sessionId}
          />
        ))}
        {unlinkedCount > 0 && (
          <button
            type="button"
            onClick={onShowFiles}
            className="flex w-full items-center gap-1.5 rounded px-0.5 text-left text-[10px] text-muted-foreground hover:bg-accent"
            title="These files are in the outputs folder but were not recorded as run outputs"
          >
            <FileWarning className="h-3 w-3 shrink-0" />
            {unlinkedCount} file{unlinkedCount === 1 ? "" : "s"} in analysis/ not linked to a run
          </button>
        )}
      </div>
    </div>
  )
}

function RunRow({
  run, expanded, onToggle, appName, userId, sessionId,
}: {
  run: Run
  expanded: boolean
  onToggle: () => void
  appName: string
  userId: string
  sessionId: string
}) {
  const [preview, setPreview] = useState<string | null>(null)

  return (
    <div>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-1 rounded px-0.5 text-left text-[11px] hover:bg-accent"
      >
        <ChevronRight
          className={cn("h-3 w-3 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-90")}
        />
        <span className="truncate" title={run.prompt || undefined}>{runLabel(run)}</span>
        <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
          {run.outputs.length}
        </span>
      </button>
      {expanded && (
        <div className="ml-4 space-y-0.5 py-0.5">
          {run.outputs.map((o) => (
            <div key={`${o.eventId}:${o.filename}`} className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() =>
                  isHtmlArtifact(o.filename)
                    ? setPreview(preview === o.filename ? null : o.filename)
                    : void downloadArtifact(appName, userId, sessionId, o.filename, o.version)
                }
                className="truncate rounded px-0.5 text-left font-mono text-[10px] hover:bg-accent"
                title={isHtmlArtifact(o.filename) ? "Preview" : "Download"}
              >
                {o.filename}
              </button>
            </div>
          ))}
          {preview && (
            <HtmlArtifactPreview
              appName={appName}
              userId={userId}
              sessionId={sessionId}
              filename={preview}
              version={run.outputs.find((x) => x.filename === preview)?.version}
            />
          )}
        </div>
      )}
    </div>
  )
}
