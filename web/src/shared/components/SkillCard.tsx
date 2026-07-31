import { useState } from "react"
import { ChevronDown, ChevronRight, Boxes } from "lucide-react"
import { cn, THREAD_ROW_WIDTH } from "@/shared/lib/utils"

/**
 * A skill activation, rendered as itself rather than as an anonymous tool call.
 *
 * Two complaints the ecosystem keeps making, both of which the generic ToolCard
 * reproduced: you cannot tell WHICH skill the model chose, and you do not see
 * what loading it cost until after it has happened. A skill's whole body enters
 * context on activation — the spec budgets under 5,000 tokens for it — so the
 * size is the interesting number, and it is right there in the response.
 */
export function SkillCard({
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
  const skill = String(a.skill_name ?? a.name ?? "")
  const detail = summarize(name, a, response)
  const failed = isError(response)

  return (
    <div className="flex justify-start">
      <div className={cn(THREAD_ROW_WIDTH, "rounded-md border border-border bg-card/50 text-sm")}>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-accent"
          data-skill-call={skill || name}
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <Boxes className={cn("h-4 w-4", failed ? "text-destructive" : "text-muted-foreground")} />
          <span className="flex-1 truncate text-xs">
            <span className="text-muted-foreground">Skill</span>{" "}
            <span className="font-mono">{skill || "—"}</span>
            {detail && <span className="text-muted-foreground"> · {detail}</span>}
          </span>
        </button>
        {open && (
          <div className="border-t border-border px-3 py-2">
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-[11px] text-muted-foreground">
              {stringify(response ?? args)}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}

/** The one line worth reading without expanding the row. */
function summarize(name: string, a: Record<string, unknown>, response: unknown): string {
  const r = (response ?? {}) as Record<string, unknown>
  if (name === "run_skill_script") {
    const file = String(a.file_path ?? a.script_path ?? "")
    if (response == null) return `running ${file}`
    const err = typeof r.error === "string" ? r.error : ""
    if (err) return `${file} — ${err.slice(0, 80)}`
    const status = String(r.status ?? "")
    return status && status !== "success" ? `${file} — ${status}` : `${file} — ok`
  }
  if (name === "load_skill_resource" || name === "search_skill_resource") {
    return `read ${String(a.resource_path ?? a.file_path ?? a.query ?? "")}`.trim()
  }
  // load_skill: the instructions have just entered context, so say how much.
  if (response == null) return "loading"
  if (typeof r.error === "string" && r.error) return r.error.slice(0, 80)
  const size = approxTokens(response)
  return size ? `loaded · ~${fmt(size)} tokens` : "loaded"
}

/** Rough, and deliberately so: the point is the order of magnitude a skill just
 *  added to the context, not an exact count nobody can act on. */
function approxTokens(response: unknown): number {
  const text = typeof response === "string" ? response : stringify(response)
  return text ? Math.round(text.length / 4) : 0
}

function fmt(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function isError(response: unknown): boolean {
  if (!response || typeof response !== "object") return false
  const r = response as Record<string, unknown>
  return Boolean(r.error) || r.status === "error"
}

function stringify(v: unknown): string {
  if (typeof v === "string") return v
  try {
    return JSON.stringify(v, null, 2) ?? ""
  } catch {
    return String(v)
  }
}
