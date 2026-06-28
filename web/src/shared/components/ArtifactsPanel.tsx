import { useEffect, useRef, useState } from "react"
import { Paperclip, Download, ChevronDown, RefreshCw, Upload } from "lucide-react"
import { Button } from "./ui/button"
import {
  listArtifacts,
  listArtifactVersions,
  downloadArtifact,
  uploadArtifact,
} from "@/shared/api/artifacts"
import { cn } from "@/shared/lib/utils"

interface ArtifactRow {
  filename: string
  latestVersion: number | null
}

/**
 * Header dropdown listing every artifact published in the current
 * session (via `save_as_artifact`), each with a download action.
 * Complements the inline ArtifactChip — this surface is discoverable
 * without scrolling back through the thread.
 *
 * Opens on click; loads the artifact list lazily on first open and
 * on demand via the refresh control. Closes on outside-click / Escape.
 */
export function ArtifactsPanel({
  appName,
  userId,
  sessionId,
}: {
  appName: string
  userId: string
  sessionId: string
}) {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState<ArtifactRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const names = await listArtifacts(appName, userId, sessionId)
      // Show filenames immediately; resolve latest version per file in
      // parallel (best-effort — a failed version lookup just hides the
      // version chip for that row, download still works via "latest").
      setRows(names.map((filename) => ({ filename, latestVersion: null })))
      const withVersions = await Promise.all(
        names.map(async (filename): Promise<ArtifactRow> => {
          try {
            const versions = await listArtifactVersions(
              appName,
              userId,
              sessionId,
              filename,
            )
            const latest = versions.length ? Math.max(...versions) : null
            return { filename, latestVersion: latest }
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
  }

  // Load on first open; reload whenever the session changes while open.
  useEffect(() => {
    if (open) void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sessionId])

  // Outside-click + Escape to close.
  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false)
    }
    window.addEventListener("mousedown", onDown)
    window.addEventListener("keydown", onKey)
    return () => {
      window.removeEventListener("mousedown", onDown)
      window.removeEventListener("keydown", onKey)
    }
  }, [open])

  async function handleDownload(row: ArtifactRow) {
    setDownloading(row.filename)
    setError(null)
    try {
      await downloadArtifact(appName, userId, sessionId, row.filename)
    } catch (e) {
      setError(`${row.filename}: ${(e as Error).message}`)
    } finally {
      setDownloading(null)
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    // Reset the input so selecting the same file again re-fires onChange.
    e.target.value = ""
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      await uploadArtifact(appName, userId, sessionId, file)
      await load() // refresh the list so the new artifact appears
    } catch (err) {
      setError(`upload ${file.name}: ${(err as Error).message}`)
    } finally {
      setUploading(false)
    }
  }

  return (
    <div ref={wrapRef} className="relative">
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen((o) => !o)}
        title="Session artifacts"
      >
        <Paperclip className="h-4 w-4" />
        <span className="hidden sm:inline">Artifacts</span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 transition-transform",
            open && "rotate-180",
          )}
        />
      </Button>

      {open && (
        <div className="absolute right-0 top-full mt-2 w-72 rounded-md border border-border bg-popover shadow-md z-30 text-sm">
          <div className="flex items-center justify-between px-3 py-2 border-b border-border/60">
            <span className="text-xs font-medium text-muted-foreground">
              Artifacts
            </span>
            <div className="flex items-center gap-2">
              <input
                ref={fileInputRef}
                type="file"
                className="hidden"
                onChange={(e) => void handleUpload(e)}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="text-muted-foreground hover:text-foreground disabled:opacity-50"
                title="Upload a file as an artifact"
              >
                {uploading ? (
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
              </button>
              <button
                type="button"
                onClick={() => void load()}
                disabled={loading}
                className="text-muted-foreground hover:text-foreground disabled:opacity-50"
                title="Refresh"
              >
                <RefreshCw
                  className={cn("h-3.5 w-3.5", loading && "animate-spin")}
                />
              </button>
            </div>
          </div>

          {error && (
            <div className="px-3 py-2 text-xs text-destructive">{error}</div>
          )}

          <div className="max-h-72 overflow-y-auto">
            {loading && rows.length === 0 && (
              <p className="px-3 py-3 text-xs text-muted-foreground">
                Loading…
              </p>
            )}
            {!loading && rows.length === 0 && !error && (
              <p className="px-3 py-3 text-xs text-muted-foreground">
                No artifacts yet. The agent publishes files here with{" "}
                <span className="font-mono">save_as_artifact</span>.
              </p>
            )}
            <ul>
              {rows.map((row) => (
                <li
                  key={row.filename}
                  className="flex items-center gap-2 px-3 py-2 hover:bg-accent"
                >
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-xs truncate">
                      {row.filename}
                    </div>
                    {row.latestVersion !== null && (
                      <div className="text-[10px] text-muted-foreground">
                        v{row.latestVersion}
                      </div>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => handleDownload(row)}
                    disabled={downloading === row.filename}
                    className="text-primary hover:text-primary/80 disabled:opacity-50 shrink-0"
                    title={`Download ${row.filename}`}
                  >
                    {downloading === row.filename ? (
                      <RefreshCw className="h-4 w-4 animate-spin" />
                    ) : (
                      <Download className="h-4 w-4" />
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}
