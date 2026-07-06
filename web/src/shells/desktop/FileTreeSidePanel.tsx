import { useCallback, useEffect, useState } from "react"
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  File as FileIcon,
  Folder,
  FolderOpen,
  RefreshCw,
  Undo2,
} from "lucide-react"
import {
  listDir,
  readFile,
  type DirEntry,
  type FileContent,
} from "@/shared/api/desktop-files"
import { listCheckpoints, restoreCheckpoint } from "@/shared/api/desktop-checkpoint"
import { RightPanelShell, type RightPanelProps } from "@/shared/components/RightPanelShell"
import { SandboxedHtml } from "@/shared/components/SandboxedHtml"
import { Markdown } from "@/shared/lib/markdown"
import { isHtml, isMarkdown } from "@/shared/lib/filetypes"
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

export function FileTreeSidePanel({
  userId: projectId,
  sessionId,
  open,
  onClose,
  refreshKey,
}: RightPanelProps) {
  // Loaded directory listings, keyed by relative path ("" = root).
  const [dirs, setDirs] = useState<Record<string, DirEntry[]>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [rootExists, setRootExists] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [canUndo, setCanUndo] = useState(false)
  const [undoing, setUndoing] = useState(false)

  const loadDir = useCallback(
    async (path: string) => {
      const res = await listDir(projectId, sessionId, path)
      setRootExists(res.root_exists)
      setDirs((prev) => ({ ...prev, [path]: res.entries }))
      return res
    },
    [projectId, sessionId],
  )

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    setExpanded(new Set())
    setDirs({})
    try {
      await loadDir("")
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [loadDir])

  // Whether an "Undo last turn" checkpoint exists for this session.
  const refreshUndo = useCallback(async () => {
    if (!projectId || !sessionId) {
      setCanUndo(false)
      return
    }
    try {
      const res = await listCheckpoints(projectId, sessionId)
      setCanUndo(res.checkpoints.length > 0)
    } catch {
      setCanUndo(false) // route only exists in desktop mode; ignore otherwise
    }
  }, [projectId, sessionId])

  async function handleUndo() {
    if (undoing || !projectId || !sessionId) return
    if (!window.confirm("Undo the last turn? Files changed since the previous turn will be reverted (this is itself reversible).")) {
      return
    }
    setUndoing(true)
    try {
      const res = await restoreCheckpoint(projectId, sessionId)
      if (res.status === "error") setError(res.error || "restore failed")
      await reload()
      await refreshUndo()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUndoing(false)
    }
  }

  // (Re)load the root whenever the session changes; clear any open file.
  useEffect(() => {
    setSelectedFile(null)
    if (projectId && sessionId) {
      void reload()
      void refreshUndo()
    }
  }, [projectId, sessionId, reload, refreshUndo])

  // After each turn (refreshKey), re-fetch the currently-loaded directories so
  // agent-created files appear — preserving expansion + the open file. Skips
  // the initial render (no dirs loaded yet; the session effect handles that).
  useEffect(() => {
    // A turn may have added a checkpoint → refresh Undo availability. Deferred
    // to a microtask so the setState isn't synchronous in the effect body.
    void Promise.resolve().then(refreshUndo)
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
    // Intentionally keyed on refreshKey only; reads current `dirs` at that turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

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

  const headerRight = (
    <div className="flex items-center gap-0.5">
      <button
        type="button"
        onClick={() => void handleUndo()}
        disabled={!canUndo || undoing}
        className="rounded-md p-1 text-muted-foreground hover:bg-accent disabled:pointer-events-none disabled:opacity-40"
        title="Undo last turn — revert files to before the last turn"
      >
        <Undo2 className={cn("h-3.5 w-3.5", undoing && "animate-pulse")} />
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

  function renderDir(path: string, depth: number) {
    const entries = dirs[path]
    if (!entries) return null
    return entries.map((e) => {
      const full = join(path, e.name)
      const pad = { paddingLeft: `${depth * 12 + 8}px` }
      if (e.type === "dir") {
        const isOpen = expanded.has(full)
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
            </button>
            {isOpen && renderDir(full, depth + 1)}
          </div>
        )
      }
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
          <span className="min-w-0 flex-1 truncate">{e.name}</span>
        </button>
      )
    })
  }

  return (
    <RightPanelShell title="Files" open={open} onClose={onClose} headerRight={headerRight}>
      {selectedFile ? (
        <FileViewer
          projectId={projectId}
          sessionId={sessionId}
          path={selectedFile}
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
  onBack,
}: {
  projectId: string
  sessionId: string
  path: string
  onBack: () => void
}) {
  const [content, setContent] = useState<FileContent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const name = path.split("/").pop() || path

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
  }, [projectId, sessionId, path])

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
        ) : isHtml(name, content?.mime) ? (
          <div className="p-2">
            <SandboxedHtml html={content?.text ?? ""} title={name} />
          </div>
        ) : isMarkdown(name, content?.mime) ? (
          <>
            <div className="adk-md p-3 text-[13px] leading-relaxed">
              <Markdown>{content?.text ?? ""}</Markdown>
            </div>
            {content?.truncated && <TruncatedNote />}
          </>
        ) : (
          <>
            <pre className="whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed">
              {content?.text}
            </pre>
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
