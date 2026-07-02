import { useMemo, useState } from "react"
import { ChevronDown, ListChecks } from "lucide-react"
import { type RunEvent } from "@/shared/api/sse"
import { deriveTasks, type TaskRow } from "./TaskSidebar"
import { cn } from "@/shared/lib/utils"

/**
 * Compact task list shown ABOVE the composer (over the chat input, above the
 * plan indicator) — the relocated home for the session's tasks now that the
 * right rail belongs to the artifact/file panel. Reuses `deriveTasks` (walks
 * task_create/update/list event responses); renders nothing when there are no
 * tasks. Collapsible to a one-line count.
 */
export function TaskStrip({ events }: { events: RunEvent[] }) {
  const tasks = useMemo(() => deriveTasks(events), [events])
  const [collapsed, setCollapsed] = useState(false)

  if (tasks.length === 0) return null

  const openCount = tasks.filter((t) => t.status !== "completed").length

  return (
    <div className="adk-task-strip flex items-center gap-2 px-1 py-0.5 text-[11px]">
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="flex shrink-0 items-center gap-1 text-muted-foreground hover:text-foreground"
        title={collapsed ? "Expand tasks" : "Collapse tasks"}
      >
        <ListChecks className="h-3.5 w-3.5" />
        <span className="font-medium">Tasks</span>
        <span>
          {openCount}/{tasks.length}
        </span>
        <ChevronDown className={cn("h-3 w-3 transition-transform", collapsed && "-rotate-90")} />
      </button>
      {!collapsed && (
        <div className="adk-task-strip-items flex min-w-0 gap-1.5 overflow-x-auto">
          {tasks.map((t) => (
            <span
              key={t.id}
              className={cn(
                "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5",
                t.status === "in_progress"
                  ? "border-primary/50 bg-primary/10 font-medium text-primary"
                  : "border-border/60 bg-background/60",
              )}
              title={t.description || t.title}
            >
              <StatusDot status={t.status} />
              <span className={cn(t.status === "completed" && "text-muted-foreground line-through")}>
                {t.title}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function StatusDot({ status }: { status: TaskRow["status"] }) {
  // in_progress: a solid accent dot with a halo ring — always fully opaque so
  // it reads clearly (no pulse, which dims it). completed: olive. pending:
  // hollow muted ring.
  if (status === "in_progress") {
    return (
      <span
        className="h-2.5 w-2.5 shrink-0 rounded-full bg-primary ring-2 ring-primary/30"
        aria-hidden
      />
    )
  }
  return (
    <span
      className={cn(
        "h-2 w-2 shrink-0 rounded-full",
        status === "pending" && "border border-muted-foreground/60",
      )}
      style={status === "completed" ? { backgroundColor: "#5a6e3a" } : undefined}
      aria-hidden
    />
  )
}
