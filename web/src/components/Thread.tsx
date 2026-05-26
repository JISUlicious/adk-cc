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

/**
 * Renders the linear event stream as chat rows.
 *
 * Each ADK Event may contain multiple parts (text, function_call,
 * function_response). We flatten parts into independent rows so the UI
 * stays single-column and the tool-call cards sit inline with the
 * surrounding text.
 *
 * Two tool names get specialized renderers when their function_call is
 * still pending (no matching function_response yet):
 *   - `adk_request_confirmation` / `adk_cc_confirmation_form`
 *       → ConfirmationCard (HITL permission ask)
 *   - `ask_user_question`
 *       → AskUserQuestionCard (structured multi-choice form)
 *
 * Once the response lands, the call falls back to the generic
 * ToolCallCard so the historical state stays inspectable.
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

export function Thread({
  events,
  isStreaming,
  onSubmitFunctionResponse,
}: {
  events: RunEvent[]
  isStreaming: boolean
  /** Called when the user submits a response to a pending long-running
   * tool call (confirmation choice, ask_user_question answers). */
  onSubmitFunctionResponse: (
    callId: string,
    toolName: string,
    response: unknown,
  ) => void
}) {
  const deduped = dedupePartials(events)
  const pendingCallIds = collectPendingCallIds(deduped)
  const rows = flattenEvents(deduped)

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

function dedupePartials(events: RunEvent[]): RunEvent[] {
  // Keep all non-partials; for partials, keep only the last in each
  // (invocation_id, author) group. That collapses streaming chunks
  // into one row without losing standalone partials when there's no
  // group to merge into.
  const out: RunEvent[] = []
  const lastPartialIdx = new Map<string, number>()
  for (const e of events) {
    if (!e.partial) {
      out.push(e)
      continue
    }
    const key = `${e.invocation_id ?? ""}::${e.author ?? ""}`
    const prev = lastPartialIdx.get(key)
    if (prev !== undefined) {
      out[prev] = e
    } else {
      lastPartialIdx.set(key, out.length)
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

function flattenEvents(events: RunEvent[]): ChatRow[] {
  const rows: ChatRow[] = []
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
        rows.push({
          kind: "function_call",
          eventId,
          callId: part.function_call.id ?? "",
          name: part.function_call.name ?? "(unnamed)",
          args: part.function_call.args,
        })
      } else if (part.function_response) {
        rows.push({
          kind: "function_response",
          eventId,
          callId: part.function_response.id ?? "",
          name: part.function_response.name ?? "(unnamed)",
          response: part.function_response.response,
        })
      }
    }
  }
  return rows
}
