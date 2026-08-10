import { useEffect, useState } from "react"
import { ArrowLeft, Download, Paperclip } from "lucide-react"
import {
  listDir,
  rawFileUrl,
  readFile,
  type DirEntry,
  type FileContent,
} from "@/shared/api/desktop-files"
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

/**
 * WEB shell file viewing, scoped deliberately small: a flat list of the
 * session workspace's `uploads/` files inside the ARTIFACTS panel, plus the
 * viewer. No tree — the web workspace root is the user HOME, and a browser
 * over it exposed `.sessions/` scratch from every other session; uploads
 * are the files a web user actually needs to see. (The desktop panel keeps
 * its full project tree — there the root IS the project.)
 */

export function UploadsList({
  userId,
  sessionId,
  refreshKey,
  onOpen,
}: {
  userId: string
  sessionId: string
  refreshKey?: number
  onOpen: (path: string) => void
}) {
  const [entries, setEntries] = useState<DirEntry[]>([])

  useEffect(() => {
    let cancelled = false
    listDir(userId, sessionId, "uploads")
      .then((l) => !cancelled && setEntries(l.entries.filter((e) => e.type === "file")))
      .catch(() => !cancelled && setEntries([]))  // no uploads dir yet — no section
    return () => {
      cancelled = true
    }
  }, [userId, sessionId, refreshKey])

  if (entries.length === 0) return null
  return (
    <div className="mt-2 border-t border-border/60 pt-2">
      <div className="px-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        Uploads
      </div>
      <ul className="space-y-0.5">
        {entries.map((e) => (
          <li key={e.name}>
            <button
              type="button"
              onClick={() => onOpen(`uploads/${e.name}`)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-accent"
            >
              <Paperclip className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{e.name}</span>
              {e.size !== null && (
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {e.size >= 1024 * 1024
                    ? `${(e.size / 1024 / 1024).toFixed(1)} MB`
                    : `${Math.max(1, Math.round(e.size / 1024))} KB`}
                </span>
              )}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function WebFileViewer({
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
            className="whitespace-pre-wrap break-words p-3 text-[11px] leading-relaxed"
          />
        )}
      </div>
    </div>
  )
}
