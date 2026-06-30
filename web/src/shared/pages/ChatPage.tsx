import { useEffect, useMemo, useRef, useState, type ComponentType } from "react"
import { Menu, ListChecks } from "lucide-react"
import { Button } from "@/shared/components/ui/button"
import { clearToken, getUser, getToken, decodeJwtPayload, markSignedOut } from "@/shared/api/auth"
import {
  createSession,
  getSession,
  patchSessionState,
  type Session,
} from "@/shared/api/sessions"
import {
  streamRun,
  streamFunctionResponse,
  type RunEvent,
} from "@/shared/api/sse"
import { SessionRail, type RailProps } from "@/shared/components/SessionRail"
import { Thread } from "@/shared/components/Thread"
import { Composer } from "@/shared/components/Composer"
import { TaskSidebar, deriveTasks } from "@/shared/components/TaskSidebar"
import { ArtifactsPanel } from "@/shared/components/ArtifactsPanel"
import { ContextGauge } from "@/shared/components/ContextGauge"
import { CompactionBadge } from "@/shared/components/CompactionBadge"
import { fetchContextLimits, type ContextLimits } from "@/shared/api/context"
import { SettingsModal } from "@/shared/components/SettingsModal"
import { listSecrets } from "@/shared/api/account"
import { IS_DESKTOP } from "@/shared/lib/platform"
import { type SlashAction } from "@/shared/components/SlashCommandMenu"
import { getStoredTheme, setStoredTheme, type ThemeMode } from "@/shared/lib/theme"

/**
 * Three-pane layout: rail (apps + sessions) | thread (messages) |
 * tasks (right rail, conditionally rendered). The rail owns its own
 * data fetching; ChatPage owns the currently-displayed session and
 * the in-flight SSE stream.
 *
 * Responsive: at lg+ all three panes sit side by side. Below lg the two
 * side rails become slide-in drawers (toggled from the header) so the
 * thread gets the full width on phones/tablets.
 *
 * Event sources merged into one rendered list:
 *   1. Session.events loaded on selection — historical truth.
 *   2. Live events arriving over SSE while a turn is running.
 * Both feed into `events`, which Thread renders linearly. When the
 * turn ends we re-GET the session so the canonical event ids/timestamps
 * replace the optimistic in-memory ones AND the session.state
 * (notably permission_mode) reflects whatever the agent's tools just did.
 */
/** The platform shells inject their own rail + settings; both default to the
 *  shared web implementations so the web build is unchanged. */
export type SettingsModalProps = { open: boolean; onClose: () => void }

export function ChatPage({
  Rail = SessionRail,
  Settings = SettingsModal,
}: {
  Rail?: ComponentType<RailProps>
  Settings?: ComponentType<SettingsModalProps>
} = {}) {
  // Stateful so the desktop rail can switch the active user_id (= project);
  // the web rail never calls setUserId, so web keeps a fixed account id.
  const [userId, setUserId] = useState(getUser())
  // Friendly display label — email/name from the token, NOT the opaque user_id
  // (which is what `userId` holds and is used for the API session path).
  const userLabel = (() => {
    const p = decodeJwtPayload(getToken() ?? "")
    return (p?.email as string) || (p?.name as string) || userId
  })()
  const [appName, setAppName] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [refreshTick, setRefreshTick] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // Count of required skill/MCP secrets the user hasn't set → badge on the
  // Settings gear. Refreshed on mount and whenever the Settings dialog closes
  // (the user may have just set some on the Account page).
  const [secretsMissing, setSecretsMissing] = useState(0)
  useEffect(() => {
    // Desktop has no /auth/secrets (no identity provider); its Secrets tab manages
    // secrets directly, so skip the web-only needs-setup badge probe.
    if (settingsOpen || IS_DESKTOP) return
    listSecrets()
      .then((v) => setSecretsMissing(v.missing_required))
      .catch(() => {})
  }, [settingsOpen])
  // Mobile drawer state (no effect at lg+, where the rails are static).
  const [railOpen, setRailOpen] = useState(false)
  const [tasksOpen, setTasksOpen] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Whether there are any tasks — drives the header's mobile tasks
  // toggle (the right rail itself renders nothing when empty).
  const taskCount = useMemo(() => deriveTasks(events).length, [events])

  // Context-fullness gauge (P2): server ladder fetched once; current usage =
  // the latest reported prompt_token_count across the loaded events.
  const [ctxLimits, setCtxLimits] = useState<ContextLimits | null>(null)
  useEffect(() => {
    fetchContextLimits().then(setCtxLimits).catch(() => setCtxLimits(null))
  }, [])
  const ctxTokens = useMemo(() => {
    let n = 0
    for (const e of events) {
      const um = (e as { usageMetadata?: { promptTokenCount?: number } }).usageMetadata
      if (typeof um?.promptTokenCount === "number") n = um.promptTokenCount
    }
    return n
  }, [events])
  // Compaction history (P3): count + last end-timestamp, live from the stream.
  const compactions = useMemo(() => {
    let count = 0
    let lastEndTs: number | undefined
    for (const e of events) {
      const c = (e as { actions?: { compaction?: { endTimestamp?: number } } })
        .actions?.compaction
      if (c) {
        count++
        if (typeof c.endTimestamp === "number") lastEndTs = c.endTimestamp
      }
    }
    return { count, lastEndTs }
  }, [events])

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
            functionResponse: { id: callId, name: toolName, response },
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

  function handleSlashAction(action: SlashAction) {
    switch (action) {
      case "help":
        // No backend protocol — we just send a plain user message
        // listing available shortcuts. Cheap, no schema.
        if (appName && session) {
          handleSend(
            "Available slash commands: /help, /clear (new session), " +
              "/plan, /exit-plan, /theme, /settings, /signout. " +
              "These are UI shortcuts on the client; the agent doesn't see them.",
          )
        }
        return
      case "clear":
        if (!appName) return
        createSession(appName, userId, {})
          .then((s) => {
            setSession(s)
            setEvents([])
            setRefreshTick((t) => t + 1)
          })
          .catch((e) =>
            setError(`Failed to create new session: ${(e as Error).message}`),
          )
        return
      case "settings":
        setSettingsOpen(true)
        return
      case "theme": {
        // Cycle: light → dark → system → light. Persisted by setStoredTheme.
        const cur = getStoredTheme()
        const next: ThemeMode =
          cur === "light" ? "dark" : cur === "dark" ? "system" : "light"
        setStoredTheme(next)
        return
      }
      case "signout":
        markSignedOut()
        clearToken()
        location.assign("/")
        return
      case "plan":
      case "exit-plan": {
        // Direct state mutation — no LLM turn. ADK appends a synthetic
        // state-update Event so the change shows up in session.events
        // and (importantly) in session.state.permission_mode for the
        // next tool call. Values match adk_cc/permissions/modes.py:
        // PLAN="plan", DEFAULT="default".
        if (!appName || !session) return
        const next = action === "plan" ? "plan" : "default"
        patchSessionState(appName, userId, session.id, {
          permission_mode: next,
        })
          .then((s) => {
            setSession(s)
            setEvents(s.events)
            setRefreshTick((t) => t + 1)
          })
          .catch((e) =>
            setError(
              `Failed to switch permission mode: ${(e as Error).message}`,
            ),
          )
        return
      }
    }
  }

  const permissionMode =
    typeof session?.state?.permission_mode === "string"
      ? (session.state.permission_mode as string)
      : undefined

  return (
    <div className="flex h-screen overflow-hidden">
      <Rail
        userId={userId}
        setUserId={setUserId}
        appName={appName}
        onAppChange={(a) => {
          setAppName(a)
          setSession(null)
        }}
        sessionId={session?.id ?? null}
        onSelect={(s) => {
          setSession(s)
          setRailOpen(false) // dismiss the mobile drawer after picking
        }}
        refreshTick={refreshTick}
        open={railOpen}
        onClose={() => setRailOpen(false)}
        userLabel={userLabel}
        onOpenSettings={() => setSettingsOpen(true)}
        secretsMissing={secretsMissing}
      />
      <div className="flex flex-1 flex-col min-w-0">
        <header className="flex items-center justify-between gap-2 px-3 sm:px-6 py-3 border-b border-border/60">
          <div className="flex items-center gap-2 sm:gap-3 min-w-0">
            {/* Mobile: open the session rail. */}
            <Button
              variant="outline"
              size="icon"
              className="lg:hidden shrink-0"
              onClick={() => setRailOpen(true)}
              title="Sessions"
            >
              <Menu className="h-4 w-4" />
            </Button>
            <span className="text-lg font-semibold tracking-tight shrink-0">
              adk-cc
            </span>
            {session && (
              <span className="hidden sm:inline text-xs font-mono text-muted-foreground truncate">
                {session.id}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 sm:gap-3 shrink-0">
            {session && (
              <CompactionBadge
                count={compactions.count}
                lastEndTs={compactions.lastEndTs}
              />
            )}
            {session && <ContextGauge current={ctxTokens} limits={ctxLimits} />}
            {appName && session && (
              <ArtifactsPanel
                appName={appName}
                userId={userId}
                sessionId={session.id}
              />
            )}
            {/* Mobile: open the tasks drawer (only when there are tasks). */}
            {session && taskCount > 0 && (
              <Button
                variant="outline"
                size="icon"
                className="lg:hidden relative"
                onClick={() => setTasksOpen(true)}
                title="Tasks"
              >
                <ListChecks className="h-4 w-4" />
                <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[9px] font-medium text-primary-foreground">
                  {taskCount}
                </span>
              </Button>
            )}
          </div>
        </header>
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
              appName={appName ?? ""}
              userId={userId}
              sessionId={session.id}
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
          onSlashAction={handleSlashAction}
          isStreaming={isStreaming}
          disabled={!session}
          mode={permissionMode}
        />
      </div>
      {session && (
        <TaskSidebar
          events={events}
          open={tasksOpen}
          onClose={() => setTasksOpen(false)}
        />
      )}
      <Settings
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  )
}
