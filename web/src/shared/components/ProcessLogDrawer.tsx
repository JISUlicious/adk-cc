import { useCallback, useEffect, useRef, useState } from "react"
import { X, Square, ArrowDownToLine } from "lucide-react"
import { apiFetch } from "@/shared/api/client"
import { Button } from "@/shared/components/ui/button"
import type { ProcessRow } from "./ProcessDock"

/**
 * The log of one background process, following live (#108).
 *
 * A panel over the right rail rather than a modal: reading a server log is
 * something you do WHILE looking at the thread, and a modal would make you
 * choose. Follow-tail disengages the moment you scroll up — the behaviour
 * every log viewer needs and few have — and offers a way back.
 */
export function ProcessLogDrawer({
  process,
  onClose,
}: {
  process: ProcessRow | null
  onClose: () => void
}) {
  const [log, setLog] = useState("")
  const [status, setStatus] = useState<ProcessRow | null>(process)
  const [follow, setFollow] = useState(true)
  const bodyRef = useRef<HTMLPreElement>(null)

  const id = process?.id
  const reload = useCallback(() => {
    if (!id) return
    apiFetch<{ log: string }>(`/api/processes/${encodeURIComponent(id)}/log`)
      .then((r) => setLog(r.log ?? ""))
      .catch(() => {})
    apiFetch<ProcessRow>(`/api/processes/${encodeURIComponent(id)}`)
      .then(setStatus)
      .catch(() => {})
  }, [id])

  useEffect(() => {
    if (!id) return
    setFollow(true)
    reload()
    const live = status?.status === "running" || status?.status === "starting"
    const iv = setInterval(reload, live ? 1500 : 8000)
    return () => clearInterval(iv)
    // `status?.status` intentionally in deps: polling slows once it exits.
  }, [id, reload, status?.status])

  useEffect(() => {
    if (follow && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [log, follow])

  if (!process) return null

  const live = status?.status === "running" || status?.status === "starting"

  return (
    <div className="absolute inset-0 z-20 flex flex-col bg-background"
         data-process-log={process.id}>
      <div className="flex items-start gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{status?.label ?? process.label}</span>
            {status?.port && (
              <a href={`http://localhost:${status.port}`} target="_blank" rel="noreferrer"
                 className="shrink-0 text-xs text-primary hover:underline">
                :{status.port}
              </a>
            )}
            <span className="shrink-0 rounded bg-muted px-1 text-[10px] text-muted-foreground">
              {status?.status ?? "?"}
            </span>
          </div>
          <p className="truncate font-mono text-[10px] text-muted-foreground"
             title={status?.command ?? process.command}>
            {status?.command ?? process.command}
          </p>
        </div>
        {live && status?.can_terminate && (
          <Button size="sm" variant="outline"
                  onClick={() =>
                    apiFetch(`/api/processes/${encodeURIComponent(process.id)}/terminate`,
                             { method: "POST" }).then(reload).catch(() => {})}>
            <Square className="h-3.5 w-3.5" /> Stop
          </Button>
        )}
        <button type="button" onClick={onClose} title="Close"
                aria-label="Close log" className="p-1 text-muted-foreground hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </div>
      <pre
        ref={bodyRef}
        onScroll={(e) => {
          const el = e.currentTarget
          // Disengage follow when the user scrolls away from the bottom.
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          if (follow !== atBottom) setFollow(atBottom)
        }}
        className="flex-1 overflow-auto whitespace-pre-wrap break-words bg-muted/30 px-3 py-2 font-mono text-[11px] leading-relaxed"
      >
        {log || "(no output yet)"}
      </pre>
      {!follow && (
        <button
          type="button"
          onClick={() => setFollow(true)}
          className="absolute bottom-3 right-4 flex items-center gap-1 rounded-full border border-border bg-card px-2 py-1 text-[11px] shadow"
        >
          <ArrowDownToLine className="h-3 w-3" /> Follow
        </button>
      )}
    </div>
  )
}
