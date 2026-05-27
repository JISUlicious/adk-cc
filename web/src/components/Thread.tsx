import { type RunEvent } from "@/api/sse"
import { MessageBubble } from "./MessageBubble"
import { ToolCallCard } from "./ToolCallCard"
import { ToolResponseCard } from "./ToolResponseCard"
import {
  ConfirmationCard,
  type ConfirmPayload,
} from "./ConfirmationCard"
import {
  AskUserQuestionCard,
  type AskUserQuestionArgsDef,
} from "./AskUserQuestionCard"
import { BashTerminalCard } from "./BashTerminalCard"
import { FileEditCard } from "./FileEditCard"
import { PlanCard } from "./PlanCard"
import { ThoughtBubble } from "./ThoughtBubble"

/**
 * Renders the linear event stream as chat rows.
 *
 * Each ADK Event may contain multiple parts (text, function_call,
 * function_response). We flatten parts into independent rows so the UI
 * stays single-column and the tool-call cards sit inline with the
 * surrounding text.
 *
 * Specialized tool renderers (one paired row per call):
 *   - `run_bash`                         → BashTerminalCard
 *   - `edit_file` / `write_file`         → FileEditCard
 *   - `write_plan` / `read_current_plan` → PlanCard
 *
 * Long-running interactive widgets (rendered for the function_call
 * row while no matching function_response exists yet):
 *   - `adk_request_confirmation` / `adk_cc_confirmation_form`
 *       → ConfirmationCard
 *   - `ask_user_question`
 *       → AskUserQuestionCard
 *
 * Anything else: generic ToolCallCard / ToolResponseCard (two rows).
 *
 * Partial events: the SSE stream emits incremental `partial: true`
 * events as the model streams tokens. We dedupe partials so only the
 * latest snapshot from each (invocation_id, author) group is rendered.
 */

const CONFIRMATION_NAMES = new Set([
  "adk_request_confirmation",
  "adk_cc_confirmation_form",
])
const ASK_QUESTION_NAME = "ask_user_question"

// Tools whose call + response render as a single combined card. The
// associated function_response row is suppressed (consumed into the
// pair) to avoid a duplicate ToolResponseCard right below.
const PAIRED_RENDERERS: Record<string, "bash" | "edit" | "write" | "plan_read" | "plan_write"> = {
  run_bash: "bash",
  edit_file: "edit",
  write_file: "write",
  read_current_plan: "plan_read",
  write_plan: "plan_write",
}

export function Thread({
  events,
  isStreaming,
  onSubmitFunctionResponse,
}: {
  events: RunEvent[]
  isStreaming: boolean
  onSubmitFunctionResponse: (
    callId: string,
    toolName: string,
    response: unknown,
  ) => void
}) {
  const deduped = dedupePartials(events)
  const pendingCallIds = collectPendingCallIds(deduped)
  const responsesByCallId = collectResponses(deduped)
  const rows = flattenEvents(deduped, responsesByCallId)

  return (
    <div className="flex flex-col gap-3 px-6 py-4">
      {rows.length === 0 && !isStreaming && (
        <p className="text-center text-sm text-muted-foreground py-12">
          Start a conversation. Your messages go straight to the
          adk-cc agent on the server.
        </p>
      )}
      {rows.map((row, i) => (
        <Row
          key={`${row.eventId}:${row.kind}:${i}`}
          row={row}
          pendingCallIds={pendingCallIds}
          onSubmitFunctionResponse={onSubmitFunctionResponse}
          submitDisabled={isStreaming}
        />
      ))}
      {isStreaming && (
        <p className="text-xs text-muted-foreground italic px-2">
          agent is working…
        </p>
      )}
    </div>
  )
}

function Row({
  row,
  pendingCallIds,
  onSubmitFunctionResponse,
  submitDisabled,
}: {
  row: ChatRow
  pendingCallIds: Set<string>
  onSubmitFunctionResponse: (
    callId: string,
    toolName: string,
    response: unknown,
  ) => void
  submitDisabled: boolean
}) {
  switch (row.kind) {
    case "text":
      return (
        <MessageBubble
          author={row.author}
          text={row.text}
          isPartial={row.isPartial}
        />
      )
    case "thought":
      return <ThoughtBubble author={row.author} text={row.text} />
    case "function_call": {
      const isPending = pendingCallIds.has(row.callId)
      // Interactive widgets first — they only render while pending.
      if (isPending && CONFIRMATION_NAMES.has(row.name)) {
        // ADK's request_confirmation tool wraps the payload under
        // `toolConfirmation.payload` (camelCase via Pydantic
        // alias_generator). ConfirmationFormUiPlugin keeps the same
        // shape when it rewrites the function name from
        // adk_request_confirmation → adk_cc_confirmation_form, so
        // both names land at the same path. Be defensive in case a
        // future plugin variant flattens to args.payload directly.
        const payload = extractConfirmPayload(row.args)
        if (payload) {
          return (
            <ConfirmationCard
              payload={payload}
              disabled={submitDisabled}
              onSubmit={(resp) =>
                onSubmitFunctionResponse(row.callId, row.name, resp)
              }
            />
          )
        }
      }
      if (isPending && row.name === ASK_QUESTION_NAME) {
        const args = row.args as AskUserQuestionArgsDef | undefined
        if (args && Array.isArray(args.questions)) {
          return (
            <AskUserQuestionCard
              args={args}
              disabled={submitDisabled}
              onSubmit={(resp) =>
                onSubmitFunctionResponse(row.callId, row.name, resp)
              }
            />
          )
        }
      }
      return (
        <ToolCallCard
          callId={row.callId}
          name={row.name}
          args={row.args}
        />
      )
    }
    case "function_response":
      return (
        <ToolResponseCard
          callId={row.callId}
          name={row.name}
          response={row.response}
        />
      )
    case "tool_pair": {
      const { pairKind, callId, args, response } = row
      switch (pairKind) {
        case "bash":
          return <BashTerminalCard callId={callId} args={args} response={response} />
        case "edit":
          return (
            <FileEditCard
              op="edit"
              callId={callId}
              args={args}
              response={response}
            />
          )
        case "write":
          return (
            <FileEditCard
              op="write"
              callId={callId}
              args={args}
              response={response}
            />
          )
        case "plan_read":
          return (
            <PlanCard
              op="read"
              callId={callId}
              args={args}
              response={response}
            />
          )
        case "plan_write":
          return (
            <PlanCard
              op="write"
              callId={callId}
              args={args}
              response={response}
            />
          )
      }
    }
  }
}

function extractConfirmPayload(args: unknown): ConfirmPayload | null {
  if (!args || typeof args !== "object") return null
  const a = args as Record<string, unknown>
  const wrapped = (a.toolConfirmation as { payload?: ConfirmPayload } | undefined)?.payload
  if (wrapped && typeof wrapped === "object") return wrapped
  const direct = a.payload as ConfirmPayload | undefined
  if (direct && typeof direct === "object") return direct
  return null
}

// --- internals ---

type ChatRow =
  | {
      kind: "text"
      eventId: string
      author: string
      text: string
      isPartial: boolean
    }
  | {
      kind: "thought"
      eventId: string
      author: string
      text: string
    }
  | {
      kind: "function_call"
      eventId: string
      callId: string
      name: string
      args: unknown
    }
  | {
      kind: "function_response"
      eventId: string
      callId: string
      name: string
      response: unknown
    }
  | {
      kind: "tool_pair"
      eventId: string
      callId: string
      /** Specialized renderer key — drives which card is used. */
      pairKind: "bash" | "edit" | "write" | "plan_read" | "plan_write"
      args: unknown
      /** null while the response hasn't landed yet. */
      response: unknown
    }

function dedupePartials(events: RunEvent[]): RunEvent[] {
  // ADK streaming protocol (google/adk/models/base_llm.py:96-101):
  // each `partial: true` event carries a DELTA chunk
  // ("The weather", " in Tokyo is", " sunny."), then one final
  // `partial: false` event arrives containing the full accumulated
  // content. So we accumulate text per (invocation_id, author) group
  // for as long as partials keep arriving, and replace the accumulated
  // bubble with the final non-partial when it lands.
  const out: RunEvent[] = []
  const open = new Map<string, { idx: number; text: string }>()

  for (const e of events) {
    const key = `${e.invocation_id ?? ""}::${e.author ?? ""}`

    if (e.partial) {
      // Only accumulate visible text — thought parts get filtered so
      // the streaming bubble doesn't grow with internal-thinking noise.
      const deltaText = (e.content?.parts ?? [])
        .filter((p) => !p.thought)
        .map((p) => (typeof p.text === "string" ? p.text : ""))
        .join("")
      const entry = open.get(key)
      if (entry) {
        entry.text += deltaText
        // Replace the stored event's text part with the accumulated
        // value so the rendered bubble grows instead of flicker-replacing.
        out[entry.idx] = {
          ...e,
          content: { ...e.content, parts: [{ text: entry.text }] },
        }
      } else {
        open.set(key, { idx: out.length, text: deltaText })
        out.push({
          ...e,
          content: { ...e.content, parts: [{ text: deltaText }] },
        })
      }
      continue
    }

    // Non-partial. If this finalizes an open partial group, swap the
    // accumulated bubble for the final event (which per ADK spec
    // already contains the full text + any tool calls). Otherwise it
    // stands on its own.
    const entry = open.get(key)
    const hasText = (e.content?.parts ?? []).some(
      (p) => !p.thought && typeof p.text === "string" && p.text.trim().length > 0,
    )
    if (entry && hasText) {
      out[entry.idx] = e
      open.delete(key)
    } else {
      out.push(e)
    }
  }
  return out
}

function collectPendingCallIds(events: RunEvent[]): Set<string> {
  const calls = new Set<string>()
  const responses = new Set<string>()
  for (const e of events) {
    for (const part of e.content?.parts ?? []) {
      if (part.functionCall?.id) calls.add(part.functionCall.id)
      if (part.functionResponse?.id) responses.add(part.functionResponse.id)
    }
  }
  for (const r of responses) calls.delete(r)
  return calls
}

interface ResponsePart {
  name: string
  response: unknown
}

function collectResponses(events: RunEvent[]): Map<string, ResponsePart> {
  const m = new Map<string, ResponsePart>()
  for (const e of events) {
    for (const part of e.content?.parts ?? []) {
      const fr = part.functionResponse
      if (fr?.id) {
        m.set(fr.id, {
          name: fr.name ?? "",
          response: fr.response,
        })
      }
    }
  }
  return m
}

function flattenEvents(
  events: RunEvent[],
  responsesByCallId: Map<string, ResponsePart>,
): ChatRow[] {
  const rows: ChatRow[] = []
  // Track which response callIds got consumed into a pair so we can
  // skip emitting their standalone functionResponse row below.
  const consumedResponseIds = new Set<string>()

  for (const e of events) {
    const eventId = (e.id as string | undefined) ?? ""
    const author = e.author ?? "agent"
    const parts = e.content?.parts ?? []
    for (const part of parts) {
      // Thought parts (Gemini thought summaries, etc.) render as
      // faded ThoughtBubbles so the reader can see the model's
      // internal reasoning without confusing it with user-facing
      // reply text. Partials are still filtered upstream in
      // dedupePartials — only consolidated thoughts surface here.
      if (part.thought) {
        if (typeof part.text === "string" && part.text.trim().length > 0) {
          rows.push({
            kind: "thought",
            eventId,
            author,
            text: part.text,
          })
        }
        continue
      }

      if (typeof part.text === "string" && part.text.trim().length > 0) {
        rows.push({
          kind: "text",
          eventId,
          author,
          text: part.text,
          isPartial: Boolean(e.partial),
        })
      } else if (part.functionCall) {
        const callId = part.functionCall.id ?? ""
        const name = part.functionCall.name ?? "(unnamed)"
        const pairKind = PAIRED_RENDERERS[name]
        if (pairKind) {
          const matched = callId ? responsesByCallId.get(callId) : undefined
          rows.push({
            kind: "tool_pair",
            eventId,
            callId,
            pairKind,
            args: part.functionCall.args,
            response: matched ? matched.response : null,
          })
          if (callId) consumedResponseIds.add(callId)
        } else {
          rows.push({
            kind: "function_call",
            eventId,
            callId,
            name,
            args: part.functionCall.args,
          })
        }
      } else if (part.functionResponse) {
        const callId = part.functionResponse.id ?? ""
        if (consumedResponseIds.has(callId)) continue
        rows.push({
          kind: "function_response",
          eventId,
          callId,
          name: part.functionResponse.name ?? "(unnamed)",
          response: part.functionResponse.response,
        })
      }
    }
  }
  return rows
}
