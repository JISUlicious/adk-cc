import { type RunEvent } from "@/api/sse"
import { MessageBubble } from "./MessageBubble"
import { ToolCallCard } from "./ToolCallCard"
import { ToolResponseCard } from "./ToolResponseCard"

/**
 * Renders the linear event stream as chat rows.
 *
 * Each ADK Event may contain multiple parts (text, function_call,
 * function_response). We flatten parts into independent rows so the UI
 * stays single-column and the tool-call cards sit inline with the
 * surrounding text.
 *
 * Partial events: the SSE stream emits incremental `partial: true`
 * events as the model streams tokens. We dedupe partials so only the
 * latest snapshot from each (invocation_id, author) group is rendered,
 * giving a "streaming" feel without the duplicate rows we'd get if we
 * appended every chunk.
 */
export function Thread({
  events,
  isStreaming,
}: {
  events: RunEvent[]
  isStreaming: boolean
}) {
  const rows = flattenEvents(dedupePartials(events))

  return (
    <div className="flex flex-col gap-3 px-6 py-4">
      {rows.length === 0 && !isStreaming && (
        <p className="text-center text-sm text-muted-foreground py-12">
          Start a conversation. Your messages go straight to the
          adk-cc agent on the server.
        </p>
      )}
      {rows.map((row, i) => (
        <Row key={`${row.eventId}:${row.kind}:${i}`} row={row} />
      ))}
      {isStreaming && (
        <p className="text-xs text-muted-foreground italic px-2">
          agent is working…
        </p>
      )}
    </div>
  )
}

function Row({ row }: { row: ChatRow }) {
  switch (row.kind) {
    case "text":
      return (
        <MessageBubble
          author={row.author}
          text={row.text}
          isPartial={row.isPartial}
        />
      )
    case "function_call":
      return (
        <ToolCallCard
          callId={row.callId}
          name={row.name}
          args={row.args}
        />
      )
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
