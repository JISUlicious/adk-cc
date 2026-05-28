import { useRef, useState, useMemo, type KeyboardEvent } from "react"
import { Send, Square, ClipboardList } from "lucide-react"
import { Button } from "./ui/button"
import { cn } from "@/lib/utils"
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
}: {
  onSend: (text: string) => void
  onAbort: () => void
  onSlashAction: (action: SlashAction) => void
  isStreaming: boolean
  disabled: boolean
  mode: string | undefined
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
    <div className="border-t bg-background px-4 py-3">
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
        {/* Plan-mode decoration frames just the input — the rest of
            the footer stays neutral so the indicator doesn't sprawl
            across the full window width. */}
        <div
          className={cn(
            "transition-colors",
            isPlan &&
              "rounded-md border border-primary/50 bg-brand-tint p-2 space-y-2",
          )}
        >
          {isPlan && (
            <div className="flex items-center gap-1.5 px-1 text-[11px] text-primary">
              <ClipboardList className="h-3.5 w-3.5" />
              <span className="font-medium">Plan mode</span>
              <span className="text-muted-foreground">
                — agent will draft a plan; destructive tools are off
                until you exit plan mode.
              </span>
            </div>
          )}
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
          className="flex-1 resize-none rounded-md bg-card px-3 py-2 text-sm shadow-[0_1px_3px_rgba(20,20,19,0.06)] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
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
