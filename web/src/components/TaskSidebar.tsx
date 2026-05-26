import { useMemo, useState } from "react"
import { ListChecks, Circle, CircleDashed, CheckCircle2 } from "lucide-react"
import { type RunEvent } from "@/api/sse"
import { cn } from "@/lib/utils"

/**
 * Right rail that mirrors the session's task list.
 *
 * Source: we walk the session events looking for function_response
 * payloads from `task_create`, `task_update`, and `task_list`. Each
 * tells us something:
 *   - task_create   → adds a new task (response.task_id, response.task_status)
 *   - task_update   → mutates an existing one (response.task)
 *   - task_list     → authoritative snapshot (response.tasks[]) — replaces
 *                     our derived state when it appears
 *
 * Walking event responses (not state files on disk) keeps the UI
 * stateless — no extra endpoint, no polling. The downside is the agent
 * has to actually emit task_create/task_update events for them to
 * appear, which it does by convention but isn't enforced.
 */

interface TaskRow {
  id: string
  title: string
  description?: string
  status: "pending" | "in_progress" | "completed"
}

export function TaskSidebar({ events }: { events: RunEvent[] }) {
  const tasks = useMemo(() => deriveTasks(events), [events])
  const [collapsed, setCollapsed] = useState(false)

  if (tasks.length === 0) return null

  return (
    <aside
      className={cn(
        "border-l bg-muted/40 flex flex-col transition-all",
        collapsed ? "w-10" : "w-64",
      )}
    >
      <button
        type="button"
        className="flex items-center gap-2 border-b px-3 py-3 text-left hover:bg-accent"
        onClick={() => setCollapsed((c) => !c)}
        title={collapsed ? "Expand tasks" : "Collapse tasks"}
      >
        <ListChecks className="h-4 w-4 text-muted-foreground" />
        {!collapsed && (
          <>
            <span className="text-xs font-medium">Tasks</span>
            <span className="ml-auto text-[10px] text-muted-foreground">
              {tasks.filter((t) => t.status !== "completed").length}/{tasks.length}
            </span>
          </>
        )}
        {collapsed && (
          <span className="text-[10px] text-muted-foreground absolute mt-7">
            {tasks.length}
          </span>
        )}
      </button>
      {!collapsed && (
        <ul className="flex-1 overflow-y-auto">
          {tasks.map((t) => (
            <li
              key={t.id}
              className="flex items-start gap-2 px-3 py-2 border-b border-border/50"
            >
              <StatusIcon status={t.status} />
              <div className="min-w-0 flex-1">
                <div
                  className={cn(
                    "text-xs",
                    t.status === "completed" &&
                      "line-through text-muted-foreground",
                  )}
                >
                  {t.title}
                </div>
                <div className="text-[10px] font-mono text-muted-foreground truncate">
                  {t.id.slice(0, 12)}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </aside>
  )
}

function StatusIcon({ status }: { status: TaskRow["status"] }) {
  if (status === "completed") {
    return <CheckCircle2 className="h-4 w-4 text-green-600 dark:text-green-400 mt-0.5 shrink-0" />
  }
  if (status === "in_progress") {
    return <CircleDashed className="h-4 w-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0 animate-pulse" />
  }
  return <Circle className="h-4 w-4 text-muted-foreground mt-0.5 shrink-0" />
}

// Re-export for tests / other consumers
export type { TaskRow }

function deriveTasks(events: RunEvent[]): TaskRow[] {
  const byId = new Map<string, TaskRow>()

  for (const e of events) {
    for (const part of e.content?.parts ?? []) {
      const fr = part.function_response
      if (!fr) continue
      const name = fr.name ?? ""
      const resp = (fr.response ?? {}) as Record<string, unknown>

      if (name === "task_list") {
        const list = resp.tasks
        if (Array.isArray(list)) {
          // Snapshot replaces — task_list is authoritative.
          byId.clear()
          for (const raw of list) {
            const row = toRow(raw as Record<string, unknown>)
            if (row) byId.set(row.id, row)
          }
        }
        continue
      }

      if (name === "task_create") {
        const id = resp.task_id
        if (typeof id === "string") {
          // task_create response carries task_id + task_status, not the
          // whole task, so we synthesize from the matching call's args.
          const args = findCreateArgs(events, fr.id ?? "")
          byId.set(id, {
            id,
            title: args?.title ?? id,
            description: args?.description,
            status:
              (typeof resp.task_status === "string"
                ? (resp.task_status as TaskRow["status"])
                : "pending") ?? "pending",
          })
        }
        continue
      }

      if (name === "task_update") {
        const task = resp.task
        const row = task ? toRow(task as Record<string, unknown>) : null
        if (row) byId.set(row.id, row)
        continue
      }
    }
  }

  return Array.from(byId.values())
}

function toRow(raw: Record<string, unknown>): TaskRow | null {
  const id = raw.id
  if (typeof id !== "string") return null
  const status = raw.status
  return {
    id,
    title: typeof raw.title === "string" ? raw.title : id,
    description:
      typeof raw.description === "string" ? raw.description : undefined,
    status:
      status === "pending" || status === "in_progress" || status === "completed"
        ? status
        : "pending",
  }
}

function findCreateArgs(
  events: RunEvent[],
  callId: string,
): { title?: string; description?: string } | null {
  if (!callId) return null
  for (const e of events) {
    for (const part of e.content?.parts ?? []) {
      const fc = part.function_call
      if (fc?.id === callId && fc.name === "task_create") {
        const a = (fc.args ?? {}) as Record<string, unknown>
        return {
          title: typeof a.title === "string" ? a.title : undefined,
          description:
            typeof a.description === "string" ? a.description : undefined,
        }
      }
    }
  }
  return null
}
