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

/** An Event from ADK's session machinery. Loose typing — the actual
 * shape comes from google.adk.events.Event; we only inspect a few
 * fields in the renderers, so we keep this open-ended. */
export interface RunEvent {
  author?: string
  content?: {
    role?: string
    parts?: Array<{
      text?: string
      function_call?: { id?: string; name?: string; args?: unknown }
      function_response?: { id?: string; name?: string; response?: unknown }
    }>
  }
  partial?: boolean
  invocation_id?: string
  actions?: Record<string, unknown>
  // ... ADK adds many more; we accept them.
  [key: string]: unknown
}

export interface RunArgs {
  appName: string
  userId: string
  sessionId: string
  message: string
}

interface StreamCallbacks {
  onEvent: (event: RunEvent) => void
  onError?: (err: Error) => void
  onClose?: () => void
}

/** Open an SSE stream against /run_sse. Returns an abort function. */
export function streamRun(args: RunArgs, cb: StreamCallbacks): () => void {
  const ctrl = new AbortController()
  void _runStreamLoop(args, cb, ctrl.signal)
  return () => ctrl.abort()
}

async function _runStreamLoop(
  args: RunArgs,
  cb: StreamCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  }
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
        newMessage: {
          role: "user",
          parts: [{ text: args.message }],
        },
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
