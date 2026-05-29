import { useState } from "react"
import { Download, FileDown, RefreshCw } from "lucide-react"
import { downloadArtifact } from "@/api/artifacts"

/**
 * Inline download chip surfaced when an event carries
 * `actions.artifactDelta`. ADK populates that map with
 * `{filename: revision}` whenever `ctx.save_artifact(...)` (or the
 * `save_as_artifact` tool) writes mid-event. Clicking fetches the
 * exact version recorded in the delta and downloads it.
 *
 * Download logic lives in `api/artifacts.ts::downloadArtifact` (shared
 * with the header ArtifactsPanel) — it fetches with the Bearer header,
 * decodes the base64 inline_data, and triggers a blob download (a plain
 * <a download> can't carry auth on a direct navigation).
 */
export function ArtifactChip({
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
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleDownload() {
    setBusy(true)
    setError(null)
    try {
      await downloadArtifact(appName, userId, sessionId, filename, version)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex justify-start">
      <button
        type="button"
        onClick={handleDownload}
        disabled={busy}
        className="max-w-[80%] flex items-center gap-2 rounded-md border border-primary/40 bg-brand-tint px-3 py-2 text-sm hover:bg-brand-tint-strong transition-colors disabled:opacity-50"
      >
        <FileDown className="h-4 w-4 text-primary shrink-0" />
        <span className="font-mono text-xs truncate">{filename}</span>
        <span className="text-[10px] text-muted-foreground shrink-0">
          v{version}
        </span>
        {busy ? (
          <RefreshCw className="h-3.5 w-3.5 text-muted-foreground ml-auto shrink-0 animate-spin" />
        ) : (
          <Download className="h-3.5 w-3.5 text-muted-foreground ml-auto shrink-0" />
        )}
        {error && (
          <span className="text-[10px] text-destructive ml-2 shrink-0">
            {error}
          </span>
        )}
      </button>
    </div>
  )
}
