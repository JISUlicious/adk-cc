import { useRef, useState, type KeyboardEvent } from "react"
import { Send, Square, ClipboardList } from "lucide-react"
import { Button } from "./ui/button"
import { cn } from "@/lib/utils"

/**
 * Multi-line message composer. Enter sends; Shift+Enter newlines.
 *
 * The send button doubles as a stop button while the agent is
 * streaming, so the user can abort a runaway turn without leaving
 * the keyboard.
 *
 * When the session is in PLAN permission mode, the composer renders a
 * small violet badge + hint above the textarea and tints the textarea
 * border violet so the user sees the active mode at the moment of
 * typing — that's the surface where it matters most.
 * `session.state.permission_mode` arrives via the `mode` prop; values
 * are defined in `adk_cc/permissions/modes.py::PermissionMode`.
 */
export function Composer({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  mode,
}: {
  onSend: (text: string) => void
  onAbort: () => void
  isStreaming: boolean
  disabled: boolean
  mode: string | undefined
}) {
  const [value, setValue] = useState("")
  const ref = useRef<HTMLTextAreaElement>(null)

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || disabled || isStreaming) return
    onSend(trimmed)
    setValue("")
    // Restore focus so the user can keep typing immediately.
    ref.current?.focus()
  }

  function handleKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const isPlan = mode === "plan"

  return (
    <div
      className={cn(
        "border-t bg-background px-4 py-3",
        isPlan && "border-t-violet-500 bg-violet-50/40 dark:bg-violet-950/20",
      )}
    >
      <div className="max-w-3xl mx-auto space-y-2">
        {isPlan && (
          <div className="flex items-center gap-1.5 text-[11px] text-violet-700 dark:text-violet-300">
            <ClipboardList className="h-3.5 w-3.5" />
            <span className="font-medium">Plan mode</span>
            <span className="text-violet-700/70 dark:text-violet-300/70">
              — agent will draft a plan; destructive tools are off until
              you exit plan mode.
            </span>
          </div>
        )}
        <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKey}
          placeholder={
            disabled
              ? "Pick or create a session to start chatting"
              : isPlan
                ? "Plan mode — describe what you want the agent to plan"
                : "Message the agent — Enter to send, Shift+Enter for newline"
          }
          disabled={disabled}
          rows={2}
          className={cn(
            "flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 disabled:cursor-not-allowed disabled:opacity-50",
            isPlan
              ? "border-violet-400 focus-visible:ring-violet-500 placeholder:text-violet-700/50 dark:placeholder:text-violet-300/50"
              : "border-input focus-visible:ring-ring",
          )}
        />
        {isStreaming ? (
          <Button
            type="button"
            variant="destructive"
            size="icon"
            onClick={onAbort}
            title="Stop the streaming response"
          >
            <Square className="h-4 w-4" />
          </Button>
        ) : (
          <Button
            type="button"
            size="icon"
            onClick={submit}
            disabled={disabled || !value.trim()}
            title="Send (Enter)"
          >
            <Send className="h-4 w-4" />
          </Button>
        )}
        </div>
      </div>
    </div>
  )
}
