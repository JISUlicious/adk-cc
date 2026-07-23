/**
 * Turn Broker client — durable runs (analysis/durable-runs-design.md).
 *
 * The legacy `/run_sse` path executes the run inside the HTTP response, so a
 * refresh/disconnect kills the turn. The broker runs turns server-side; this
 * client starts a turn and TAILS it (`/api/turns/{id}/stream?cursor=N`), so
 * detaching is harmless and reopening a session can re-attach mid-turn.
 *
 * Keeps the legacy `StreamCallbacks` contract (onEvent/onError/onClose) so
 * ChatPage's stream state machine is unchanged. The returned abort function
 * is a FULL abort: it cancels the server-side turn AND detaches — the only
 * caller is the stop button. (Plain detach is what tab-close does naturally.)
 */

import { getToken } from "./auth"
import { ApiError, apiFetch, ensureFreshAccess } from "./client"
import type { FunctionResponseArgs, RunArgs, RunEvent, StreamCallbacks } from "./sse"

export interface TurnError {
  type: string
  message: string
  rate_limited: boolean
  kind?: "burst" | "upstream" | "quota"
  reset_hint_s?: number | null
}

export interface TurnSnapshot {
  turn_id: string
  status: "running" | "done" | "error" | "aborted"
  cursor: number
  model_events: number
  session_id: string
  error: TurnError | null
}

/** Error subclass carrying the broker's classified terminal payload, so the
 * UI can render "Retry" (rate-limited) vs a plain failure notice. */
export class TurnFailedError extends Error {
  turnError: TurnError
  constructor(te: TurnError) {
    super(te.message || te.type)
    this.turnError = te
  }
}

export async function latestTurn(
  appName: string, userId: string, sessionId: string,
): Promise<TurnSnapshot | null> {
  try {
    return await apiFetch<TurnSnapshot>(
      `/api/turns/latest?appName=${encodeURIComponent(appName)}&userId=${encodeURIComponent(userId)}&sessionId=${encodeURIComponent(sessionId)}`,
    )
  } catch {
    return null // 404 (no turn / broker absent) → caller treats as "nothing running"
  }
}

export async function abortTurnById(turnId: string): Promise<void> {
  await apiFetch(`/api/turns/${encodeURIComponent(turnId)}/abort`, { method: "POST" })
}

export async function retryLastTurn(
  appName: string, userId: string, sessionId: string,
): Promise<TurnSnapshot> {
  return apiFetch<TurnSnapshot>(`/api/turns/retry-last`, {
    method: "POST",
    body: JSON.stringify({ appName, userId, sessionId }),
  })
}

async function _startTurn(
  args: { appName: string; userId: string; sessionId: string },
  newMessage: unknown,
): Promise<TurnSnapshot> {
  // 409 = single-flight busy. The visible turn can END (final reply, or a
  // confirmation card) while its server-side task briefly lives on for
  // post-turn work (e.g. the out-of-band session-title call) — a user who
  // answers a confirmation the moment the card appears lands in that window.
  // Briefly retry instead of surfacing a spurious "busy" error.
  for (let attempt = 0; ; attempt++) {
    try {
      return await apiFetch<TurnSnapshot>(`/api/turns`, {
        method: "POST",
        body: JSON.stringify({ ...args, newMessage }),
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && attempt < 20) {
        await new Promise((r) => setTimeout(r, 500))
        continue
      }
      throw e
    }
  }
}

/** Tail a turn's SSE stream from `cursor`. Resolves when the stream ends. */
async function _tailLoop(
  turnId: string,
  cursor: number,
  cb: StreamCallbacks,
  signal: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { Accept: "text/event-stream" }
  await ensureFreshAccess()
  const token = getToken()
  if (token) headers["Authorization"] = `Bearer ${token}`

  try {
    const resp = await fetch(
      `/api/turns/${encodeURIComponent(turnId)}/stream?cursor=${cursor}`,
      { headers, signal },
    )
    if (!resp.ok || !resp.body) {
      throw new Error(`turn stream returned ${resp.status}`)
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const messages = buffer.split("\n\n")
      buffer = messages.pop() ?? ""
      for (const message of messages) {
        const trimmed = message.trim()
        if (!trimmed || trimmed.startsWith(":")) continue // keepalive comment
        let eventName = "message"
        let data = ""
        for (const line of trimmed.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim()
          else if (line.startsWith("data:")) data = line.slice(5).trim()
        }
        if (!data) continue
        if (eventName === "turn_end") {
          const end = JSON.parse(data) as { status: string; error: TurnError | null }
          if (end.status === "error" && end.error) {
            cb.onError?.(new TurnFailedError(end.error))
          }
          continue // onClose follows when the stream drains
        }
        try {
          cb.onEvent(JSON.parse(data) as RunEvent)
        } catch {
          /* tolerate malformed lines */
        }
      }
    }
    cb.onClose?.()
  } catch (e) {
    if ((e as Error).name === "AbortError") {
      cb.onClose?.()
      return
    }
    cb.onError?.(e as Error)
    cb.onClose?.()
  }
}

function _run(
  args: { appName: string; userId: string; sessionId: string },
  newMessage: unknown,
  cb: StreamCallbacks,
): () => void {
  const ctrl = new AbortController()
  let turnId: string | null = null
  void (async () => {
    try {
      const snap = await _startTurn(args, newMessage)
      turnId = snap.turn_id
      await _tailLoop(snap.turn_id, 0, cb, ctrl.signal)
    } catch (e) {
      cb.onError?.(e as Error)
      cb.onClose?.()
    }
  })()
  return () => {
    // FULL abort: stop the server-side turn, then drop the tail.
    if (turnId) void abortTurnById(turnId).catch(() => {})
    ctrl.abort()
  }
}

/** Start a durable turn from a plain user message. */
export function streamTurnRun(args: RunArgs, cb: StreamCallbacks): () => void {
  return _run(args, { role: "user", parts: [{ text: args.message }] }, cb)
}

/** Start a durable turn from a function response (confirmations etc.). */
export function streamTurnFunctionResponse(
  args: FunctionResponseArgs,
  cb: StreamCallbacks,
): () => void {
  return _run(
    args,
    {
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
    },
    cb,
  )
}

/** Re-attach to an EXISTING turn (reconnect-on-mount / after retry-last).
 * The abort function here only detaches when the turn already ended. */
export function streamExistingTurn(
  turnId: string,
  cursor: number,
  cb: StreamCallbacks,
): () => void {
  const ctrl = new AbortController()
  void _tailLoop(turnId, cursor, cb, ctrl.signal)
  return () => {
    void abortTurnById(turnId).catch(() => {})
    ctrl.abort()
  }
}
