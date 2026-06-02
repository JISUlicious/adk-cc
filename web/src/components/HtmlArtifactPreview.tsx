import { useEffect, useRef, useState } from "react"
import { RefreshCw, AlertTriangle } from "lucide-react"
import { fetchArtifactText } from "@/api/artifacts"

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

  return (
    <div className="space-y-1">
      {ALLOW_SCRIPTS && (
        <p className="text-[10px] text-muted-foreground">
          interactive preview — scripts run in an isolated sandbox (no access
          to your session)
        </p>
      )}
      <iframe
        // SANDBOX: "" (no scripts) by default, "allow-scripts" when the
        // build-time flag is on. allow-same-origin is NEVER set — that
        // combination would let the frame read the parent's token.
        sandbox={SANDBOX}
        srcDoc={html}
        title={`${filename} (preview)`}
        className="w-full h-96 rounded-md border border-border bg-white"
      />
    </div>
  )
}
