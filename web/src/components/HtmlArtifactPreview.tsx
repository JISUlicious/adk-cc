import { useEffect, useRef, useState } from "react"
import { RefreshCw, AlertTriangle } from "lucide-react"
import { fetchArtifactText } from "@/api/artifacts"

/**
 * Renders an HTML artifact inside a STRICTLY sandboxed <iframe srcdoc>.
 *
 * Security model — this is agent/user-generated HTML, i.e. untrusted:
 *   - `sandbox=""` (empty) — the most restrictive setting. No scripts
 *     run (no allow-scripts), the frame is treated as a unique opaque
 *     origin (no allow-same-origin), so it CANNOT reach the parent app's
 *     DOM, cookies, localStorage, or the bearer token. Forms/popups/
 *     top-navigation are all disallowed too. HTML + CSS render; JS is
 *     inert. This is the safe default for rendering arbitrary HTML.
 *   - `srcdoc` (not src) keeps the content inline — no extra authed
 *     fetch from the iframe, and nothing hits the network as the frame's
 *     origin.
 *
 * The iframe auto-grows to its content height via a ResizeObserver on the
 * inner document — but only works while same-origin reads are allowed,
 * which they are NOT under `sandbox=""`. So instead we cap the height and
 * let the frame scroll; the user can open the raw file via the chip's
 * download if they want the full page.
 */
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
    <iframe
      // sandbox="" → no scripts, no same-origin: fully isolated.
      sandbox=""
      srcDoc={html}
      title={`${filename} (preview)`}
      className="w-full h-96 rounded-md border border-border bg-white"
    />
  )
}
