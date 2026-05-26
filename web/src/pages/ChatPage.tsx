import { useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { clearToken, getUser } from "@/api/auth"
import { getSession, type Session } from "@/api/sessions"
import {
  streamRun,
  streamFunctionResponse,
  type RunEvent,
} from "@/api/sse"
import { SessionRail } from "@/components/SessionRail"
import { Thread } from "@/components/Thread"
import { Composer } from "@/components/Composer"
import { PlanModeBanner } from "@/components/PlanModeBanner"
import { TaskSidebar } from "@/components/TaskSidebar"

/**
 * Three-pane layout: rail (apps + sessions) | thread (messages) |
 * tasks (right rail, conditionally rendered). The rail owns its own
 * data fetching; ChatPage owns the currently-displayed session and
 * the in-flight SSE stream.
 *
 * Event sources merged into one rendered list:
 *   1. Session.events loaded on selection — historical truth.
 *   2. Live events arriving over SSE while a turn is running.
 * Both feed into `events`, which Thread renders linearly. When the
 * turn ends we re-GET the session so the canonical event ids/timestamps
 * replace the optimistic in-memory ones AND the session.state
 * (notably permission_mode) reflects whatever the agent's tools just did.
 */
export function ChatPage() {
  const userId = getUser()
  const [appName, setAppName] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<(() => void) | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // When the selected session changes, fetch its full event log + state.
  useEffect(() => {
    if (!appName || !session) {
      setEvents([])
      return
    }
    let cancelled = false
    getSession(appName, userId, session.id)
      .then((s) => {
        if (cancelled) return
        setEvents(s.events)
        // Refresh local Session reference so state.permission_mode etc.
        // stay current even when the rail's cached row is stale.
        setSession(s)
      })
      .catch((e) => {
        if (!cancelled) setError(`Failed to load session: ${e.message}`)
      })
    return () => {
      cancelled = true
    }
  }, [appName, userId, session?.id])

  // Auto-scroll to bottom when events grow.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [events, isStreaming])

  function attachStream(open: () => () => void) {
    setIsStreaming(true)
    abortRef.current = open()
  }

  function refreshAfterTurn() {
    if (!appName || !session) return
    getSession(appName, userId, session.id)
      .then((s) => {
        setEvents(s.events)
        setSession(s)
      })
      .catch(() => {
        /* keep optimistic if reload fails */
      })
    setRefreshTick((t) => t + 1)
  }

  function handleSend(text: string) {
    if (!appName || !session) return
    setError(null)

    // Optimistic user-message append so the bubble shows immediately
    // (before the SSE stream echoes it back).
    const optimistic: RunEvent = {
      id: `optimistic-${Date.now()}`,
      author: "user",
      content: { role: "user", parts: [{ text }] },
    }
    setEvents((prev) => [...prev, optimistic])

    attachStream(() =>
      streamRun(
        { appName, userId, sessionId: session.id, message: text },
        {
          onEvent: (e) => setEvents((prev) => [...prev, e]),
          onError: (err) => {
            setError(err.message)
            setIsStreaming(false)
          },
          onClose: () => {
            setIsStreaming(false)
            abortRef.current = null
            refreshAfterTurn()
          },
        },
      ),
    )
  }

  function handleSubmitFunctionResponse(
    callId: string,
    toolName: string,
    response: unknown,
  ) {
    if (!appName || !session) return
    setError(null)
    // Optimistic function_response so the widget hides immediately
    // and the user gets visible feedback. The canonical event lands
    // after refreshAfterTurn().
    const optimistic: RunEvent = {
      id: `optimistic-${Date.now()}`,
      author: "user",
      content: {
        role: "user",
        parts: [
          {
            function_response: { id: callId, name: toolName, response },
          },
        ],
      },
    }
    setEvents((prev) => [...prev, optimistic])

    attachStream(() =>
      streamFunctionResponse(
        {
          appName,
          userId,
          sessionId: session.id,
          callId,
          toolName,
          response,
        },
        {
          onEvent: (e) => setEvents((prev) => [...prev, e]),
          onError: (err) => {
            setError(err.message)
            setIsStreaming(false)
          },
          onClose: () => {
            setIsStreaming(false)
            abortRef.current = null
            refreshAfterTurn()
          },
        },
      ),
    )
  }

  function handleAbort() {
    abortRef.current?.()
    abortRef.current = null
    setIsStreaming(false)
  }

  const permissionMode =
    typeof session?.state?.permission_mode === "string"
      ? (session.state.permission_mode as string)
      : undefined

  return (
    <div className="flex h-screen">
      <SessionRail
        userId={userId}
        appName={appName}
        onAppChange={(a) => {
          setAppName(a)
          setSession(null)
        }}
        sessionId={session?.id ?? null}
        onSelect={(s) => setSession(s)}
        refreshTick={refreshTick}
      />
      <div className="flex flex-1 flex-col min-w-0">
        <header className="flex items-center justify-between border-b px-6 py-3">
          <div className="flex items-center gap-3 min-w-0">
            <span className="text-lg font-semibold tracking-tight">
              adk-cc
            </span>
            {session && (
              <span className="text-xs font-mono text-muted-foreground truncate">
                {session.id}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              Signed in as <span className="font-mono">{userId}</span>
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                clearToken()
                location.reload()
              }}
            >
              Sign out
            </Button>
          </div>
        </header>
        <PlanModeBanner mode={permissionMode} />
        {error && (
          <div className="border-b bg-destructive/10 px-6 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          {session ? (
            <Thread
              events={events}
              isStreaming={isStreaming}
              onSubmitFunctionResponse={handleSubmitFunctionResponse}
            />
          ) : (
            <div className="flex h-full items-center justify-center p-12">
              <p className="max-w-md text-center text-sm text-muted-foreground">
                Pick a session from the left rail or click{" "}
                <span className="font-mono">+ New</span> to start one.
              </p>
            </div>
          )}
        </div>
        <Composer
          onSend={handleSend}
          onAbort={handleAbort}
          isStreaming={isStreaming}
          disabled={!session}
        />
      </div>
      {session && <TaskSidebar events={events} />}
    </div>
  )
}
