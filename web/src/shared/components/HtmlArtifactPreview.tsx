import { useEffect, useRef, useState } from "react"
import { RefreshCw, AlertTriangle } from "lucide-react"
import { fetchArtifactText } from "@/shared/api/artifacts"
import { SandboxedHtml } from "./SandboxedHtml"

/**
 * Renders an HTML artifact: fetches its text, then hands it to SandboxedHtml
 * (which owns the sandboxed-iframe security model + the expand overlay). This
 * component is just the artifact-fetch wrapper.
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
  /** Optional — omitted fetches the latest version. */
  version?: number
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

  return <SandboxedHtml html={html} title={filename} />
}
