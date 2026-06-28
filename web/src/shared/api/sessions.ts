/**
 * Typed client for ADK's session REST surface.
 *
 * Routes (all served by google.adk.cli.adk_web_server):
 *   GET    /list-apps                                                   → string[]
 *   GET    /apps/{app}/users/{user}/sessions                            → Session[]
 *   POST   /apps/{app}/users/{user}/sessions      body=CreateSessionRequest  → Session
 *   GET    /apps/{app}/users/{user}/sessions/{id}                       → Session
 *   DELETE /apps/{app}/users/{user}/sessions/{id}                       → 204
 *
 * Pydantic models on the server side use alias_generator=to_camel, so
 * Session fields arrive as appName/userId/lastUpdateTime/etc. The same
 * camelCase shape is what we type here.
 */

import { apiFetch } from "./client"
import type { RunEvent } from "./sse"

/** An ADK Session as serialized by FastAPI. */
export interface Session {
  id: string
  appName: string
  userId: string
  state: Record<string, unknown>
  /** Events recorded so far on this session (chronological). */
  events: RunEvent[]
  lastUpdateTime: number
}

/** Request body for POST /apps/.../sessions. All fields optional —
 * server generates an id and starts with empty state if omitted. */
export interface CreateSessionRequest {
  sessionId?: string
  state?: Record<string, unknown>
  events?: RunEvent[]
}

export async function listApps(): Promise<string[]> {
  return apiFetch<string[]>("/list-apps")
}

export async function listSessions(
  appName: string,
  userId: string,
): Promise<Session[]> {
  return apiFetch<Session[]>(
    `/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`,
  )
}

export async function getSession(
  appName: string,
  userId: string,
  sessionId: string,
): Promise<Session> {
  return apiFetch<Session>(
    `/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`,
  )
}

export async function createSession(
  appName: string,
  userId: string,
  body: CreateSessionRequest = {},
): Promise<Session> {
  return apiFetch<Session>(
    `/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  )
}

/** Mutate session state without running the agent. ADK exposes this
 * via `PATCH /apps/.../sessions/{id}` taking a `state_delta` body —
 * the server appends a synthetic state-update Event under the hood.
 * Used by slash commands like `/plan` to flip `permission_mode`
 * directly instead of asking the LLM to do it through a tool call. */
export async function patchSessionState(
  appName: string,
  userId: string,
  sessionId: string,
  stateDelta: Record<string, unknown>,
): Promise<Session> {
  return apiFetch<Session>(
    `/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ stateDelta }),
    },
  )
}

export async function deleteSession(
  appName: string,
  userId: string,
  sessionId: string,
): Promise<void> {
  await apiFetch<void>(
    `/apps/${encodeURIComponent(appName)}/users/${encodeURIComponent(userId)}/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  )
}
