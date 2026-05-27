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
    case "function_call": {
      const isPending = pendingCallIds.has(row.callId)
      // Interactive widgets first — they only render while pending.
      if (isPending && CONFIRMATION_NAMES.has(row.name)) {
        const payload =
          row.args && typeof row.args === "object"
            ? ((row.args as { payload?: ConfirmPayload }).payload ?? null)
            : null
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
      const deltaText = (e.content?.parts ?? [])
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
      (p) => typeof p.text === "string" && p.text.length > 0,
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
      if (part.function_call?.id) calls.add(part.function_call.id)
      if (part.function_response?.id) responses.add(part.function_response.id)
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
      const fr = part.function_response
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
  // skip emitting their standalone function_response row below.
  const consumedResponseIds = new Set<string>()

  // First pass: build the rows, deciding whether each call is
  // specialized (→ tool_pair row) or generic (→ function_call row).
  for (const e of events) {
    const eventId = (e.id as string | undefined) ?? ""
    const author = e.author ?? "agent"
    const parts = e.content?.parts ?? []
    for (const part of parts) {
      if (typeof part.text === "string" && part.text.length > 0) {
        rows.push({
          kind: "text",
          eventId,
          author,
          text: part.text,
          isPartial: Boolean(e.partial),
        })
      } else if (part.function_call) {
        const callId = part.function_call.id ?? ""
        const name = part.function_call.name ?? "(unnamed)"
        const pairKind = PAIRED_RENDERERS[name]
        if (pairKind) {
          const matched = callId ? responsesByCallId.get(callId) : undefined
          rows.push({
            kind: "tool_pair",
            eventId,
            callId,
            pairKind,
            args: part.function_call.args,
            response: matched ? matched.response : null,
          })
          if (callId) consumedResponseIds.add(callId)
        } else {
          rows.push({
            kind: "function_call",
            eventId,
            callId,
            name,
            args: part.function_call.args,
          })
        }
      } else if (part.function_response) {
        const callId = part.function_response.id ?? ""
        if (consumedResponseIds.has(callId)) continue // shown inside the pair row
        rows.push({
          kind: "function_response",
          eventId,
          callId,
          name: part.function_response.name ?? "(unnamed)",
          response: part.function_response.response,
        })
      }
    }
  }
  return rows
}
