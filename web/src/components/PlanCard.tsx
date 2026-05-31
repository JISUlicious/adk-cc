import { useState } from "react"
import { BookOpen, FileText, ChevronDown, ChevronRight } from "lucide-react"
import { Markdown } from "@/lib/markdown"

/**
 * Renders `write_plan` and `read_current_plan` tool calls.
 *
 *   write_plan args:        { content: str (markdown), slug?: str }
 *   write_plan response:    { status, path, title, slug, bytes }
 *
 *   read_current_plan args: {} (empty)
 *   read_current_plan resp: { status: "ok" | "no_plan" | "sandbox_denied",
 *                             path, title, content, history[],
 *                             error?, warning? }
 *
 * Markdown is rendered as a styled <pre> for V1 — adequate for the
 * Claude-Code-style "step list" plans the agent emits. Phase 4 can
 * swap in `react-markdown` for full GFM + headings if the UX wins.
 *
 * Pending state (call without response) still shows the args content,
 * since `write_plan` carries the full plan body inbound — so the user
 * can read what was just authored.
 */

type Op = "read" | "write"

interface PlanArgs {
  content?: string
  slug?: string
}
interface PlanResponse {
  status?: string
  path?: string
  title?: string
  slug?: string
  bytes?: number
  content?: string
  history?: Array<{
    path?: string
    title?: string
    slug?: string
    written_at?: string
  }>
  error?: string
  warning?: string
}

export function PlanCard({
  op,
  args,
  response,
  callId,
}: {
  op: Op
  args: unknown
  response: unknown
  callId: string
}) {
  const [open, setOpen] = useState(true)
  const [historyOpen, setHistoryOpen] = useState(false)
  const a = (args ?? {}) as PlanArgs
  const r = response ? ((response ?? {}) as PlanResponse) : null
  const isPending = r === null

  // For write_plan, args carry the content; for read_current_plan,
  // the response does. Pick whichever is available, preferring the
  // response so we show whatever the server actually has.
  const content = r?.content ?? a.content ?? ""
  const title = r?.title ?? extractTitle(content) ?? (op === "read" ? "Current plan" : "Plan")
  const path = r?.path
  const isMissing = !isPending && r?.status === "no_plan"
  const failed = !isPending && r?.status === "sandbox_denied"

  const Icon = op === "read" ? BookOpen : FileText

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] w-full rounded-md border border-primary/40 bg-brand-tint text-sm">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent rounded-md"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-primary" />
          ) : (
            <ChevronRight className="h-4 w-4 text-primary" />
          )}
          <Icon className="h-4 w-4 text-primary" />
          <span className="text-xs font-medium truncate flex-1">{title}</span>
          {isPending && (
            <span className="rounded-sm bg-amber-500/15 text-amber-700 dark:text-amber-300 px-1.5 py-0.5 text-[10px] font-medium">
              writing…
            </span>
          )}
          {isMissing && (
            <span className="rounded-sm bg-muted text-muted-foreground px-1.5 py-0.5 text-[10px] font-medium">
              no plan
            </span>
          )}
          {failed && (
            <span className="rounded-sm bg-destructive/15 text-destructive px-1.5 py-0.5 text-[10px] font-medium">
              denied
            </span>
          )}
          {callId && (
            <span className="font-mono text-[10px] text-muted-foreground shrink-0">
              {callId.slice(0, 8)}
            </span>
          )}
        </button>
        {open && (
          <div className="px-3 pb-3 space-y-2">
            {failed && r?.error && (
              <div className="rounded bg-destructive/10 text-destructive px-2 py-1 text-xs">
                {r.error}
              </div>
            )}
            {r?.warning && (
              <div className="rounded bg-amber-500/10 text-amber-800 dark:text-amber-200 px-2 py-1 text-xs">
                {r.warning}
              </div>
            )}
            {content && (
              <div className="rounded bg-background/60 p-3 max-h-96 overflow-y-auto text-sm leading-relaxed">
                <Markdown>{content}</Markdown>
              </div>
            )}
            {path && (
              <div className="text-[10px] font-mono text-muted-foreground">
                {path}
              </div>
            )}
            {r?.history && r.history.length > 0 && (
              <div>
                <button
                  type="button"
                  onClick={() => setHistoryOpen((h) => !h)}
                  className="text-[10px] text-primary hover:underline"
                >
                  {historyOpen ? "Hide" : "Show"} history ({r.history.length})
                </button>
                {historyOpen && (
                  <ul className="mt-1 space-y-1">
                    {r.history.map((h, i) => (
                      <li
                        key={(h.path ?? "") + i}
                        className="text-[10px] font-mono text-muted-foreground"
                      >
                        {h.written_at?.slice(0, 19)} · {h.title ?? h.slug ?? h.path}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function extractTitle(markdown: string): string | undefined {
  // Find the first `# <title>` heading line; mirrors the
  // server-side title extraction in adk_cc/plans/storage.py.
  const m = markdown.match(/^#\s+(.+?)\s*$/m)
  return m?.[1]
}
