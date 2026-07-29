import { useCallback, useEffect, useMemo, useState } from "react"
import {
  ArrowLeft,
  Braces,
  ChevronDown,
  ChevronRight,
  Code2,
  Eye,
  File as FileIcon,
  Folder,
  FolderOpen,
  History,
  RefreshCw,
  RotateCcw,
  Undo2,
} from "lucide-react"
import {
  getFileStatus,
  listDir,
  readFile,
  type DirEntry,
  type FileContent,
  type FileStatus,
} from "@/shared/api/desktop-files"
import {
  listCheckpoints,
  restoreCheckpoint,
  checkpointAgo,
  checkpointReason,
  type Checkpoint,
} from "@/shared/api/desktop-checkpoint"
import { RightPanelShell, type RightPanelProps } from "@/shared/components/RightPanelShell"
import { SandboxedHtml } from "@/shared/components/SandboxedHtml"
import { Database } from "lucide-react"
import { listProjects } from "@/shared/api/projects"
import { RunsSection } from "@/shared/components/RunsSection"
import { collectRuns } from "@/shared/lib/runs"
import { pickFile } from "@/shared/lib/tauri"
import {
  listDatasets, addDatasetFromPath, profileDataset,
  type Dataset, type DatasetProfile,
} from "@/shared/api/desktop-settings"
import { CodeView } from "@/shared/components/CodeView"
import { Markdown } from "@/shared/lib/markdown"
import { isHtml, isMarkdown, langFromPath } from "@/shared/lib/filetypes"
import { cn } from "@/shared/lib/utils"

/**
 * Desktop right-panel: a lazy file tree of the session's in-place workspace (the
 * project root) with an inline file viewer, plus an "Undo last turn" control
 * that reverts the project to the checkpoint taken before the last turn.
 * Injected into ChatPage via the `RightPanel` seam by DesktopApp (replacing the
 * web ArtifactsSidePanel). `userId` is the desktop project id. Read-only view.
 */

function join(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name
}

// Git-style change markers: a single letter + color per status, mirroring how
// editors annotate uncommitted files. Text color also tints the filename.
const STATUS_META: Record<FileStatus, { letter: string; label: string; className: string }> = {
  new: { letter: "A", label: "Created", className: "text-emerald-600 dark:text-emerald-400" },
  modified: { letter: "M", label: "Modified", className: "text-amber-600 dark:text-amber-400" },
  renamed: { letter: "R", label: "Renamed", className: "text-sky-600 dark:text-sky-400" },
  deleted: { letter: "D", label: "Deleted", className: "text-rose-600 dark:text-rose-400" },
}

/** The single-letter badge shown at the right edge of a changed file row. */
function StatusBadge({ status }: { status: FileStatus }) {
  const m = STATUS_META[status]
  return (
    <span
      className={cn("ml-1 w-3 shrink-0 text-center text-[10px] font-bold leading-none", m.className)}
      title={m.label}
      aria-label={m.label}
    >
      {m.letter}
    </span>
  )
}


/** Keep BOTH ends of a path readable in a narrow header.
 *
 * `dir="rtl"` is the usual trick for tail-visible truncation, but it reorders
 * the leading separator and still clipped the tail here — so shorten
 * deterministically instead. The full path stays in the tooltip. */
function middleEllipsis(path: string, max = 46): string {
  if (path.length <= max) return path
  const tail = Math.max(12, Math.floor(max * 0.6))
  return `${path.slice(0, max - tail - 1)}…${path.slice(-tail)}`
}

export function FileTreeSidePanel({
  appName,
  userId: projectId,
  sessionId,
  open,
  onClose,
  refreshKey,
  onRestored,
  events,
}: RightPanelProps) {
  // Loaded directory listings, keyed by relative path ("" = root).
  const [dirs, setDirs] = useState<Record<string, DirEntry[]>>({})
  // Git working-tree status per changed file (workspace-relative path → status).
  const [statuses, setStatuses] = useState<Record<string, FileStatus>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [rootExists, setRootExists] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  // Datasets (W5): the agent can only analyse what is IN the workspace, so the
  // panel that shows the workspace is also where you put data into it.
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [dsBusy, setDsBusy] = useState(false)
  const [dsError, setDsError] = useState<string | null>(null)
  // Profile of the dataset the user opened: shape, dtypes, nulls, head — the
  // things an analyst checks before asking anything, so they cost no turn.
  const [dsOpen, setDsOpen] = useState<string | null>(null)
  const [dsProfile, setDsProfile] = useState<DatasetProfile | null>(null)
  const [dsProfiling, setDsProfiling] = useState(false)
  // The workspace root, shown ONCE beside the panel title: every path in the
  // tree is relative to it, and "which directory am I actually in" was
  // otherwise unanswerable from this panel.
  const [projectPath, setProjectPath] = useState<string | null>(null)
  // Files sitting in analysis/ that belong to no run — over the artifact size
  // cap, or written where the artifact plugin's candidate scan never looked.
  // Counted by NAME set-difference: the tree API returns no mtime, so pairing
  // them to a run by time would be invention, not evidence.
  const [unlinkedOutputs, setUnlinkedOutputs] = useState(0)
  const [canUndo, setCanUndo] = useState(false)
  // Non-null when checkpointing can't work for this project (remote device
  // without git) — shown as the Undo tooltip so the dead button explains itself.
  const [undoUnavailable, setUndoUnavailable] = useState<string | null>(null)
  const [undoing, setUndoing] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([])

  const loadDir = useCallback(
    async (path: string) => {
      const res = await listDir(projectId, sessionId, path)
      setRootExists(res.root_exists)
      setDirs((prev) => ({ ...prev, [path]: res.entries }))
      return res
    },
    [projectId, sessionId],
  )

  // Refresh the whole-workspace git status map (change markers). Best-effort:
  // the route only exists in desktop mode and returns empty for a non-repo, so
  // any failure just clears the markers rather than surfacing an error.
  const loadStatus = useCallback(async () => {
    if (!projectId || !sessionId) return
    try {
      const res = await getFileStatus(projectId, sessionId)
      setStatuses(res.statuses)
    } catch {
      setStatuses({})
    }
  }, [projectId, sessionId])

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    setExpanded(new Set())
    setDirs({})
    try {
      await loadDir("")
      await loadStatus()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [loadDir, loadStatus])

  // Whether an "Undo last turn" checkpoint exists for this session.
  const refreshUndo = useCallback(async () => {
    if (!projectId || !sessionId) {
      setCanUndo(false)
      return
    }
    try {
      const res = await listCheckpoints(projectId, sessionId)
      setCanUndo(res.checkpoints.length > 0)
      setUndoUnavailable(res.supported === false ? (res.reason ?? "undo unavailable") : null)
    } catch {
      setCanUndo(false) // route only exists in desktop mode; ignore otherwise
    }
  }, [projectId, sessionId])

  const loadCheckpoints = useCallback(async () => {
    if (!projectId || !sessionId) return
    try {
      const res = await listCheckpoints(projectId, sessionId)
      setCheckpoints(res.checkpoints)
      setCanUndo(res.checkpoints.length > 0)
    } catch {
      setCheckpoints([])
    }
  }, [projectId, sessionId])

  // Restore the project to a checkpoint. `id` omitted → undo the last turn.
  async function performRestore(id?: string) {
    if (undoing || !projectId || !sessionId) return
    const msg = id
      ? "Rewind to this checkpoint? Files AND the conversation roll back to this point; later turns are removed."
      : "Undo the last turn? Files and the conversation roll back to before the last turn; the turn's messages are removed."
    if (!window.confirm(msg)) return
    setUndoing(true)
    try {
      const res = await restoreCheckpoint(projectId, sessionId, id)
      if (res.status === "error") setError(res.error || "restore failed")
      await reload()
      await loadCheckpoints()
      setHistoryOpen(false)
      onRestored?.() // reload the thread — a rewind rolls back the conversation too
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUndoing(false)
    }
  }

  function toggleHistory() {
    setHistoryOpen((v) => {
      const next = !v
      if (next) void loadCheckpoints()
      return next
    })
  }

  // (Re)load the root whenever the session changes; clear any open file.
  useEffect(() => {
    setSelectedFile(null)
    if (projectId && sessionId) {
      void reload()
      void refreshUndo()
    }
  }, [projectId, sessionId, reload, refreshUndo])

  // LIVE refresh: while the panel is open, tick every few seconds so file
  // changes show as they happen (mid-turn agent writes, external edits) —
  // not only at turn end. Each tick re-runs the same loaded-dirs refresh
  // below and re-reads the open file; expansion and selection are preserved.
  // Cheap: one listDir per loaded dir + one status call (remote projects
  // ride the shared ControlMaster at ~25ms/op).
  const [liveTick, setLiveTick] = useState(0)
  useEffect(() => {
    if (!open) return
    const id = setInterval(() => setLiveTick((t) => t + 1), 4000)
    return () => clearInterval(id)
  }, [open])

  // After each turn (refreshKey) and on every live tick, re-fetch the
  // currently-loaded directories so agent-created files appear — preserving
  // expansion + the open file. Skips the initial render (no dirs loaded yet;
  // the session effect handles that).
  useEffect(() => {
    // A turn may have added a checkpoint → refresh Undo availability. Deferred
    // to a microtask so the setState isn't synchronous in the effect body.
    void Promise.resolve().then(refreshUndo)
    // A turn likely changed files → refresh the git-status change markers.
    // Deferred to a microtask (same as refreshUndo) so the effect body has no
    // synchronous setState.
    void Promise.resolve().then(loadStatus)
    const loaded = Object.keys(dirs)
    if (loaded.length === 0) return
    let cancelled = false
    Promise.all(
      loaded.map((pth) =>
        listDir(projectId, sessionId, pth)
          .then((r) => [pth, r.entries] as const)
          .catch(() => null),
      ),
    ).then((results) => {
      if (cancelled) return
      setDirs((prev) => {
        const next = { ...prev }
        for (const r of results) if (r) next[r[0]] = r[1]
        return next
      })
    })
    return () => {
      cancelled = true
    }
    // Intentionally keyed on refreshKey + liveTick only; reads current `dirs`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, liveTick])

  async function toggleDir(path: string) {
    const next = new Set(expanded)
    if (next.has(path)) {
      next.delete(path)
    } else {
      next.add(path)
      if (!dirs[path]) {
        try {
          await loadDir(path)
        } catch (e) {
          setError((e as Error).message)
        }
      }
    }
    setExpanded(next)
  }

  useEffect(() => {
    let live = true
    listProjects()
      .then((r) => {
        if (!live) return
        const p = r.projects.find((x) => x.id === projectId)
        setProjectPath(
          p ? (p.remote ? `${p.remote.host}:${p.remote.path}` : p.repo_path ?? null) : null,
        )
      })
      .catch(() => setProjectPath(null))
    return () => { live = false }
  }, [projectId])

  useEffect(() => {
    if (!projectId || !sessionId) return
    const produced = new Set(
      collectRuns((events ?? []) as never[]).flatMap((r) => r.outputs.map((o) => o.filename)),
    )
    listDir(projectId, sessionId, "analysis")
      .then((r) => {
        const files = (r.entries ?? []).filter((e) => e.type === "file")
        setUnlinkedOutputs(files.filter((f) => !produced.has(f.name)).length)
      })
      .catch(() => setUnlinkedOutputs(0))   // no analysis/ dir yet — nothing to say
  }, [projectId, sessionId, events, refreshKey])

  const reloadDatasets = useCallback(() => {
    if (!projectId || !sessionId) return
    listDatasets(projectId, sessionId)
      .then((r) => setDatasets(r.datasets))
      .catch(() => setDatasets([]))   // no workspace bound yet — not an error
  }, [projectId, sessionId])
  useEffect(reloadDatasets, [reloadDatasets, refreshKey])

  async function openDataset(name: string) {
    if (dsOpen === name) { setDsOpen(null); setDsProfile(null); return }
    setDsOpen(name); setDsProfile(null); setDsError(null); setDsProfiling(true)
    try {
      const r = await profileDataset(name, projectId, sessionId)
      setDsProfile(r.profile)
    } catch (e) {
      // The first profile provisions the analysis runtime; say so rather than
      // showing a bare failure.
      setDsError((e as Error).message)
    } finally {
      setDsProfiling(false)
    }
  }

  async function addDataset() {
    setDsError(null)
    const picked = await pickFile(["csv", "tsv", "parquet", "xlsx", "json", "jsonl"])
    if (picked === null) return                     // cancelled
    const path = picked ?? window.prompt("Path to the data file")  // no native IPC
    if (!path) return
    setDsBusy(true)
    try {
      await addDatasetFromPath(path, projectId, sessionId)
      reloadDatasets()
      void reload()
    } catch (e) {
      setDsError((e as Error).message)
    } finally {
      setDsBusy(false)
    }
  }

  const headerRight = (
    <div className="flex items-center gap-0.5">
      {/* span carries the tooltip — a disabled button has pointer-events off */}
      <span title={undoUnavailable ?? "Undo last turn — revert files to before the last turn"}>
        <button
          type="button"
          onClick={() => void performRestore()}
          disabled={!canUndo || undoing || undoUnavailable !== null}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent disabled:pointer-events-none disabled:opacity-40"
        >
          <Undo2 className={cn("h-3.5 w-3.5", undoing && "animate-pulse")} />
        </button>
      </span>
      <button
        type="button"
        onClick={toggleHistory}
        disabled={!canUndo}
        className={cn(
          "rounded-md p-1 text-muted-foreground hover:bg-accent disabled:pointer-events-none disabled:opacity-40",
          historyOpen && "bg-accent text-foreground",
        )}
        title="Checkpoint history — restore to an earlier turn"
      >
        <History className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        onClick={() => void reload()}
        className="rounded-md p-1 text-muted-foreground hover:bg-accent"
        title="Refresh"
      >
        <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
      </button>
    </div>
  )

  // Directories that contain a changed descendant, so a collapsed folder can
  // show a change dot. Built from the status map by adding every ancestor
  // prefix of each changed path ("a/b/c.txt" → "a", "a/b").
  const changedDirs = useMemo(() => {
    const set = new Set<string>()
    for (const p of Object.keys(statuses)) {
      let i = p.indexOf("/")
      while (i !== -1) {
        set.add(p.slice(0, i))
        i = p.indexOf("/", i + 1)
      }
    }
    return set
  }, [statuses])

  function renderDir(path: string, depth: number) {
    const entries = dirs[path]
    if (!entries) return null
    return entries.map((e) => {
      const full = join(path, e.name)
      const pad = { paddingLeft: `${depth * 12 + 8}px` }
      if (e.type === "dir") {
        const isOpen = expanded.has(full)
        // Show the change dot only while collapsed — an open folder's own
        // rows carry their markers, so a dot there would be redundant noise.
        const dirChanged = !isOpen && changedDirs.has(full)
        return (
          <div key={full}>
            <button
              type="button"
              style={pad}
              onClick={() => void toggleDir(full)}
              className="flex w-full items-center gap-1 py-1 pr-2 text-left text-xs hover:bg-accent"
            >
              {isOpen ? (
                <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
              )}
              {isOpen ? (
                <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1 truncate">{e.name}</span>
              {dirChanged && (
                <span
                  className="ml-1 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500/80"
                  title="Contains changes"
                  aria-label="Contains changes"
                />
              )}
            </button>
            {isOpen && renderDir(full, depth + 1)}
          </div>
        )
      }
      const status = statuses[full]
      return (
        <button
          key={full}
          type="button"
          style={pad}
          onClick={() => setSelectedFile(full)}
          className={cn(
            "flex w-full items-center gap-1 py-1 pr-2 text-left text-xs hover:bg-accent",
            selectedFile === full && "bg-accent",
          )}
        >
          <span className="w-3 shrink-0" />
          <FileIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span
            className={cn(
              "min-w-0 flex-1 truncate",
              status && STATUS_META[status].className,
            )}
          >
            {e.name}
          </span>
          {status && <StatusBadge status={status} />}
        </button>
      )
    })
  }

  return (
    <RightPanelShell
      title={
        <span className="flex min-w-0 items-baseline gap-1.5">
          Files
          {projectPath && (
            <span
              className="min-w-0 truncate text-[10px] font-normal text-muted-foreground"
              title={projectPath}
            >
              {middleEllipsis(projectPath)}
            </span>
          )}
        </span>
      }
      titleText="Files"
      open={open}
      onClose={onClose}
      headerRight={headerRight}
    >
      <RunsSection
        appName={appName}
        userId={projectId}
        sessionId={sessionId}
        events={events ?? []}
        unlinkedCount={unlinkedOutputs}
      />
      <div className="mb-2 border-b border-border/50 pb-2">
        <div className="flex items-center gap-1.5 px-1">
          <Database className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-xs font-medium">Datasets</span>
          <span className="text-[10px] text-muted-foreground">
            {datasets.length ? `${datasets.length} in data/` : "none in data/"}
          </span>
          <button
            type="button"
            onClick={() => void addDataset()}
            disabled={dsBusy}
            title="Copy a data file into this project's data/ folder"
            className="ml-auto rounded-md px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent disabled:opacity-40"
          >
            {dsBusy ? "Adding…" : "+ Add"}
          </button>
        </div>
        {datasets.length > 0 && (
          <div className="mt-1 space-y-0.5 px-1">
            {datasets.slice(0, 6).map((d) => (
              <div key={d.name}>
                <button
                  type="button"
                  onClick={() => void openDataset(d.name)}
                  className="flex w-full items-center gap-2 rounded px-0.5 text-left text-[11px] hover:bg-accent"
                  title="Show shape, dtypes, nulls and head"
                >
                  <span className="truncate font-mono">{d.name}</span>
                  <span className="ml-auto shrink-0 text-muted-foreground">
                    {d.bytes > 1024 * 1024
                      ? `${(d.bytes / 1024 / 1024).toFixed(1)}MB`
                      : `${Math.max(1, Math.round(d.bytes / 1024))}KB`}
                  </span>
                </button>
                {dsOpen === d.name && (
                  <div className="mb-1 mt-1 min-w-0 rounded-md border border-border/60 bg-card/40 p-1.5">
                    {dsProfiling && (
                      <p className="text-[10px] text-muted-foreground">
                        profiling… (the first one provisions the analysis runtime)
                      </p>
                    )}
                    {dsProfile && !dsProfile.error && (
                      <div className="min-w-0 space-y-1">
                        <p className="text-[10px] font-medium">
                          {dsProfile.rows.toLocaleString()}
                          {dsProfile.rows_exact ? "" : "+"}{" "}
                          {dsProfile.rows === 1 ? "row" : "rows"} ×{" "}
                          {dsProfile.columns.length}{" "}
                          {dsProfile.columns.length === 1 ? "col" : "cols"}
                          <span className="ml-1 font-normal text-muted-foreground">
                            (dtypes/nulls from{" "}
                            {dsProfile.sampled === dsProfile.rows
                              ? "all rows"
                              : `a ${dsProfile.sampled.toLocaleString()}-row sample`})
                          </span>
                        </p>
                        {/* Bordered scroll boxes: a bare max-height clips the
                            last row mid-glyph and reads as a broken layout
                            rather than "there is more, scroll". */}
                        <div className="max-h-32 overflow-y-auto rounded border border-border/50 p-1">
                          {dsProfile.columns.map((c) => (
                            <div key={c.name} className="flex items-center gap-1.5 text-[10px]">
                              <span className="truncate font-mono">{c.name}</span>
                              <span className="text-muted-foreground">{c.dtype}</span>
                              {c.nulls > 0 && (
                                <span className="ml-auto text-amber-600 dark:text-amber-500">
                                  {c.null_pct}% null
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                        {/* min-w-0 + w-full on the wrapper: without it the table
                            sizes to its content and pushes past the panel edge
                            instead of scrolling inside it (a flex child's
                            default min-width is `auto`). */}
                        {dsProfile.head.rows.length > 0 && (
                          <div className="w-full min-w-0 overflow-x-auto rounded border border-border/50">
                            <table className="text-[9px]">
                              <thead>
                                <tr className="text-muted-foreground">
                                  {dsProfile.head.columns.map((c) => (
                                    <th key={c} className="px-1 text-left font-medium">{c}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {dsProfile.head.rows.slice(0, 5).map((r, i) => (
                                  <tr key={i} className="border-t border-border/40">
                                    {r.map((v, j) => (
                                      <td key={j} className="max-w-[9rem] truncate px-1 font-mono">{v}</td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </div>
                    )}
                    {dsProfile?.error && (
                      <p className="text-[10px] text-destructive">{dsProfile.error}</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        {dsError && <p className="px-1 text-[11px] text-destructive">{dsError}</p>}
      </div>
      {historyOpen && (
        <>
          {/* click-away backdrop */}
          <div className="fixed inset-0 z-20" aria-hidden onClick={() => setHistoryOpen(false)} />
          <div className="absolute right-2 top-12 z-30 flex max-h-[65%] w-64 flex-col overflow-hidden rounded-md border border-border bg-popover shadow-lg">
            <div className="flex items-center justify-between border-b border-border/60 px-3 py-2">
              <span className="text-xs font-medium">Restore to a checkpoint</span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {checkpoints.length === 0 ? (
                <p className="px-3 py-3 text-xs text-muted-foreground">No checkpoints yet.</p>
              ) : (
                checkpoints.map((cp) => (
                  <button
                    key={cp.id}
                    type="button"
                    onClick={() => void performRestore(cp.id)}
                    disabled={undoing}
                    title={`Restore to ${cp.sha.slice(0, 8)}`}
                    className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-accent disabled:opacity-50"
                  >
                    <RotateCcw className="h-3 w-3 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">
                      <span className="font-medium">{checkpointReason(cp.reason)}</span>
                      <span className="ml-1 text-muted-foreground">· {checkpointAgo(cp.ts)}</span>
                    </span>
                  </button>
                ))
              )}
            </div>
          </div>
        </>
      )}
      {selectedFile ? (
        <FileViewer
          projectId={projectId}
          sessionId={sessionId}
          path={selectedFile}
          refreshKey={(refreshKey ?? 0) + liveTick}
          onBack={() => setSelectedFile(null)}
        />
      ) : !rootExists ? (
        <div className="p-4 text-center text-xs text-muted-foreground">
          Workspace initializes on the first message.
        </div>
      ) : error ? (
        <div className="p-3 text-xs text-destructive">{error}</div>
      ) : (
        <div className="adk-file-tree py-1">{renderDir("", 0)}</div>
      )}
    </RightPanelShell>
  )
}

function FileViewer({
  projectId,
  sessionId,
  path,
  refreshKey,
  onBack,
}: {
  projectId: string
  sessionId: string
  path: string
  /** Bumped after each turn (and on the final response) → re-read the open file
   * so an agent edit to it shows without the user reselecting. */
  refreshKey?: number
  onBack: () => void
}) {
  const [content, setContent] = useState<FileContent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [formatted, setFormatted] = useState(true)
  const [showSource, setShowSource] = useState(false)
  const name = path.split("/").pop() || path
  // Only JSON is reformat-on-view today; the toggle shows for those files.
  const canFormat = langFromPath(name) === "json"
  // Renderable files (markdown / HTML) can be viewed rendered OR as source.
  const renderable = isMarkdown(name, content?.mime) || isHtml(name, content?.mime)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    readFile(projectId, sessionId, path)
      .then((c) => !cancelled && setContent(c))
      .catch((e) => !cancelled && setError((e as Error).message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [projectId, sessionId, path, refreshKey])

  return (
    <div className="adk-file-viewer flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border/60 px-2 py-1.5">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent"
          title="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <span className="min-w-0 flex-1 truncate text-xs font-medium">{name}</span>
        {renderable && !loading && !content?.binary && (
          <button
            type="button"
            onClick={() => setShowSource((s) => !s)}
            aria-pressed={!showSource}
            className={cn(
              "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium",
              !showSource
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-accent",
            )}
            title={showSource ? "Show rendered" : "Show source"}
          >
            {showSource ? (
              <>
                <Code2 className="h-3.5 w-3.5" /> Code
              </>
            ) : (
              <>
                <Eye className="h-3.5 w-3.5" /> Preview
              </>
            )}
          </button>
        )}
        {canFormat && !loading && !content?.binary && (
          <button
            type="button"
            onClick={() => setFormatted((f) => !f)}
            aria-pressed={formatted}
            className={cn(
              "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium",
              formatted
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-accent",
            )}
            title={formatted ? "Show raw file" : "Pretty-print JSON"}
          >
            <Braces className="h-3.5 w-3.5" />
            {formatted ? "Formatted" : "Raw"}
          </button>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {loading ? (
          <div className="p-4 text-center text-xs text-muted-foreground">Loading…</div>
        ) : error ? (
          <div className="p-3 text-xs text-destructive">{error}</div>
        ) : content?.binary ? (
          <div className="p-4 text-center text-xs text-muted-foreground">
            Binary file ({content.size.toLocaleString()} bytes) — not shown.
          </div>
        ) : isHtml(name, content?.mime) && !showSource ? (
          <div className="p-2">
            <SandboxedHtml html={content?.text ?? ""} title={name} />
          </div>
        ) : isMarkdown(name, content?.mime) && !showSource ? (
          <>
            <div className="adk-md p-3 text-[13px] leading-relaxed">
              <Markdown>{content?.text ?? ""}</Markdown>
            </div>
            {content?.truncated && <TruncatedNote />}
          </>
        ) : (
          <>
            <CodeView
              code={content?.text ?? ""}
              lang={langFromPath(name)}
              format={formatted}
              className="whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed"
            />
            {content?.truncated && <TruncatedNote />}
          </>
        )}
      </div>
    </div>
  )
}

function TruncatedNote() {
  return (
    <div className="border-t border-border/60 px-3 py-1 text-[10px] text-muted-foreground">
      Truncated at 1 MiB.
    </div>
  )
}
