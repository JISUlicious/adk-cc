import { useState } from "react"
import { FileEdit, FilePlus, ChevronDown, ChevronRight } from "lucide-react"

/**
 * Renders `edit_file` and `write_file` tool calls. Two shapes:
 *
 *   edit_file args (`adk_cc/tools/schemas.py`):
 *     { path, old_string, new_string }
 *
 *   write_file args:
 *     { path, content }
 *
 * Both responses:
 *     { status: "ok" | "error" | "sandbox_denied",
 *       path: str, bytes?: int, error?: str }
 *
 * For edit_file we show old/new in two side-by-side blocks tinted
 * red/green — this gives a recognizable "before/after" without
 * pulling in a real diff library. For write_file we show the path
 * and content preview as a single green block.
 *
 * Phase 4 polish can swap the side-by-side for a real unified diff
 * (e.g. via `diff` + minimal renderer) once we've validated this is
 * the right grouping.
 */

type Op = "edit" | "write"

interface EditArgs {
  path?: string
  old_string?: string
  new_string?: string
}
interface WriteArgs {
  path?: string
  content?: string
}
interface FileResponse {
  status?: string
  path?: string
  bytes?: number
  error?: string
}

export function FileEditCard({
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
  const a = (args ?? {}) as EditArgs & WriteArgs
  const r = response ? ((response ?? {}) as FileResponse) : null
  const isPending = r === null
  const failed = !isPending && r?.status !== "ok"

  const path = a.path ?? r?.path ?? "(unknown path)"
  const Icon = op === "edit" ? FileEdit : FilePlus
  const title = op === "edit" ? "Edit file" : "Write file"

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] w-full rounded-md border border-border bg-card/50 text-sm">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent rounded-md"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <Icon className="h-4 w-4 text-muted-foreground" />
          <span className="text-xs font-medium">{title}</span>
          <span className="font-mono text-xs text-muted-foreground truncate flex-1">
            {path}
          </span>
          {isPending && (
            <span className="rounded-sm bg-secondary text-secondary-foreground px-1.5 py-0.5 text-[10px] font-medium">
              writing…
            </span>
          )}
          {!isPending && failed && (
            <span className="rounded-sm bg-destructive/15 text-destructive px-1.5 py-0.5 text-[10px] font-medium">
              {r?.status ?? "error"}
            </span>
          )}
          {!isPending && !failed && (
            <span className="rounded-sm bg-brand-tint text-primary px-1.5 py-0.5 text-[10px] font-medium">
              ok{typeof r?.bytes === "number" ? ` · ${r.bytes}b` : ""}
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
            {op === "edit" ? (
              <EditDiff
                oldString={a.old_string ?? ""}
                newString={a.new_string ?? ""}
              />
            ) : (
              <CodeBlock
                content={a.content ?? ""}
                variant="add"
                label="content"
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function EditDiff({
  oldString,
  newString,
}: {
  oldString: string
  newString: string
}) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <CodeBlock content={oldString} variant="remove" label="before" />
      <CodeBlock content={newString} variant="add" label="after" />
    </div>
  )
}

function CodeBlock({
  content,
  variant,
  label,
}: {
  content: string
  variant: "add" | "remove"
  label: string
}) {
  // kami: diff red/green is the rare case where second/third hues
  // are functional, not decorative — but we tune them warmer so they
  // sit on a parchment canvas without screaming. Olive-green for add
  // ("#5a6e3a"-ish), warm rust for remove ("#9a3325" via destructive).
  const labelStyle =
    variant === "add"
      ? { color: "#5a6e3a" }
      : { color: "#9a3325" }
  const blockStyle =
    variant === "add"
      ? { background: "rgba(132, 145, 95, 0.12)", color: "#3d3d3a" }
      : { background: "rgba(154, 51, 37, 0.10)", color: "#3d3d3a" }
  return (
    <div className="min-w-0">
      <div
        className="text-[10px] uppercase tracking-wider mb-1 font-medium"
        style={labelStyle}
      >
        {label}
      </div>
      <pre
        className="rounded p-2 text-xs leading-relaxed font-mono overflow-x-auto max-h-64 whitespace-pre-wrap break-all"
        style={blockStyle}
      >
        {content || <span className="opacity-50">(empty)</span>}
      </pre>
    </div>
  )
}
