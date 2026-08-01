import { useEffect, useRef, useState } from "react"
import { Check, Loader, Users } from "lucide-react"
import { apiFetch } from "@/shared/api/client"

/**
 * The little bottom section of the right panel listing spawned sub-agents
 * (user request). The thread's AgentCards are history; this is "what is
 * running for this session RIGHT NOW", polled from the server-side registry —
 * children execute server-side and stream nothing, so the registry is the
 * only live source. Renders nothing at all when there are no children, so
 * sessions that never spawn pay no space.
 */
type Child = { id: string; task: string; elapsed_s: number; status: string }

export function SubagentsDock({
  appName,
  userId,
  sessionId,
}: {
  appName: string
  userId: string
  sessionId: string
}) {
  const [children, setChildren] = useState<Child[]>([])
  const timer = useRef<number | null>(null)

  useEffect(() => {
    let stop = false
    // Adaptive poll: 6s idle heartbeat, 1.5s while anything is listed. The
    // endpoint is a dict lookup — cheap — but a visible-tab-only guard keeps
    // background sessions quiet.
    async function tick() {
      if (stop) return
      let next = 6000
      if (!document.hidden) {
        try {
          const r = await apiFetch<{ children: Child[] }>(
            `/api/subagents?app_name=${encodeURIComponent(appName)}` +
              `&user_id=${encodeURIComponent(userId)}` +
              `&session_id=${encodeURIComponent(sessionId)}`,
          )
          if (!stop) setChildren(r.children ?? [])
          if ((r.children ?? []).length > 0) next = 1500
        } catch {
          /* transient — keep the last snapshot */
        }
      }
      timer.current = window.setTimeout(tick, next)
    }
    tick()
    return () => {
      stop = true
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [appName, userId, sessionId])

  if (children.length === 0) return null
  const running = children.filter((c) => c.status === "running").length

  return (
    <div
      className="border-t border-border bg-card/30 px-3 py-2"
      data-subagents-dock
    >
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
        <Users className="h-3.5 w-3.5" />
        Sub-agents
        <span className="font-normal">
          {running > 0 ? `${running} running` : "finishing"}
        </span>
      </div>
      <div className="space-y-0.5">
        {children.map((c) => (
          <div key={c.id} className="flex items-center gap-1.5 text-[11px]">
            {c.status === "running" ? (
              <Loader className="h-3 w-3 shrink-0 animate-spin text-primary" />
            ) : (
              <Check className="h-3 w-3 shrink-0 text-muted-foreground" />
            )}
            <span className="min-w-0 flex-1 truncate text-muted-foreground" title={c.task}>
              {c.task}
            </span>
            <span className="shrink-0 tabular-nums text-muted-foreground/70">
              {Math.round(c.elapsed_s)}s
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
