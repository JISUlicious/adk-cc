import { type RunEvent } from "@/api/sse"
import { MessageBubble } from "./MessageBubble"
import { ToolResponseCard } from "./ToolResponseCard"
import {
  ConfirmationCard,
  type ConfirmPayload,
} from "./ConfirmationCard"
import {
  AskUserQuestionCard,
  type AskUserQuestionArgsDef,
} from "./AskUserQuestionCard"
import { ArtifactChip } from "./ArtifactChip"
import { BashTerminalCard } from "./BashTerminalCard"
import { FileEditCard } from "./FileEditCard"
import { PlanCard } from "./PlanCard"
import { ThoughtBubble } from "./ThoughtBubble"
import { ToolCard } from "./ToolCard"

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
 * Anything else: ToolCard — call+response merged with a status chip
 *   (called / finished / error). Orphan function_responses (no matching
 *   call) still fall through to ToolResponseCard.
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
const PAIRED_RENDERERS: Record<
  string,
  "bash" | "edit" | "write" | "plan_read" | "plan_write"
> = {
  run_bash: "bash",
  edit_file: "edit",
  write_file: "write",
  read_current_plan: "plan_read",
  write_plan: "plan_write",
}

/** Function-call names we intentionally drop from the thread.
 * `_handback_to_coordinator` is the synthetic control-call ADK fires
 * from `after_agent_callback` to keep the LLM flow looping (see
 * `adk_cc/agent.py::_force_coordinator_continuation`). It never gets
 * a response and isn't user-relevant. */
const HIDDEN_TOOL_NAMES = new Set(["_handback_to_coordinator"])

export function Thread({
  events,
  isStreaming,
  onSubmitFunctionResponse,
  appName,
  userId,
  sessionId,
}: {
  events: RunEvent[]
  isStreaming: boolean
  onSubmitFunctionResponse: (
    callId: string,
    toolName: string,
    response: unknown,
  ) => void
  /** Needed by ArtifactChip to construct the artifact download URL. */
  appName: string
  userId: string
  sessionId: string
}) {
  const deduped = dedupePartials(events)
  const pendingCallIds = collectPendingCallIds(deduped)
  const responsesByCallId = collectResponses(deduped)
  const rows = mergeAdjacentThoughts(
    flattenEvents(deduped, responsesByCallId),
  )

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
          appName={appName}
          userId={userId}
          sessionId={sessionId}
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
  appName,
  userId,
  sessionId,
}: {
  row: ChatRow
  pendingCallIds: Set<string>
  onSubmitFunctionResponse: (
    callId: string,
    toolName: string,
    response: unknown,
  ) => void
  submitDisabled: boolean
  appName: string
  userId: string
  sessionId: string
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
    case "artifact":
      return (
        <ArtifactChip
          appName={appName}
          userId={userId}
          sessionId={sessionId}
          filename={row.filename}
          version={row.version}
        />
      )
    case "function_response":
      // Orphan response (no matching function_call in the event log)
      // — rare, falls through to the generic response card.
      return (
        <ToolResponseCard
          callId={row.callId}
          name={row.name}
          response={row.response}
        />
      )
    case "tool_pair": {
      const { pairKind, callId, name, args, response } = row
      const isPending = pendingCallIds.has(callId)

      // Interactive widgets while pending take precedence over the
      // generic ToolCard. Once a response lands, the call+response
      // pair falls through to the generic card so the user can see
      // their answered question/confirmation as resolved history.
      if (isPending && CONFIRMATION_NAMES.has(name)) {
        // ADK wraps the payload under `toolConfirmation.payload`
        // (camelCase via Pydantic alias_generator).
        // ConfirmationFormUiPlugin keeps the same shape when it
        // rewrites the function name. extractConfirmPayload also
        // tolerates a flat `args.payload` for future plugin variants.
        const payload = extractConfirmPayload(args)
        if (payload) {
          return (
            <ConfirmationCard
              payload={payload}
              disabled={submitDisabled}
              onSubmit={(resp) =>
                onSubmitFunctionResponse(callId, name, resp)
              }
            />
          )
        }
      }
      if (isPending && name === ASK_QUESTION_NAME) {
        const askArgs = args as AskUserQuestionArgsDef | undefined
        if (askArgs && Array.isArray(askArgs.questions)) {
          return (
            <AskUserQuestionCard
              args={askArgs}
              disabled={submitDisabled}
              onSubmit={(resp) =>
                onSubmitFunctionResponse(callId, name, resp)
              }
            />
          )
        }
      }

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
        case "generic":
          return (
            <ToolCard
              name={name}
              callId={callId}
              args={args}
              response={response}
            />
          )
      }
    }
  }
}

/** Collapse adjacent thought rows from the same author into a single
 * row. Some providers split internal thinking across multiple
 * non-partial events (one part per event, or several short parts in
 * one event); rendered as-is they show up as a stack of tiny faded
 * bubbles. Merging keeps the thought as one coherent block — same
 * cadence as Claude's thinking summaries. */
function mergeAdjacentThoughts(rows: ChatRow[]): ChatRow[] {
  const merged: ChatRow[] = []
  for (const row of rows) {
    const prev = merged[merged.length - 1]
    if (
      row.kind === "thought" &&
      prev &&
      prev.kind === "thought" &&
      prev.author === row.author
    ) {
      merged[merged.length - 1] = {
        ...prev,
        text: prev.text + row.text,
      }
      continue
    }
    merged.push(row)
  }
  return merged
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
      /** Tool function name — carried even into the generic case so
       * the ToolCard header can print it. */
      name: string
      /** Specialized renderer key — drives which card is used.
       * `"generic"` falls through to ToolCard. */
      pairKind:
        | "bash"
        | "edit"
        | "write"
        | "plan_read"
        | "plan_write"
        | "generic"
      args: unknown
      /** null while the response hasn't landed yet. */
      response: unknown
    }
  | {
      kind: "artifact"
      eventId: string
      filename: string
      version: number
    }

function dedupePartials(events: RunEvent[]): RunEvent[] {
  // ADK streaming protocol (google/adk/models/base_llm.py:96-101):
  // each `partial: true` event carries a DELTA chunk
  // ("The weather", " in Tokyo is", " sunny."), then one final
  // `partial: false` event arrives containing the full accumulated
  // content. So we accumulate per (invocation_id, author) group for
  // as long as partials keep arriving, and replace the accumulated
  // event with the final non-partial when it lands.
  //
  // We accumulate TWO streams in parallel: visible text and thought
  // text. Some providers stream thought deltas the same way they
  // stream body deltas, so dropping them would lose the model's
  // reasoning entirely. The thought stream surfaces as a separate
  // part (rendered by ThoughtBubble); the body text stream surfaces
  // as a MessageBubble.
  const out: RunEvent[] = []
  const open = new Map<string, { idx: number; text: string; thought: string }>()

  for (const e of events) {
    const key = `${e.invocation_id ?? ""}::${e.author ?? ""}`

    if (e.partial) {
      let deltaText = ""
      let deltaThought = ""
      for (const part of e.content?.parts ?? []) {
        if (typeof part.text !== "string") continue
        if (part.thought) deltaThought += part.text
        else deltaText += part.text
      }

      const entry = open.get(key)
      if (entry) {
        entry.text += deltaText
        entry.thought += deltaThought
      } else {
        const created = { idx: out.length, text: deltaText, thought: deltaThought }
        open.set(key, created)
        out.push(e) // placeholder; rewritten below
      }
      const cur = open.get(key)!
      out[cur.idx] = {
        ...e,
        content: {
          ...e.content,
          parts: [
            ...(cur.thought ? [{ text: cur.thought, thought: true }] : []),
            ...(cur.text ? [{ text: cur.text }] : []),
          ],
        },
      }
      continue
    }

    // Non-partial. If this finalizes an open partial group AND carries
    // visible text/thought of its own, swap the accumulated event for
    // the final one (per ADK spec it already contains the full body +
    // any tool calls + the consolidated thought). Otherwise pass through.
    const entry = open.get(key)
    const hasMeaningfulText = (e.content?.parts ?? []).some(
      (p) => typeof p.text === "string" && p.text.trim().length > 0,
    )
    if (entry && hasMeaningfulText) {
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

    // Artifact deltas — one chip per (filename → version) entry.
    // ADK populates event.actions.artifactDelta whenever a tool calls
    // ctx.save_artifact() (in adk-cc that's `save_as_artifact`).
    // Wire keys are camelCase via Pydantic's to_camel alias, but
    // accept snake_case too for resilience.
    const actions = (e.actions ?? {}) as Record<string, unknown>
    const delta =
      (actions.artifactDelta as Record<string, unknown> | undefined) ??
      (actions.artifact_delta as Record<string, unknown> | undefined)
    if (delta && typeof delta === "object") {
      for (const [filename, ver] of Object.entries(delta)) {
        if (typeof filename !== "string" || !filename) continue
        const version = typeof ver === "number" ? ver : Number(ver)
        if (!Number.isFinite(version)) continue
        rows.push({ kind: "artifact", eventId, filename, version })
      }
    }

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
        if (HIDDEN_TOOL_NAMES.has(name)) {
          // Skip the row AND eat the (rare) matching response so we
          // don't leave an orphan ToolResponseCard below.
          if (callId) consumedResponseIds.add(callId)
          continue
        }
        const pairKind = PAIRED_RENDERERS[name] ?? "generic"
        const matched = callId ? responsesByCallId.get(callId) : undefined
        rows.push({
          kind: "tool_pair",
          eventId,
          callId,
          name,
          pairKind,
          args: part.functionCall.args,
          response: matched ? matched.response : null,
        })
        if (callId) consumedResponseIds.add(callId)
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
