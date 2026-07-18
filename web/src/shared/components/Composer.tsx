import { useRef, useState, useMemo, type KeyboardEvent, type ReactNode } from "react"
import { Send, Square, ClipboardList } from "lucide-react"
import { Button } from "./ui/button"
import { SandboxBadge } from "./SandboxBadge"
import { cn } from "@/shared/lib/utils"
import {
  SlashCommandMenu,
  filterSlash,
  type SlashCommand,
  type SlashAction,
} from "./SlashCommandMenu"

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
 *
 * When the input starts with `/`, the SlashCommandMenu floats above
 * the textarea. Up/Down navigates, Tab/Enter picks, Escape closes.
 * UI-only commands route to `onSlashAction`; templated-message
 * commands replace the input with their text and submit through
 * `onSend` like any normal message.
 */
export function Composer({
  onSend,
  onAbort,
  onSlashAction,
  isStreaming,
  disabled,
  mode,
  sessionId,
  userId,
  footer,
  taskStrip,
}: {
  onSend: (text: string) => void
  onAbort: () => void
  onSlashAction: (action: SlashAction) => void
  isStreaming: boolean
  disabled: boolean
  mode: string | undefined
  /** Active session id — lets the SandboxBadge show THIS chat's resolved
   *  backend (per-session truth) instead of the global setting. */
  sessionId?: string | null
  /** Active project id (desktop) — pre-turn backend prediction for the badge. */
  userId?: string | null
  /** Rendered below the input, left-aligned within the same max-width column
   *  (e.g. the context gauge) so it lines up with the input box. */
  footer?: ReactNode
  /** Slim strip stacked directly above the plan-mode row (e.g. the task list),
   *  aligned to the same max-width column as the input. */
  taskStrip?: ReactNode
}) {
  const [value, setValue] = useState("")
  const [slashCursor, setSlashCursor] = useState(0)
  const ref = useRef<HTMLTextAreaElement>(null)

  // Slash UX is only active when the FIRST char is `/` and there's no
  // newline — i.e. the user is typing a command, not a message that
  // happens to contain a slash. Filter against the text after `/`.
  const slashQuery = useMemo(() => {
    if (!value.startsWith("/")) return null
    if (value.includes("\n")) return null
    return value.slice(1)
  }, [value])
  const slashMatches = useMemo(
    () => (slashQuery === null ? [] : filterSlash(slashQuery)),
    [slashQuery],
  )
  const slashOpen = slashQuery !== null && slashMatches.length > 0

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || disabled || isStreaming) return
    onSend(trimmed)
    setValue("")
    setSlashCursor(0)
    ref.current?.focus()
  }

  function pickSlash(cmd: SlashCommand) {
    if (cmd.kind.type === "action") {
      // Clear input and dispatch — no message hits the wire.
      setValue("")
      setSlashCursor(0)
      onSlashAction(cmd.kind.action)
      ref.current?.focus()
    } else {
      // Send the templated text now. Don't leave it sitting in the
      // input for the user to second-guess.
      const text = cmd.kind.text
      setValue("")
      setSlashCursor(0)
      onSend(text)
      ref.current?.focus()
    }
  }

  function handleKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (slashOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault()
        setSlashCursor((i) => (i + 1) % slashMatches.length)
        return
      }
      if (e.key === "ArrowUp") {
        e.preventDefault()
        setSlashCursor((i) => (i - 1 + slashMatches.length) % slashMatches.length)
        return
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault()
        pickSlash(slashMatches[slashCursor] ?? slashMatches[0])
        return
      }
      if (e.key === "Escape") {
        e.preventDefault()
        setValue("")
        setSlashCursor(0)
        return
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  const isPlan = mode === "plan"

  return (
    <div className="adk-composer px-4 pt-0.5 pb-2 faded-top-edge">
      <div className="max-w-3xl mx-auto relative">
        {slashOpen && (
          <div className="absolute bottom-full left-0 right-0 mb-2">
            <SlashCommandMenu
              query={slashQuery ?? ""}
              selectedIndex={Math.min(slashCursor, slashMatches.length - 1)}
              onPick={pickSlash}
            />
          </div>
        )}
        {/* Task strip stacked directly above the plan-mode row, same column. */}
        {taskStrip}
        {/* Plan-mode decoration frames just the input. The wrapper +
            badge slot are ALWAYS rendered so the footer height stays
            constant across the mode toggle — only the border/bg/
            badge-visibility light up when plan is active. */}
        <div
          className={cn(
            "adk-composer-box rounded-md border px-2 py-1 space-y-0.5 transition-colors",
            isPlan
              ? "border-primary/50 bg-brand-tint"
              : "border-transparent bg-transparent",
          )}
        >
          {/* Meta row above the input: plan-mode hint on the left (invisible
              when off), context gauge on the right. Always rendered so the box
              height is constant, and it does double duty instead of an empty
              spacer. */}
          <div className="flex items-center gap-1.5 px-1 text-[11px]">
            <div
              className={cn(
                "flex min-w-0 items-center gap-1.5 overflow-hidden text-primary",
                !isPlan && "invisible",
              )}
            >
              <ClipboardList className="h-3.5 w-3.5 shrink-0" />
              <span className="font-medium shrink-0">Plan mode</span>
              <span className="truncate text-muted-foreground">
                — agent will draft a plan; destructive tools are off
                until you exit plan mode.
              </span>
            </div>
            <div className="ml-auto flex items-center gap-2 shrink-0">
              <SandboxBadge sessionId={sessionId} userId={userId} />
              {footer && <div className="adk-gauge-slot">{footer}</div>}
            </div>
          </div>
          <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => {
            setValue(e.target.value)
            setSlashCursor(0)
          }}
          onKeyDown={handleKey}
          placeholder={
            disabled
              ? "Pick or create a session to start chatting"
              : isPlan
                ? "Plan mode — describe what you want the agent to plan"
                : "Message the agent — Enter to send, type / for commands"
          }
          disabled={disabled}
          rows={2}
          className="adk-composer-input flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
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
    </div>
  )
}
