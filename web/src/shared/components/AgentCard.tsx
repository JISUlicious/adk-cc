import { useState } from "react"
import { ChevronDown, ChevronRight, Loader, Users } from "lucide-react"
import { cn, THREAD_ROW_WIDTH } from "@/shared/lib/utils"

/**
 * Spawned sub-agents, rendered as themselves (user requirement: the UI must
 * SHOW that spawned agents are running).
 *
 * Two tools, two states each:
 *   spawn_explorers   — lists what was spawned; its response arrives at once.
 *   collect_explorers — while pending this IS the "N explorers running…"
 *     indicator (children stream nothing to the thread; the pending collect is
 *     the visible face of their work). Done, it shows one line per explorer:
 *     ok/failed, elapsed, tool count, task.
 *
 * Same never-fold rule as skills and plans: behind a "N tool calls" header
 * this answers nothing.
 */
export function AgentCard({
  name,
  args,
  response,
}: {
  name: string
  args: unknown
  response: unknown
}) {
  const [open, setOpen] = useState(false)
  const a = (args ?? {}) as Record<string, unknown>
  const r = (response ?? {}) as Record<string, unknown>
  const pending = response === null || response === undefined

  let icon = <Users className="h-4 w-4 text-muted-foreground" />
  let line: string
  let detail: string[] = []

  if (name === "spawn_explorers") {
    const tasks = Array.isArray(a.tasks) ? (a.tasks as unknown[]) : []
    const spawned = Array.isArray(r.spawned) ? (r.spawned as unknown[]) : []
    line = pending
      ? `Spawning ${tasks.length} explorer${tasks.length === 1 ? "" : "s"}…`
      : `Spawned ${spawned.length} explorer${spawned.length === 1 ? "" : "s"}`
    detail = tasks.map((t) => `• ${String(t)}`)
  } else {
    // collect_explorers
    if (pending) {
      icon = <Loader className="h-4 w-4 animate-spin text-primary" />
      line = "Explorers running — waiting for reports…"
    } else {
      const done = Array.isArray(r.done) ? (r.done as Record<string, unknown>[]) : []
      const running = Array.isArray(r.running) ? (r.running as Record<string, unknown>[]) : []
      const failed = done.filter((d) => !d.ok).length
      line =
        `${done.length} report${done.length === 1 ? "" : "s"}` +
        (failed ? ` (${failed} failed)` : "") +
        (running.length ? ` · ${running.length} still running` : "")
      detail = [
        ...done.map((d) =>
          `${d.ok ? "✓" : "✗"} ${String(d.task ?? d.id)} — ` +
          `${Number(d.elapsed_s ?? 0).toFixed(1)}s, ${Number(d.tool_calls ?? 0)} tools` +
          (d.error ? ` — ${String(d.error)}` : "")),
        ...running.map((d) =>
          `… ${String(d.task ?? d.id)} — running ${Number(d.elapsed_s ?? 0).toFixed(0)}s`),
      ]
    }
  }

  return (
    <div className="flex justify-start">
      <div
        className={cn(THREAD_ROW_WIDTH, "rounded-md border border-border bg-card/50 text-sm")}
        data-agent-card={name}
        data-agent-pending={pending ? "1" : undefined}
      >
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-accent"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          {icon}
          <span className="flex-1 truncate text-xs">
            <span className="text-muted-foreground">Sub-agents</span> {line}
          </span>
        </button>
        {(open || pending) && detail.length > 0 && (
          <div className="border-t border-border px-3 py-2">
            {detail.map((d, i) => (
              <div key={i} className="truncate text-[11px] text-muted-foreground">
                {d}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
