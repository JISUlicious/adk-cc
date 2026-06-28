import { useState } from "react"
import { ShieldAlert } from "lucide-react"
import { Button } from "./ui/button"

/**
 * Renders an adk-cc permission "ask" confirmation prompt inline in the
 * thread. The payload format is defined by
 * `adk_cc/permissions/confirmation.py::ConfirmPrompt` and arrives in
 * the function_call args under `payload`.
 *
 * Two function-call names hit this widget — `adk_request_confirmation`
 * (the canonical ADK name) and `adk_cc_confirmation_form` (the rewrite
 * name used by `ConfirmationFormUiPlugin` for the bundled UI). Both
 * carry the same args / response contract; the rewrite just changes the
 * function name so the bundled UI's renderer matches.
 *
 * On submit, posts a function_response with `{chose_id, comment?,
 * persist_across_sessions?}`. The plugin reads `chose_id` to route the
 * decision (allow_once / allow_always / deny / etc.).
 */

export interface ConfirmOptionDef {
  id: string
  label: string
  description: string
}

export interface ConfirmPayload {
  style: "confirm_deny" | "single_select"
  title: string
  detail: string
  options: ConfirmOptionDef[]
  with_comment?: boolean
  with_persist_toggle?: boolean
}

export function ConfirmationCard({
  payload,
  onSubmit,
  disabled,
}: {
  payload: ConfirmPayload
  onSubmit: (response: {
    chose_id: string
    comment?: string
    persist_across_sessions?: boolean
  }) => void
  disabled: boolean
}) {
  const [comment, setComment] = useState("")
  const [persist, setPersist] = useState(false)

  function pick(id: string) {
    if (disabled) return
    onSubmit({
      chose_id: id,
      ...(payload.with_comment && comment ? { comment } : {}),
      ...(payload.with_persist_toggle ? { persist_across_sessions: persist } : {}),
    })
  }

  return (
    <div className="flex justify-start">
      {/* kami: single accent. The "this needs your attention" weight
          comes from ink-blue framing and the icon, not a second hue. */}
      <div className="max-w-[80%] w-full rounded-md border border-primary/40 bg-brand-tint text-sm">
        <div className="flex items-start gap-2 px-3 pt-3">
          <ShieldAlert className="h-5 w-5 text-primary mt-0.5 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="font-medium">{payload.title}</div>
            <div className="text-xs text-muted-foreground whitespace-pre-wrap mt-1">
              {payload.detail}
            </div>
          </div>
        </div>
        {payload.with_comment && (
          <div className="px-3 pt-3">
            <textarea
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              placeholder="Optional comment for the agent"
              rows={2}
              disabled={disabled}
              className="w-full resize-none rounded-md border border-input bg-background px-2 py-1.5 text-xs"
            />
          </div>
        )}
        {payload.with_persist_toggle && (
          <label className="flex items-center gap-2 px-3 pt-3 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={persist}
              onChange={(e) => setPersist(e.target.checked)}
              disabled={disabled}
            />
            Persist across sessions
          </label>
        )}
        <div className="flex flex-col gap-2 p-3">
          {payload.options.map((opt) => (
            <Button
              key={opt.id}
              type="button"
              variant={
                opt.id === "deny"
                  ? "destructive"
                  : opt.id === "allow_always"
                    ? "default"
                    : "outline"
              }
              size="sm"
              disabled={disabled}
              onClick={() => pick(opt.id)}
              className="justify-start text-left h-auto py-2"
            >
              <div className="flex flex-col items-start gap-0.5">
                <span className="font-medium">{opt.label}</span>
                <span className="text-[10px] font-normal opacity-70">
                  {opt.description}
                </span>
              </div>
            </Button>
          ))}
        </div>
      </div>
    </div>
  )
}
