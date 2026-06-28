import { useEffect, useRef, useState } from "react"
import { RefreshCw, AlertTriangle, Maximize2, X } from "lucide-react"
import { fetchArtifactText } from "@/shared/api/artifacts"

/**
 * Renders an HTML artifact inside a sandboxed <iframe srcdoc>.
 *
 * Security model — this is agent/user-generated HTML, i.e. untrusted.
 *
 * DEFAULT (`sandbox=""`): the most restrictive setting. No scripts run, the
 * frame is a unique opaque origin (no allow-same-origin), so it CANNOT reach
 * the parent app's DOM, cookies, localStorage, or the bearer token.
 * Forms/popups/top-navigation are disallowed too. HTML + CSS render; JS is
 * inert. Safe for arbitrary HTML, but JS-built content (Plotly, Chart.js,
 * frameworks) renders BLANK because its drawing code never runs.
 *
 * OPT-IN interactive mode (`VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1`, baked
 * at build time): flips the sandbox to `allow-scripts` so JS-driven artifacts
 * (interactive Plotly etc.) render. Tradeoff: untrusted, model/user-generated
 * JS then EXECUTES in the user's browser and can make network requests (e.g.
 * fetch a CDN, or phone home with anything in the artifact itself). What it
 * still CANNOT do — because we keep `allow-same-origin` HARD-OFF — is read the
 * parent's bearer token / cookies / localStorage / DOM, or navigate the top
 * window. (Verified: allow-scripts WITHOUT allow-same-origin keeps the frame
 * on an opaque origin, so cross-frame access throws SecurityError.) NEVER add
 * `allow-same-origin` here — `allow-scripts` + `allow-same-origin` together is
 * the dangerous combo that would let the frame exfiltrate the token.
 *
 * `srcdoc` (not src) keeps the content inline. Height is capped with internal
 * scroll (auto-grow needs same-origin reads we don't allow).
 */

// Build-time flag (mirrors VITE_ADK_CC_GLOBAL_TENANT in api/admin.ts). Default
// OFF — operators opt in to executing untrusted JS in users' browsers.
const ALLOW_SCRIPTS =
  String(import.meta.env.VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS ?? "") === "1"
// allow-same-origin is intentionally ABSENT — see the security note above.
const SANDBOX = ALLOW_SCRIPTS ? "allow-scripts" : ""
export function HtmlArtifactPreview({
  appName,
  userId,
  sessionId,
  filename,
  version,
}: {
  appName: string
  userId: string
  sessionId: string
  filename: string
  version: number
}) {
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const cancelled = useRef(false)

  useEffect(() => {
    cancelled.current = false
    setHtml(null)
    setError(null)
    fetchArtifactText(appName, userId, sessionId, filename, version)
      .then(({ text }) => {
        if (!cancelled.current) setHtml(text)
      })
      .catch((e) => {
        if (!cancelled.current) setError((e as Error).message)
      })
    return () => {
      cancelled.current = true
    }
  }, [appName, userId, sessionId, filename, version])

  // Escape closes the expanded overlay.
  useEffect(() => {
    if (!expanded) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setExpanded(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [expanded])

  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        preview failed: {error}
      </div>
    )
  }

  if (html === null) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-border bg-card/50 px-3 py-2 text-xs text-muted-foreground">
        <RefreshCw className="h-3.5 w-3.5 shrink-0 animate-spin" />
        rendering {filename}…
      </div>
    )
  }

  // The sandboxed frame, reused inline and in the expanded overlay.
  // SANDBOX: "" (no scripts) by default, "allow-scripts" when the build-time
  // flag is on. allow-same-origin is NEVER set — that combination would let
  // the frame read the parent's token.
  const frame = (heightClass: string) => (
    <iframe
      sandbox={SANDBOX}
      srcDoc={html}
      title={`${filename} (preview)`}
      className={`w-full ${heightClass} rounded-md border border-border bg-white`}
    />
  )

  const scriptNote = ALLOW_SCRIPTS ? (
    <p className="text-[10px] text-muted-foreground">
      interactive preview — scripts run in an isolated sandbox (no access to
      your session)
    </p>
  ) : null

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        {scriptNote ?? <span />}
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
          aria-label={`Expand ${filename} preview`}
        >
          <Maximize2 className="h-3 w-3" />
          expand
        </button>
      </div>
      {/* Inline: taller than before (70vh of the chat area, capped) so charts
          have real room without dominating the page; the frame scrolls
          internally and the expand button gives full height on demand. */}
      {frame("h-[70vh] max-h-[640px] min-h-[320px]")}

      {expanded && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setExpanded(false)}
        >
          <div
            className="flex h-[92vh] w-full max-w-5xl flex-col rounded-lg border border-border bg-background shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b px-4 py-2">
              <span className="truncate font-mono text-xs">{filename}</span>
              <button
                type="button"
                onClick={() => setExpanded(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Close preview"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 p-2">{frame("h-full")}</div>
            {scriptNote && <div className="px-4 pb-2">{scriptNote}</div>}
          </div>
        </div>
      )}
    </div>
  )
}
