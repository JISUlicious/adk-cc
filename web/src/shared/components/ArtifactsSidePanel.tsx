import { useCallback, useEffect, useState } from "react"
import { ArrowLeft, Braces, Code2, Download, Eye, FileText, RefreshCw } from "lucide-react"
import {
  listArtifacts,
  listArtifactVersions,
  fetchArtifactText,
  downloadArtifact,
  isHtmlArtifact,
} from "@/shared/api/artifacts"
import { HtmlArtifactPreview } from "./HtmlArtifactPreview"
import { RightPanelShell, type RightPanelProps } from "./RightPanelShell"
import { CodeView } from "@/shared/components/CodeView"
import { Markdown } from "@/shared/lib/markdown"
import { isMarkdown, langFromPath } from "@/shared/lib/filetypes"
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
  refreshKey,
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

  // Clear the viewer only when switching sessions (not on every turn refresh).
  useEffect(() => {
    setSelected(null)
  }, [sessionId])

  // Load on mount, on session change, and after each turn (refreshKey) so a
  // just-produced artifact appears without a manual refresh.
  useEffect(() => {
    if (appName && sessionId) void load()
  }, [appName, sessionId, refreshKey, load])

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
  const md = isMarkdown(filename)
  const [text, setText] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(!html)
  const [formatted, setFormatted] = useState(true)
  const [showSource, setShowSource] = useState(false)
  const canFormat = langFromPath(filename) === "json" // JSON is reformat-on-view today
  // Markdown can be viewed rendered OR as source. (HTML uses a separate preview
  // that doesn't fetch the raw text, so no source toggle for it here.)
  const renderable = md

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
        {renderable && !loading && !error && text != null && (
          <button
            type="button"
            onClick={() => setShowSource((s) => !s)}
            aria-pressed={!showSource}
            className={cn(
              "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium",
              !showSource ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent",
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
        {canFormat && !loading && !error && text != null && (
          <button
            type="button"
            onClick={() => setFormatted((f) => !f)}
            aria-pressed={formatted}
            className={cn(
              "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium",
              formatted ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent",
            )}
            title={formatted ? "Show raw file" : "Pretty-print JSON"}
          >
            <Braces className="h-3.5 w-3.5" />
            {formatted ? "Formatted" : "Raw"}
          </button>
        )}
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
        ) : md && !showSource ? (
          <div className="adk-md p-3 text-[13px] leading-relaxed">
            <Markdown>{text ?? ""}</Markdown>
          </div>
        ) : (
          <CodeView
            code={text ?? ""}
            lang={langFromPath(filename)}
            format={formatted}
            className="whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed"
          />
        )}
      </div>
    </div>
  )
}
