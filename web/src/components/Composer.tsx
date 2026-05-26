import { useRef, useState, type KeyboardEvent } from "react"
import { Send, Square } from "lucide-react"
import { Button } from "./ui/button"

/**
 * Multi-line message composer. Enter sends; Shift+Enter newlines.
 *
 * The send button doubles as a stop button while the agent is
 * streaming, so the user can abort a runaway turn without leaving
 * the keyboard.
 */
export function Composer({
  onSend,
  onAbort,
  isStreaming,
  disabled,
}: {
  onSend: (text: string) => void
  onAbort: () => void
  isStreaming: boolean
  disabled: boolean
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

  return (
    <div className="border-t bg-background px-4 py-3">
      <div className="flex items-end gap-2 max-w-3xl mx-auto">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKey}
          placeholder={
            disabled
              ? "Pick or create a session to start chatting"
              : "Message the agent — Enter to send, Shift+Enter for newline"
          }
          disabled={disabled}
          rows={2}
          className="flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
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
  )
}
