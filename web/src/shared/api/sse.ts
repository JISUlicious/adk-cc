/**
 * SSE consumer for ADK's /run_sse endpoint.
 *
 * Why fetch + manual parser instead of the EventSource API: EventSource
 * doesn't support custom headers (Authorization), POST bodies, or
 * cancellation. ADK's /run_sse expects a POST with the request body in
 * JSON. So we do a streaming fetch, parse the SSE wire format inline,
 * and dispatch each event to the caller.
 *
 * Events from ADK are full Event JSON objects, one per "data:" line.
 * We deserialize and yield them as-is; the Thread renderer in Phase 1
 * interprets the parts inside.
 */

import { getToken } from "./auth"
import { ensureFreshAccess } from "./client"

/** An Event from ADK's session machinery. Loose typing — the actual
 * shape comes from google.adk.events.Event; we only inspect a few
 * fields in the renderers, so we keep this open-ended.
 *
 * Wire format: ADK's Event model uses `alias_generator=to_camel` with
 * `populate_by_name=True`, and the server serializes with
 * `by_alias=True`, so we get **camelCase** keys on the wire
 * (`functionCall`, `functionResponse`, `invocationId`). The Pydantic
 * model accepts either form on input, so when we POST back a
 * function_response we can use either case — we standardize on
 * camelCase to match what we read.
 *
 * `thought` parts are the model's internal thinking output (Gemini
 * "thought summaries", etc.) — they carry `text` but should NOT
 * render as a normal chat bubble. The renderers skip them.
 */
export interface RunEvent {
  author?: string
  content?: {
    role?: string
    parts?: Array<{
      text?: string
      /** Marks the part as model-internal thinking. Renderers hide it. */
      thought?: boolean
      functionCall?: { id?: string; name?: string; args?: unknown }
      functionResponse?: { id?: string; name?: string; response?: unknown }
    }>
  }
  partial?: boolean
  invocationId?: string
  actions?: Record<string, unknown>
  // ... ADK adds many more; we accept them.
  [key: string]: unknown
}

/**
 * Whether an event is the turn's final response — mirrors ADK's server-side
 * `Event.is_final_response()`. The UI uses this as the in-band "stop" signal so
 * the "agent is working…" indicator clears when the reply is actually done,
 * instead of waiting for the HTTP stream to close (which lags by any silent
 * post-turn work, e.g. the out-of-band session-title model call — the ~5s tail).
 *
 * A `true` here means "the agent is done or now waiting on the user" (a
 * long-running tool like ask_user_question / confirmation also counts, since the
 * agent has stopped working). Callers should RE-ARM on any later non-final event
 * so multi-agent turns (where each sub-agent emits its own final response before
 * control returns) don't stop the indicator early.
 */
export function isFinalResponse(e: RunEvent): boolean {
  const actions = (e.actions ?? {}) as { skipSummarization?: boolean }
  const longRunning = (e as { longRunningToolIds?: unknown[] }).longRunningToolIds
  if (actions.skipSummarization || (Array.isArray(longRunning) && longRunning.length > 0)) {
    return true
  }
  if (e.partial) return false
  const parts = e.content?.parts ?? []
  const hasCall = parts.some((p) => p.functionCall)
  const hasResp = parts.some((p) => p.functionResponse)
  return !hasCall && !hasResp
}

export interface RunArgs {
  appName: string
  userId: string
  sessionId: string
  message: string
}

export interface FunctionResponseArgs {
  appName: string
  userId: string
  sessionId: string
  /** The function_call id this resolves. Matches the original call event. */
  callId: string
  /** The tool name. Required by ADK to route the resume back to the
   * right pending call (`ask_user_question`, `adk_request_confirmation`,
   * etc.). */
  toolName: string
  /** JSON payload the agent receives as the call's response. Shape is
   * tool-specific:
   *   - confirmation: `{chose_id, comment?, persist_across_sessions?}`
   *   - ask_user_question: `{<question_text>: <chosen_label>}` (or array
   *     for multi_select). */
  response: unknown
}

export interface StreamCallbacks {
  onEvent: (event: RunEvent) => void
  onError?: (err: Error) => void
  onClose?: () => void
}

/** Open an SSE stream against /run_sse. Returns an abort function. */
export function streamRun(args: RunArgs, cb: StreamCallbacks): () => void {
  const ctrl = new AbortController()
  const newMessage = {
    role: "user",
    parts: [{ text: args.message }],
  }
  void _runStreamLoop(args, newMessage, cb, ctrl.signal)
  return () => ctrl.abort()
}

/** Resume a pending long-running tool call by submitting its
 * function_response. The agent loop picks up the response on the next
 * turn and continues. Returns an abort function. */
export function streamFunctionResponse(
  args: FunctionResponseArgs,
  cb: StreamCallbacks,
): () => void {
  const ctrl = new AbortController()
  const newMessage = {
    role: "user",
    parts: [
      {
        functionResponse: {
          id: args.callId,
          name: args.toolName,
          response: args.response,
        },
      },
    ],
  }
  void _runStreamLoop(args, newMessage, cb, ctrl.signal)
  return () => ctrl.abort()
}

async function _runStreamLoop(
  args: { appName: string; userId: string; sessionId: string },
  newMessage: unknown,
  cb: StreamCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  }
  // This path bypasses apiFetch (streams can't be replayed after a 401), so
  // refresh proactively when the access token is at/near expiry.
  await ensureFreshAccess()
  const token = getToken()
  if (token) headers["Authorization"] = `Bearer ${token}`

  try {
    const resp = await fetch("/run_sse", {
      method: "POST",
      headers,
      body: JSON.stringify({
        appName: args.appName,
        userId: args.userId,
        sessionId: args.sessionId,
        newMessage,
        // streaming=true tells ADK's runner to emit partial events as
        // the model produces tokens (`partial: true` chunks). Without
        // it, the loop still streams *event*-level (one full
        // function_call / response / text event at a time) but no
        // token-level partials. We already dedupe partials in
        // Thread.tsx so multiple partial events per turn collapse
        // into one streaming bubble.
        streaming: true,
      }),
      signal,
    })

    if (!resp.ok) {
      throw new Error(
        `/run_sse returned ${resp.status} ${resp.statusText}`,
      )
    }
    if (!resp.body) {
      throw new Error("/run_sse returned no body")
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // SSE messages are delimited by \n\n. Split, process complete
      // ones, keep the trailing partial chunk for the next read.
      const messages = buffer.split("\n\n")
      buffer = messages.pop() ?? ""

      for (const message of messages) {
        const trimmed = message.trim()
        if (!trimmed) continue
        // Each SSE message is a stack of lines: `event:`, `id:`, `data:`.
        // We care about `data:` only; ADK emits the full JSON there.
        for (const line of trimmed.split("\n")) {
          if (!line.startsWith("data:")) continue
          const json = line.slice("data:".length).trim()
          if (!json) continue
          try {
            const event = JSON.parse(json) as RunEvent
            // A server-side failure (model error, rate limit, …) arrives as a
            // VALID event: `{"error": "..."}`. Routing it through onEvent
            // renders as an empty event — the chat just "stops" with no
            // explanation (field confusion, 2026-07-22: every backend failure
            // looked like a silent hang). Surface it as the error it is;
            // ChatPage's onError shows the banner and ends the stream state.
            const errText = (event as { error?: unknown }).error
            if (typeof errText === "string" && errText) {
              cb.onError?.(new Error(errText))
              continue
            }
            cb.onEvent(event)
          } catch (parseErr) {
            cb.onError?.(
              new Error(
                `Failed to parse SSE event JSON: ${String(parseErr)} — payload: ${json.slice(0, 120)}…`,
              ),
            )
          }
        }
      }
    }
    cb.onClose?.()
  } catch (err) {
    if ((err as Error).name === "AbortError") return
    cb.onError?.(err as Error)
  }
}
