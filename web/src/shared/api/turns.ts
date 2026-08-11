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
  /** Wall-clock of the running turn, and the tool currently in flight —
   * without these a 30-minute turn of real work (browser automation, model
   * generation) is indistinguishable from a hang. */
  elapsed_s?: number
  current_tool?: string
  current_tool_elapsed_s?: number
  /** Present while the model call is sleeping out a rate limit — the
   * otherwise-invisible backoff, surfaced as a countdown. */
  model_status?: {
    state: "rate_limited"
    model: string
    attempt: number
    of: number
    reason: string
    resume_in_s: number
  }
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

export interface CompactResult {
  status: "summarized" | "mechanical" | "nothing_to_compact" | "failed"
  before_tokens?: number
  after_tokens?: number
  compacted_events?: number
  guided?: boolean
}

/** #128 guided /compact — manual compaction of a quiescent session. The
 * optional guide biases what the summary keeps ("keep #127, drop 125").
 * Server 409s while a turn is running. */
export async function manualCompact(
  appName: string, userId: string, sessionId: string, guide?: string,
): Promise<CompactResult> {
  return apiFetch<CompactResult>(`/api/compact`, {
    method: "POST",
    body: JSON.stringify({ appName, userId, sessionId, guide: guide ?? "" }),
  })
}

async function _startTurn(
  args: { appName: string; userId: string; sessionId: string },
  newMessage: unknown,
): Promise<TurnSnapshot> {
  // 409 = single-flight busy. The visible turn can END (final reply, or a
  // confirmation card) while its server-side task lives on — post-turn work,
  // or a tool call that is simply still going. A user who answers a
  // confirmation the moment the card appears lands in that window.
  //
  // This used to give up after a fixed 20 x 500ms. Ten seconds is fine for a
  // title call and far too short for a real tool: a skill script can hold the
  // turn for minutes, and when the budget ran out the user's answer was not
  // delayed, it was LOST — surfaced as a bare "409 conflict" with no way to
  // resend. So: keep waiting as long as the server says a turn is genuinely
  // running for this session, and only give up when it is not.
  const deadline = Date.now() + _BUSY_MAX_WAIT_MS
  for (let sawIdle = 0; ; ) {
    try {
      return await apiFetch<TurnSnapshot>(`/api/turns`, {
        method: "POST",
        body: JSON.stringify({ ...args, newMessage }),
      })
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409)) throw e
      if (Date.now() > deadline) throw e
      // Is something actually running, or is the 409 stale? Only the second
      // case should ever give up — and only after a few tries, since the
      // status and the reservation are read at slightly different moments.
      const t = await latestTurn(args.appName, args.userId, args.sessionId)
        .catch(() => null)
      if (t && t.status === "running") {
        sawIdle = 0
      } else if (++sawIdle > 6) {
        throw e
      }
      await new Promise((r) => setTimeout(r, 500))
    }
  }
}

/** Upper bound on waiting out a busy session. Not a tool's runtime limit —
 *  just a backstop so a wedged turn cannot hang the composer forever. */
const _BUSY_MAX_WAIT_MS = 20 * 60 * 1000

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

/** Ending a stream means one of two different things.
 *
 *  Stop  = the user wants the WORK to end → abort the server-side turn.
 *  Detach = we are just looking away (switching sessions, unmounting) → drop
 *  our tail and leave the turn running. Turns are durable precisely so this
 *  is possible; conflating the two silently killed a running turn whenever
 *  the user opened another session. */
export type CancelStream = (opts?: { detachOnly?: boolean }) => void

function _run(
  args: { appName: string; userId: string; sessionId: string },
  newMessage: unknown,
  cb: StreamCallbacks,
): CancelStream {
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
  return (opts) => {
    if (!opts?.detachOnly && turnId) void abortTurnById(turnId).catch(() => {})
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
): CancelStream {
  const ctrl = new AbortController()
  void _tailLoop(turnId, cursor, cb, ctrl.signal)
  return (opts) => {
    if (!opts?.detachOnly) void abortTurnById(turnId).catch(() => {})
    ctrl.abort()
  }
}
