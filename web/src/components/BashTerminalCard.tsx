import { useState } from "react"
import { Terminal, ChevronDown, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Terminal-style renderer for `run_bash` calls.
 *
 * Args (from `adk_cc/tools/bash/tool.py`):
 *   - command: str
 *   - timeout_seconds: int (default 30)
 *
 * Response (when present — null while pending):
 *   - status: "ok" | "timeout"
 *   - command: str
 *   - exit_code: int (omitted if timeout)
 *   - stdout: str (last 4000 chars)
 *   - stderr: str (last 2000 chars)
 *
 * Visual: dark monospace block with the command as a `$ ` prompt
 * row, stdout in light text, stderr in red, and an exit-code chip
 * in the header.
 */

interface BashArgs {
  command?: string
  timeout_seconds?: number
}

interface BashResponse {
  status?: string
  command?: string
  exit_code?: number
  stdout?: string
  stderr?: string
}

export function BashTerminalCard({
  args,
  response,
  callId,
}: {
  args: unknown
  response: unknown
  callId: string
}) {
  // Default open if there's already a response; collapsed if pending.
  const [open, setOpen] = useState(true)
  const a = (args ?? {}) as BashArgs
  const r = response ? ((response ?? {}) as BashResponse) : null
  const isPending = r === null
  const command = a.command ?? ""
  const exitCode = r?.exit_code
  const isTimeout = r?.status === "timeout"
  const isFailure = !isPending && (isTimeout || (typeof exitCode === "number" && exitCode !== 0))

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
          <Terminal className="h-4 w-4 text-muted-foreground" />
          <span className="font-mono text-xs truncate flex-1">
            {command || "run_bash"}
          </span>
          {isPending && (
            <span className="rounded-sm bg-amber-500/15 text-amber-700 dark:text-amber-300 px-1.5 py-0.5 text-[10px] font-medium">
              running…
            </span>
          )}
          {!isPending && isTimeout && (
            <span className="rounded-sm bg-orange-500/15 text-orange-700 dark:text-orange-300 px-1.5 py-0.5 text-[10px] font-medium">
              timeout
            </span>
          )}
          {!isPending && !isTimeout && (
            <span
              className={cn(
                "rounded-sm px-1.5 py-0.5 text-[10px] font-medium",
                isFailure
                  ? "bg-destructive/15 text-destructive"
                  : "bg-green-500/15 text-green-700 dark:text-green-400",
              )}
            >
              exit {exitCode ?? "?"}
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
            <pre className="rounded bg-zinc-950 text-zinc-100 dark:bg-zinc-900 p-3 text-xs leading-relaxed font-mono overflow-x-auto">
              <span className="text-emerald-400 select-none">$ </span>
              {command}
              {r?.stdout && (
                <>
                  {"\n"}
                  <span className="text-zinc-100">{r.stdout.trimEnd()}</span>
                </>
              )}
              {r?.stderr && (
                <>
                  {"\n"}
                  <span className="text-red-400">{r.stderr.trimEnd()}</span>
                </>
              )}
              {isPending && (
                <>
                  {"\n"}
                  <span className="text-zinc-500 italic">
                    (waiting for output…)
                  </span>
                </>
              )}
            </pre>
            {typeof a.timeout_seconds === "number" && a.timeout_seconds !== 30 && (
              <div className="text-[10px] text-muted-foreground font-mono">
                timeout = {a.timeout_seconds}s
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
