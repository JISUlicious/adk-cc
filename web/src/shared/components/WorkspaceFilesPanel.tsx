import { useCallback, useEffect, useState, type ReactNode } from "react"
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Download,
  File as FileIcon,
  Folder,
  FolderOpen,
  RefreshCw,
} from "lucide-react"
import {
  listDir,
  rawFileUrl,
  readFile,
  type DirEntry,
  type FileContent,
} from "@/shared/api/desktop-files"
import { ArtifactsSidePanel } from "./ArtifactsSidePanel"
import { RightPanelShell, type RightPanelProps } from "./RightPanelShell"
import { SandboxedHtml } from "./SandboxedHtml"
import { CodeView } from "./CodeView"
import { Markdown } from "@/shared/lib/markdown"
import {
  isAudio,
  isHtml,
  isImage,
  isMarkdown,
  isPdf,
  isVideo,
  langFromPath,
} from "@/shared/lib/filetypes"
import { cn } from "@/shared/lib/utils"

/**
 * WEB shell's workspace file browser: a slim lazy tree + the same viewer set
 * as the desktop panel (image/PDF/media/HTML/markdown/code, download for the
 * rest), over /api/files/* scoped to the tenant workspace. Exists because
 * uploads made web workspaces hold files users want to SEE — before this the
 * web shell had no file browser at all. Deliberately minimal: no git
 * markers, checkpoints, or datasets (those stay desktop concerns).
 */
export function WorkspaceFilesPanel({
  userId,
  sessionId,
  open,
  onClose,
  refreshKey,
  headerExtra,
}: RightPanelProps & { headerExtra?: ReactNode }) {
  const [dirs, setDirs] = useState<Record<string, DirEntry[]>>({})
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ "": true })
  const [file, setFile] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const load = useCallback(
    (path: string) => {
      listDir(userId, sessionId, path)
        .then((l) => setDirs((d) => ({ ...d, [path]: l.entries })))
        .catch(() => setDirs((d) => ({ ...d, [path]: [] })))
    },
    [userId, sessionId],
  )

  useEffect(() => {
    // Reload every expanded dir on turn end / manual refresh, so files the
    // agent (or an upload) just produced appear without reselecting.
    for (const p of Object.keys(expanded)) if (expanded[p]) load(p)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, sessionId, refreshKey, tick])

  function renderDir(path: string, depth: number): ReactNode {
    const entries = dirs[path]
    if (!entries) return null
    return entries.map((e) => {
      const full = path ? `${path}/${e.name}` : e.name
      if (e.type === "dir") {
        const isOpen = !!expanded[full]
        return (
          <div key={full}>
            <button
              type="button"
              className="flex w-full items-center gap-1 rounded px-2 py-0.5 text-left text-xs hover:bg-accent"
              style={{ paddingLeft: 8 + depth * 12 }}
              onClick={() => {
                setExpanded((x) => ({ ...x, [full]: !isOpen }))
                if (!isOpen && !dirs[full]) load(full)
              }}
            >
              {isOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              {isOpen ? <FolderOpen className="h-3.5 w-3.5 text-muted-foreground" /> : <Folder className="h-3.5 w-3.5 text-muted-foreground" />}
              <span className="truncate">{e.name}</span>
            </button>
            {isOpen && renderDir(full, depth + 1)}
          </div>
        )
      }
      return (
        <button
          key={full}
          type="button"
          className="flex w-full items-center gap-1 rounded px-2 py-0.5 text-left text-xs hover:bg-accent"
          style={{ paddingLeft: 8 + depth * 12 + 16 }}
          onClick={() => setFile(full)}
        >
          <FileIcon className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="truncate">{e.name}</span>
        </button>
      )
    })
  }

  return (
    <RightPanelShell
      title="Files"
      titleText="Files"
      open={open}
      onClose={onClose}
      headerRight={
        <>
          {headerExtra}
          <button
            type="button"
            onClick={() => setTick((t) => t + 1)}
            className="rounded-md p-1 text-muted-foreground hover:bg-accent"
            title="Refresh"
          >
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </>
      }
    >
      {file ? (
        <WebFileViewer
          userId={userId}
          sessionId={sessionId}
          path={file}
          refreshKey={refreshKey}
          onBack={() => setFile(null)}
        />
      ) : (
        <div className="adk-file-tree py-1">{renderDir("", 0)}</div>
      )}
    </RightPanelShell>
  )
}

function WebFileViewer({
  userId,
  sessionId,
  path,
  refreshKey,
  onBack,
}: {
  userId: string
  sessionId: string
  path: string
  refreshKey?: number
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
    readFile(userId, sessionId, path)
      .then((c) => !cancelled && setContent(c))
      .catch((e) => !cancelled && setError((e as Error).message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [userId, sessionId, path, refreshKey])

  return (
    <div className="flex h-full flex-col">
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
        <a
          href={rawFileUrl(userId, sessionId, path, true)}
          download={name}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent"
          title="Download"
        >
          <Download className="h-3.5 w-3.5" />
        </a>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {loading ? (
          <div className="p-4 text-center text-xs text-muted-foreground">Loading…</div>
        ) : error ? (
          <div className="p-3 text-xs text-destructive">{error}</div>
        ) : isImage(name, content?.mime) ? (
          <div className="flex items-start justify-center p-2">
            <img
              src={rawFileUrl(userId, sessionId, path)}
              alt={name}
              className="max-w-full rounded"
              data-file-viewer="image"
            />
          </div>
        ) : isPdf(name, content?.mime) ? (
          <iframe
            src={rawFileUrl(userId, sessionId, path)}
            title={name}
            className="h-full w-full"
            data-file-viewer="pdf"
          />
        ) : isVideo(name, content?.mime) ? (
          <video
            src={rawFileUrl(userId, sessionId, path)}
            controls
            className="max-w-full p-2"
            data-file-viewer="video"
          />
        ) : isAudio(name, content?.mime) ? (
          <audio
            src={rawFileUrl(userId, sessionId, path)}
            controls
            className="w-full p-3"
            data-file-viewer="audio"
          />
        ) : content?.binary ? (
          <div className="p-4 text-center text-xs text-muted-foreground">
            <div>Binary file ({content.size.toLocaleString()} bytes) — no viewer for this type.</div>
            <a
              href={rawFileUrl(userId, sessionId, path, true)}
              download={name}
              className="mt-2 inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] hover:bg-accent"
            >
              <Download className="h-3.5 w-3.5" /> Download
            </a>
          </div>
        ) : isHtml(name, content?.mime) ? (
          <div className="p-2">
            <SandboxedHtml html={content?.text ?? ""} title={name} />
          </div>
        ) : isMarkdown(name, content?.mime) ? (
          <div className="adk-md p-3 text-[13px] leading-relaxed">
            <Markdown>{content?.text ?? ""}</Markdown>
          </div>
        ) : (
          <CodeView
            code={content?.text ?? ""}
            lang={langFromPath(name)}
            className={cn("whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed")}
          />
        )}
      </div>
    </div>
  )
}

/** Tabbed right panel for the WEB shell: Artifacts | Files. Desktop keeps its
 * own richer FileTreeSidePanel; this exists so web users can SEE the files
 * they upload (and everything else in the tenant workspace). */
export function WebRightPanel(props: RightPanelProps) {
  const [tab, setTab] = useState<"artifacts" | "files">("artifacts")
  const toggle = (
    <div className="mr-1 flex rounded-md border border-border/60 p-0.5">
      {(["artifacts", "files"] as const).map((t) => (
        <button
          key={t}
          type="button"
          data-panel-tab={t}
          onClick={() => setTab(t)}
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-medium capitalize",
            tab === t ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent",
          )}
        >
          {t}
        </button>
      ))}
    </div>
  )
  return tab === "artifacts" ? (
    <ArtifactsSidePanel {...props} headerExtra={toggle} />
  ) : (
    <WorkspaceFilesPanel {...props} headerExtra={toggle} />
  )
}
