import { useState } from "react"
import { Download, FileDown } from "lucide-react"
import { getToken } from "@/api/auth"

/**
 * Download chip surfaced when an event carries `actions.artifactDelta`.
 * ADK populates that map with `{filename: revision}` entries whenever
 * `ctx.save_artifact(...)` (or our `save_as_artifact` tool) writes to
 * the artifact service mid-event. The chip resolves the artifact via
 * the standard REST surface ADK exposes:
 *
 *   GET /apps/{app}/users/{user}/sessions/{session}/artifacts/{name}
 *   GET /apps/{app}/users/{user}/sessions/{session}/artifacts/{name}/versions/{v}
 *
 * Because the auth middleware gates that path, we can't just drop a
 * plain `<a download>` link — the browser doesn't send the Bearer
 * header on direct navigations. Instead we fetch through apiFetch-
 * equivalent (manual fetch + Bearer header), turn the response into
 * a blob, and trigger the download via an in-memory object URL.
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
      const headers: Record<string, string> = {}
      const tok = getToken()
      if (tok) headers["Authorization"] = `Bearer ${tok}`

      // We hit the versioned endpoint when version is known; ADK
      // returns the inline_data bytes with the original MIME type.
      const url =
        `/apps/${encodeURIComponent(appName)}` +
        `/users/${encodeURIComponent(userId)}` +
        `/sessions/${encodeURIComponent(sessionId)}` +
        `/artifacts/${encodeURIComponent(filename)}` +
        `/versions/${encodeURIComponent(String(version))}`

      const resp = await fetch(url, { headers })
      if (!resp.ok) {
        throw new Error(`${resp.status} ${resp.statusText}`)
      }

      // ADK serializes types.Part as JSON; the inline_data bytes are
      // base64-encoded under `inline_data.data` (or `inlineData.data`
      // depending on camelCase alias). Either way, decode → blob →
      // object URL → click → revoke.
      const payload = (await resp.json()) as Record<string, unknown>
      const inline =
        (payload.inline_data ?? payload.inlineData) as
          | { data?: string; mime_type?: string; mimeType?: string }
          | undefined
      if (!inline?.data) {
        throw new Error("artifact has no inline_data")
      }
      const mime =
        inline.mime_type || inline.mimeType || "application/octet-stream"
      const bytes = base64ToBytes(inline.data)
      // TS 5.7 narrowed Blob to ArrayBufferView<ArrayBuffer>; our
      // Uint8Array is typed ArrayBufferLike. The runtime accepts it
      // either way — explicit BlobPart cast is the standard escape.
      const blob = new Blob([bytes as BlobPart], { type: mime })
      const objectUrl = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = objectUrl
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(objectUrl)
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
        <Download className="h-3.5 w-3.5 text-muted-foreground ml-auto shrink-0" />
        {error && (
          <span className="text-[10px] text-destructive ml-2 shrink-0">
            {error}
          </span>
        )}
      </button>
    </div>
  )
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}
