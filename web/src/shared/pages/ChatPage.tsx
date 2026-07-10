import { useEffect, useMemo, useRef, useState, type ComponentType } from "react"
import { useNavigate } from "react-router-dom"
import { Menu, PanelRight } from "lucide-react"
import { Button } from "@/shared/components/ui/button"
import { clearToken, getUser, getToken, decodeJwtPayload, markSignedOut } from "@/shared/api/auth"
import { revokeSession } from "@/shared/api/identity"
import {
  createSession,
  getSession,
  patchSessionState,
  type Session,
} from "@/shared/api/sessions"
import {
  streamRun,
  streamFunctionResponse,
  isFinalResponse,
  type RunEvent,
  type StreamCallbacks,
} from "@/shared/api/sse"
import { SessionRail, type RailProps } from "@/shared/components/SessionRail"
import { Thread } from "@/shared/components/Thread"
import { Composer } from "@/shared/components/Composer"
import { TaskStrip } from "@/shared/components/TaskStrip"
import { ArtifactsSidePanel } from "@/shared/components/ArtifactsSidePanel"
import { type RightPanelProps } from "@/shared/components/RightPanelShell"
import { ContextGauge } from "@/shared/components/ContextGauge"
import { sessionTitle } from "@/shared/sessions/SessionList"
import { CompactionBadge } from "@/shared/components/CompactionBadge"
import { fetchContextLimits, type ContextLimits } from "@/shared/api/context"
import { SettingsModal } from "@/shared/components/SettingsModal"
import { listSecrets } from "@/shared/api/account"
import { IS_DESKTOP } from "@/shared/lib/platform"
import { pickDirectory } from "@/shared/lib/tauri"
import { addWorkingDir } from "@/shared/api/desktop-settings"
import { type SlashAction } from "@/shared/components/SlashCommandMenu"
import { RewindDialog } from "@/shared/components/RewindDialog"
import { ModelPicker } from "@/shared/components/ModelPicker"
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
  RightPanel = ArtifactsSidePanel,
}: {
  Rail?: ComponentType<RailProps>
  Settings?: ComponentType<SettingsModalProps>
  RightPanel?: ComponentType<RightPanelProps>
} = {}) {
  const navigate = useNavigate()
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
  // Neutral transient confirmation channel (e.g. /add-dir), distinct from the
  // destructive-styled `error` banner.
  const [notice, setNotice] = useState<string | null>(null)
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (noticeTimer.current) clearTimeout(noticeTimer.current) }, [])
  const showNotice = (msg: string) => {
    setNotice(msg)
    if (noticeTimer.current) clearTimeout(noticeTimer.current)
    noticeTimer.current = setTimeout(() => setNotice(null), 6000)
  }
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [rewindOpen, setRewindOpen] = useState(false)
  const [modelPickerOpen, setModelPickerOpen] = useState(false)
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
  // Right-side panel (artifacts on web / file tree on desktop) mobile drawer.
  const [rightPanelOpen, setRightPanelOpen] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)
  // Monotonic per-turn id. Because the "working" indicator now clears on the
  // in-band final-response event (not the socket close), the composer re-enables
  // while the previous stream may still be draining its silent post-turn tail —
  // so a stale stream's late onEvent/onClose must not disturb a newer turn.
  const streamGen = useRef(0)
  const scrollRef = useRef<HTMLDivElement>(null)
  // One-shot timer: re-poll the rail a beat after a turn so a late-persisted
  // session title (generated out-of-band, detached from the turn) shows up.
  const titlePollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (titlePollRef.current) clearTimeout(titlePollRef.current) }, [])

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

  // Open a stream and own all of its state transitions. `make` receives the
  // guarded callbacks and returns the stream's abort fn. The "working" indicator
  // tracks the AGENT'S actual work: it clears on the turn's final-response event
  // (the in-band stop signal) rather than waiting for the socket to close — which
  // lags behind by any silent post-turn work (e.g. the session-title model call).
  // Every callback is fenced by the turn's `gen`, so a prior stream finishing its
  // tail can't stomp a newer turn.
  function attachStream(make: (cb: StreamCallbacks) => () => void) {
    const gen = ++streamGen.current
    setIsStreaming(true)
    abortRef.current = make({
      onEvent: (e) => {
        if (gen !== streamGen.current) return
        setEvents((prev) => [...prev, e])
        // Final response → the reply is done (or the agent is now waiting on the
        // user). Re-arm on any later non-final event (multi-agent turns emit a
        // final response per sub-agent before control returns to the coordinator).
        const final = isFinalResponse(e)
        setIsStreaming(!final)
        // Refresh the right panel (file tree + Undo/History availability) NOW, when
        // the reply lands — not at socket close, which lags by the silent title
        // tail. The turn's checkpoint was already taken mid-turn, so without this
        // the Undo button stays disabled for the several-second gap between the
        // reply finishing and the stream actually closing.
        if (final) setRefreshTick((t) => t + 1)
      },
      onError: (err) => {
        if (gen !== streamGen.current) return
        setError(err.message)
        setIsStreaming(false)
      },
      onClose: () => {
        if (gen !== streamGen.current) return
        setIsStreaming(false)
        abortRef.current = null
        refreshAfterTurn()
      },
    })
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
    // The title lands out-of-band, possibly after the stream closed → poll once more.
    if (titlePollRef.current) clearTimeout(titlePollRef.current)
    titlePollRef.current = setTimeout(() => setRefreshTick((t) => t + 1), 2500)
  }

  // Reload the thread from the server — used after a rewind, which truncates the
  // session's events (conversation) to match the file restore. Without this the
  // chat would keep showing the messages from the reverted turn(s).
  function reloadSession() {
    if (!appName || !session) return
    getSession(appName, userId, session.id)
      .then((s) => {
        setEvents(s.events)
        setSession(s)
      })
      .catch(() => {})
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

    attachStream((cb) =>
      streamRun({ appName, userId, sessionId: session.id, message: text }, cb),
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

    attachStream((cb) =>
      streamFunctionResponse(
        { appName, userId, sessionId: session.id, callId, toolName, response },
        cb,
      ),
    )
  }

  function handleAbort() {
    streamGen.current++ // fence: ignore any late callbacks from the aborted stream
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
              "/plan, /exit-plan, /theme, /settings, /signout, " +
              "/wiki (open the knowledge graph — wiki pages + memory)" +
              (IS_DESKTOP ? ", /model (switch the active model across providers), /rewind (rewind to a checkpoint — roll back files + conversation), /add-dir (grant a working directory outside the project)" : "") +
              ". These are UI shortcuts on the client; the agent doesn't see them.",
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
      case "wiki":
        // UI shortcut to the knowledge-graph view (wiki pages + memory). Desktop
        // scopes memory to the current project via ?user=; web omits it (the
        // authenticated principal decides server-side).
        navigate(
          IS_DESKTOP && userId
            ? `/knowledge?user=${encodeURIComponent(userId)}`
            : "/knowledge",
        )
        return
      case "add-dir":
        // Desktop: grant the agent a persistent working directory outside the
        // project (native folder picker → POST). The dir is folded into the
        // sandbox scope for this project's sessions.
        if (!IS_DESKTOP || !userId) return
        void (async () => {
          const path = await pickDirectory()
          if (path === undefined) {
            // No native picker here (plain browser) — direct to the Settings tab.
            setError("Add a working directory from Settings → Working dirs.")
            return
          }
          if (!path) return // null = user cancelled the native dialog
          try {
            await addWorkingDir(path, userId)
            showNotice(`✓ Working directory added: ${path}`)
          } catch (e) {
            setError(`Failed to add working directory: ${(e as Error).message}`)
          }
        })()
        return
      case "rewind":
        // Desktop-only: open the multi-step picker to rewind to any checkpoint —
        // rolls back both the project files AND the conversation to that turn.
        if (!IS_DESKTOP || !appName || !session) return
        setRewindOpen(true)
        return
      case "model":
        // Desktop-only: open the model palette to switch the active model across
        // providers (takes effect on the next turn — SelectableLlm re-reads).
        if (!IS_DESKTOP) return
        setModelPickerOpen(true)
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
        revokeSession()
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
      <div className="adk-chat flex flex-1 flex-col min-w-0">
        <header className="adk-chat-header flex items-center justify-between gap-2 px-3 sm:px-6 py-3">
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
            {session && (
              <span className="adk-chat-title text-base font-semibold tracking-tight truncate">
                {sessionTitle(session) ?? "New Chat"}
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
            {/* Mobile: open the right-side panel (artifacts on web, files on
                desktop). Static column at lg+, so this toggle is lg:hidden. */}
            {appName && session && (
              <Button
                variant="outline"
                size="icon"
                className="lg:hidden"
                onClick={() => setRightPanelOpen(true)}
                title="Files & artifacts"
              >
                <PanelRight className="h-4 w-4" />
              </Button>
            )}
          </div>
        </header>
        {error && (
          <div className="border-b bg-destructive/10 px-6 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
        {notice && (
          <div className="border-b bg-brand-tint px-6 py-2 text-sm text-muted-foreground">
            {notice}
          </div>
        )}
        <div className="adk-thread relative min-h-0 flex-1">
          <div ref={scrollRef} className="adk-thread-scroll h-full overflow-y-auto">
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
          {/* Soft fades (matching the Settings modal): content dissolves under the
              header at the top and toward the input at the bottom — no hard divider. */}
          <div className="adk-fade-top faded-header-edge pointer-events-none absolute inset-x-0 top-0 h-4" />
          <div className="adk-fade-bottom faded-top-edge pointer-events-none absolute inset-x-0 bottom-0 h-4" />
        </div>
        <Composer
          onSend={handleSend}
          onAbort={handleAbort}
          onSlashAction={handleSlashAction}
          isStreaming={isStreaming}
          disabled={!session}
          mode={permissionMode}
          footer={session ? <ContextGauge current={ctxTokens} limits={ctxLimits} /> : undefined}
          taskStrip={session ? <TaskStrip events={events} /> : undefined}
        />
      </div>
      {appName && session && (
        <RightPanel
          appName={appName}
          userId={userId}
          sessionId={session.id}
          open={rightPanelOpen}
          onClose={() => setRightPanelOpen(false)}
          refreshKey={refreshTick}
          onRestored={reloadSession}
        />
      )}
      <Settings
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
      {IS_DESKTOP && session && (
        <RewindDialog
          projectId={userId}
          sessionId={session.id}
          open={rewindOpen}
          onClose={() => setRewindOpen(false)}
          onRestored={reloadSession}
        />
      )}
      {IS_DESKTOP && modelPickerOpen && (
        <ModelPicker
          onClose={() => setModelPickerOpen(false)}
          onPicked={(label) => showNotice(`✓ Model switched to ${label} (next turn)`)}
        />
      )}
    </div>
  )
}
