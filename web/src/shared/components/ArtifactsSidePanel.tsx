import { useCallback, useEffect, useState } from "react"
import { ArrowLeft, Download, FileText, RefreshCw } from "lucide-react"
import {
  listArtifacts,
  listArtifactVersions,
  fetchArtifactText,
  downloadArtifact,
  isHtmlArtifact,
} from "@/shared/api/artifacts"
import { HtmlArtifactPreview } from "./HtmlArtifactPreview"
import { RightPanelShell, type RightPanelProps } from "./RightPanelShell"
import { cn } from "@/shared/lib/utils"

/**
 * Web right-panel: the session's artifacts (published via `save_as_artifact`)
 * as a list, with an inline viewer — HTML in the sandboxed preview iframe,
 * text in a <pre>, anything else via download. This is the web default for
 * ChatPage's `RightPanel` seam; the desktop shell swaps in FileTreeSidePanel.
 * Replaces the old header ArtifactsPanel dropdown.
 */

interface Row {
  filename: string
  latestVersion: number | null
}

interface Selection {
  filename: string
  version: number | null
}

export function ArtifactsSidePanel({
  appName,
  userId,
  sessionId,
  open,
  onClose,
}: RightPanelProps) {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Selection | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const names = await listArtifacts(appName, userId, sessionId)
      setRows(names.map((filename) => ({ filename, latestVersion: null })))
      const withVersions = await Promise.all(
        names.map(async (filename): Promise<Row> => {
          try {
            const versions = await listArtifactVersions(appName, userId, sessionId, filename)
            return { filename, latestVersion: versions.length ? Math.max(...versions) : null }
          } catch {
            return { filename, latestVersion: null }
          }
        }),
      )
      setRows(withVersions)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [appName, userId, sessionId])

  // Load on mount and whenever the session changes; clear any selection.
  useEffect(() => {
    setSelected(null)
    if (appName && sessionId) void load()
  }, [appName, sessionId, load])

  const refresh = (
    <button
      type="button"
      onClick={() => void load()}
      className="rounded-md p-1 text-muted-foreground hover:bg-accent"
      title="Refresh"
    >
      <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
    </button>
  )

  return (
    <RightPanelShell title="Artifacts" open={open} onClose={onClose} headerRight={refresh}>
      {selected ? (
        <ArtifactViewer
          appName={appName}
          userId={userId}
          sessionId={sessionId}
          selection={selected}
          onBack={() => setSelected(null)}
        />
      ) : (
        <div className="adk-artifacts-list p-2">
          {error && <div className="px-2 py-1 text-xs text-destructive">{error}</div>}
          {!error && rows.length === 0 && (
            <div className="px-2 py-6 text-center text-xs text-muted-foreground">
              {loading ? "Loading…" : "No artifacts yet."}
            </div>
          )}
          <ul className="space-y-0.5">
            {rows.map((r) => (
              <li key={r.filename}>
                <button
                  type="button"
                  onClick={() => setSelected({ filename: r.filename, version: r.latestVersion })}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent"
                >
                  <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate">{r.filename}</span>
                  {r.latestVersion !== null && (
                    <span className="shrink-0 text-[10px] text-muted-foreground">v{r.latestVersion}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </RightPanelShell>
  )
}

function ArtifactViewer({
  appName,
  userId,
  sessionId,
  selection,
  onBack,
}: {
  appName: string
  userId: string
  sessionId: string
  selection: Selection
  onBack: () => void
}) {
  const { filename, version } = selection
  const html = isHtmlArtifact(filename)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(!html)

  useEffect(() => {
    if (html) return
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchArtifactText(appName, userId, sessionId, filename, version ?? undefined)
      .then((r) => {
        if (cancelled) return
        // Heuristic: non-text mimes decode to mojibake — offer download instead.
        if (r.mime && !/^text\/|json|xml|javascript|csv/i.test(r.mime)) {
          setError("Binary file — use download.")
        } else {
          setText(r.text)
        }
      })
      .catch((e) => !cancelled && setError((e as Error).message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [appName, userId, sessionId, filename, version, html])

  return (
    <div className="adk-artifact-viewer flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border/60 px-2 py-1.5">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent"
          title="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <span className="min-w-0 flex-1 truncate text-xs font-medium">{filename}</span>
        <button
          type="button"
          onClick={() => void downloadArtifact(appName, userId, sessionId, filename, version ?? undefined)}
          className="rounded-md p-1 text-muted-foreground hover:bg-accent"
          title="Download"
        >
          <Download className="h-4 w-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {html ? (
          <HtmlArtifactPreview
            appName={appName}
            userId={userId}
            sessionId={sessionId}
            filename={filename}
            version={version ?? undefined}
          />
        ) : loading ? (
          <div className="p-4 text-center text-xs text-muted-foreground">Loading…</div>
        ) : error ? (
          <div className="p-4 text-center text-xs text-muted-foreground">{error}</div>
        ) : (
          <pre className="whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed">{text}</pre>
        )}
      </div>
    </div>
  )
}
