import { useRef, useState, useMemo, type KeyboardEvent, type ReactNode } from "react"
import { Send, Square, ClipboardList, Paperclip, X, Loader } from "lucide-react"
import { Button } from "./ui/button"
import { SandboxBadge } from "./SandboxBadge"
import { cn } from "@/shared/lib/utils"
import {
  attachmentLine,
  formatBytes,
  uploadWithAutoRename,
} from "@/shared/api/uploads"
import { ApiError } from "@/shared/api/client"
import {
  SlashCommandMenu,
  filterSlash,
  type SlashCommand,
  type SlashAction,
} from "./SlashCommandMenu"

/** A file staged in the composer, before/while/after its upload. */
interface StagedFile {
  id: number
  file: File
  status: "staged" | "uploading" | "error"
  error?: string
}

let _stagedId = 0

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
  modelChip,
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
  /** Current-model chip (desktop) — rendered in the meta row next to the
   *  sandbox badge so the user sees which model the next turn uses. */
  modelChip?: ReactNode
}) {
  const [value, setValue] = useState("")
  const [slashCursor, setSlashCursor] = useState(0)
  const ref = useRef<HTMLTextAreaElement>(null)
  // Attachments (#121): staged locally, uploaded on SEND (not on pick — the
  // user can still remove one), then announced to the model as plain-text
  // lines appended to the message. A failed upload blocks the send: a
  // message naming a file that is not there is a half-truth the model acts
  // on.
  const [staged, setStaged] = useState<StagedFile[]>([])
  const [uploading, setUploading] = useState(false)
  // Visible failure line under the chips. A tooltip-only error reads as
  // "pressed Enter and nothing happened" — reported live with a PDF.
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  function stageFiles(files: FileList | File[] | null) {
    if (!files || disabled) return
    setUploadError(null)
    const add = Array.from(files).map((file) => ({
      id: ++_stagedId, file, status: "staged" as const,
    }))
    if (add.length) setStaged((s) => [...s, ...add])
  }

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
    if (disabled || isStreaming || uploading) return
    if (!trimmed && staged.length === 0) return
    if (staged.length === 0) {
      onSend(trimmed)
      setValue("")
      setSlashCursor(0)
      ref.current?.focus()
      return
    }
    void submitWithAttachments(trimmed)
  }

  async function submitWithAttachments(text: string) {
    // Upload sequentially, THEN send one message carrying the attachment
    // lines. Any failure keeps the message unsent and marks the chip —
    // never tell the model about a file that did not land.
    if (!sessionId || !userId) {
      setUploadError("no active session — pick or create one first")
      return
    }
    setUploading(true)
    setUploadError(null)
    setStaged((s) => s.map((f) => ({ ...f, status: "uploading" as const })))
    const lines: string[] = []
    for (const f of staged) {
      try {
        const up = await uploadWithAutoRename({
          file: f.file, name: f.file.name,
          userId, sessionId,
        })
        lines.push(attachmentLine(up))
      } catch (e) {
        // Prefer the server's own explanation (413 cap, 400 name, 404
        // route/project) over a generic status line.
        const detail =
          e instanceof ApiError && e.body && typeof e.body === "object"
            && "detail" in (e.body as Record<string, unknown>)
            ? String((e.body as Record<string, unknown>).detail)
            : e instanceof Error ? e.message : String(e)
        setStaged((s) => s.map((x) =>
          x.id === f.id
            ? { ...x, status: "error" as const, error: detail }
            : { ...x, status: "staged" as const }))
        setUploadError(`upload failed: ${f.file.name} — ${detail}`)
        setUploading(false)
        return
      }
    }
    setUploading(false)
    setStaged([])
    onSend([text, ...lines].filter(Boolean).join("\n"))
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
    <div
      className="adk-composer px-4 pt-0.5 pb-2 faded-top-edge"
      onDragOver={(e) => {
        if (disabled || !e.dataTransfer.types.includes("Files")) return
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return
        e.preventDefault()
        setDragOver(false)
        stageFiles(e.dataTransfer.files)
      }}
    >
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
              {mode && mode !== "plan" && (
                <span
                  data-mode-chip={mode}
                  className="text-[10px] text-muted-foreground"
                  title="Session permission mode — /mode-… to change"
                >
                  {mode === "bypassPermissions" ? "bypass" : mode}
                </span>
              )}
              <SandboxBadge sessionId={sessionId} userId={userId} />
              {footer && <div className="adk-gauge-slot">{footer}</div>}
            </div>
          </div>
          {/* Staged attachments (#121): chips above the input row. */}
          {staged.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 px-1 pb-0.5">
              {staged.map((f) => (
                <span
                  key={f.id}
                  data-upload-chip={f.file.name}
                  title={f.error || f.file.name}
                  className={cn(
                    "flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-[11px]",
                    f.status === "error"
                      ? "border-destructive/40 bg-destructive/10 text-destructive"
                      : "border-border bg-muted text-muted-foreground",
                  )}
                >
                  {f.status === "uploading" && (
                    <Loader className="h-3 w-3 animate-spin" />
                  )}
                  <Paperclip className="h-3 w-3" />
                  <span className="max-w-48 truncate">{f.file.name}</span>
                  <span className="opacity-70">{formatBytes(f.file.size)}</span>
                  {f.status !== "uploading" && (
                    <button
                      type="button"
                      className="ml-0.5 hover:text-foreground"
                      title="Remove"
                      onClick={() =>
                        setStaged((s) => s.filter((x) => x.id !== f.id))}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </span>
              ))}
            </div>
          )}
          {uploadError && (
            <div
              data-upload-error
              className="px-1 pb-0.5 text-[11px] text-destructive"
            >
              {uploadError}
            </div>
          )}
          <div className={cn("flex items-end gap-2 rounded-md",
                             dragOver && "ring-2 ring-primary/60")}>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            stageFiles(e.target.files)
            e.target.value = ""
          }}
        />
        <Button
          type="button"
          size="icon"
          variant="ghost"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || uploading}
          title="Attach a file — it lands at uploads/<name> in the workspace"
        >
          <Paperclip className="h-4 w-4" />
        </Button>
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => {
            setValue(e.target.value)
            setSlashCursor(0)
          }}
          onKeyDown={handleKey}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData?.files ?? [])
            if (files.length) {
              e.preventDefault()
              stageFiles(files)
            }
          }}
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
            disabled={disabled || uploading
              || (!value.trim() && staged.length === 0)}
            title={uploading ? "Uploading attachments…" : "Send (Enter)"}
          >
            {uploading
              ? <Loader className="h-4 w-4 animate-spin" />
              : <Send className="h-4 w-4" />}
          </Button>
        )}
          </div>
          {/* Minimal model line under the input — quiet by design; the pin
              state is the only thing that earns a colour. */}
          {modelChip && (
            <div className="flex items-center px-1 pt-0.5">{modelChip}</div>
          )}
        </div>
      </div>
    </div>
  )
}
