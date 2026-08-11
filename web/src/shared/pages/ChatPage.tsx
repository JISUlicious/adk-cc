import { useEffect, useMemo, useRef, useState, type ComponentType } from "react"
import { useNavigate } from "react-router-dom"
import { Hash, Menu, PanelRight } from "lucide-react"
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
import {
  TurnFailedError,
  latestTurn,
  retryLastTurn,
  streamExistingTurn,
  type CancelStream,
  streamTurnFunctionResponse,
  streamTurnRun,
  type TurnError,
  type TurnSnapshot,
} from "@/shared/api/turns"
import { ModelChip } from "@/shared/components/ModelChip"
import { AnalysisEnvChip } from "@/shared/components/AnalysisEnvChip"
import { ProcessChip } from "@/shared/components/ProcessChip"
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
export type SettingsModalProps = {
  open: boolean
  onClose: () => void
  /** Tab to open on — `/skills` deep-links to the skill catalog. */
  initialTab?: string
}

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
  // Model-layer wait state (rate-limit backoff) from the turn snapshot —
  // rendered under the working indicator so a retry sleep is a visible
  // countdown, not a dead spinner (#99).
  const [modelStatus, setModelStatus] =
    useState<TurnSnapshot["model_status"] | null>(null)
  // Elapsed + in-flight tool (#105): measured live, a legitimate 30-minute
  // turn looked exactly like a hang.
  const [progress, setProgress] = useState<{
    elapsed_s?: number
    current_tool?: string
    current_tool_elapsed_s?: number
  } | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const [error, setError] = useState<string | null>(null)
  // Broker-classified terminal error of the last turn (rate-limited etc.) —
  // drives the Retry button on the error banner (F2b).
  const [turnError, setTurnError] = useState<TurnError | null>(null)
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
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined)
  const [rewindOpen, setRewindOpen] = useState(false)
  const [modelPickerOpen, setModelPickerOpen] = useState(false)
  // Bumped whenever the active model may have changed (palette pick, Settings
  // close) → the composer's ModelChip re-reads the registry.
  const [modelTick, setModelTick] = useState(0)
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
  const [notesOpen, setNotesOpen] = useState(false)
  const [showSessionId, setShowSessionId] = useState(
    () => localStorage.getItem("adk.showSessionIds") === "1",
  )
  const abortRef = useRef<CancelStream | null>(null)
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
    // Two readings, take the max. The model-reported usage is exact but goes
    // STALE when a burst of tool payloads lands after the last successful
    // call — the 2026-08-02 overflow showed 38% (stale usage) while the wire
    // carried ~145% of the window. The measured estimate mirrors the
    // server's estimate_events_tokens: payload-inclusive chars/4 over the
    // events a request would actually replay (respecting the latest
    // compaction range).
    let usage = 0
    let cutoff = 0
    let summaryChars = 0
    for (const e of events) {
      const um = (e as { usageMetadata?: { promptTokenCount?: number } }).usageMetadata
      if (typeof um?.promptTokenCount === "number") usage = um.promptTokenCount
      const comp = (e as { actions?: { compaction?: {
        endTimestamp?: number; compactedContent?: unknown } } }).actions?.compaction
      if (comp) {
        const end = comp.endTimestamp ?? 0
        if (end >= cutoff) {
          cutoff = end
          summaryChars = extractCompactionLength(comp.compactedContent)
        }
      }
    }
    let chars = summaryChars
    for (const e of events) {
      const ev = e as {
        timestamp?: number
        actions?: { compaction?: unknown }
        content?: { parts?: Array<{
          text?: string
          functionCall?: { args?: unknown }
          functionResponse?: { response?: unknown }
        }> }
      }
      if (ev.actions?.compaction) continue
      if (cutoff && (ev.timestamp ?? 0) <= cutoff) continue
      for (const p of ev.content?.parts ?? []) {
        if (p.text) chars += p.text.length
        if (p.functionCall?.args) chars += safeJsonLength(p.functionCall.args)
        if (p.functionResponse?.response) chars += safeJsonLength(p.functionResponse.response)
      }
    }
    return Math.max(usage, Math.floor(chars / 4))
  }, [events])
  function safeJsonLength(v: unknown): number {
    try {
      return JSON.stringify(v)?.length ?? 0
    } catch {
      return String(v).length
    }
  }

  function extractCompactionLength(content: unknown): number {
    if (typeof content === "string") return content.length
    const parts = (content as { parts?: Array<{ text?: string }> })?.parts
    if (Array.isArray(parts)) {
      return parts.reduce((n, p) => n + (p.text?.length ?? 0), 0)
    }
    return 0
  }

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
    // Leaving a session must end our attachment to ITS turn, BEFORE the new
    // one loads. Without this, switching to another session while one is
    // running left `isStreaming` true — so a brand-new session opened
    // mid-run showed "agent is working…" and refused input forever — and
    // left the old stream attached, appending the OTHER session's events
    // into this one's thread.
    //
    // Detaching is not aborting: the turn is durable server-side, so it
    // keeps running and re-attaches below (or on the next visit) via
    // latestTurn + streamExistingTurn. Only the client fetch is dropped.
    streamGen.current++            // fence: ignore late callbacks from it
    // detachOnly: looking away is not "stop". The turn is durable, so it
    // keeps running and we re-attach on return; a full abort here killed the
    // user's in-flight work every time they opened another session.
    abortRef.current?.({ detachOnly: true })
    abortRef.current = null
    setIsStreaming(false)
    setTurnError(null)

    let cancelled = false
    getSession(appName, userId, session.id)
      .then((s) => {
        if (cancelled) return
        setEvents(s.events)
        // Refresh local Session reference so state.permission_mode etc.
        // stay current even when the rail's cached row is stale.
        setSession(s)
        // Durable runs (F1): if this session has a turn STILL RUNNING
        // server-side (we refreshed / reopened mid-turn), re-attach its
        // tail — cursor 0 + id-dedupe stitches buffer and loaded events.
        void latestTurn(appName, userId, s.id).then((t) => {
          if (cancelled || !t || t.status !== "running") return
          setIsStreaming(true)
          attachStream((cb) => streamExistingTurn(t.turn_id, 0, cb))
        })
      })
      .catch((e) => {
        if (!cancelled) setError(`Failed to load session: ${e.message}`)
      })
    return () => {
      cancelled = true
    }
  }, [appName, userId, session?.id])

  // While a turn runs, poll its snapshot for model-layer wait state
  // (rate-limit backoff). Same bounded-poll pattern as the sub-agents dock:
  // only while streaming, cleared the moment the turn ends.
  useEffect(() => {
    if (!isStreaming || !appName || !session) {
      setModelStatus(null)
      setProgress(null)
      return
    }
    let cancelled = false
    const sid = session.id
    const tick = () => {
      void latestTurn(appName, userId, sid).then((t) => {
        if (cancelled) return
        setModelStatus(
          t && t.status === "running" ? t.model_status ?? null : null)
        setProgress(
          t && t.status === "running"
            ? {
                elapsed_s: t.elapsed_s,
                current_tool: t.current_tool,
                current_tool_elapsed_s: t.current_tool_elapsed_s,
              }
            : null)
      })
    }
    tick()
    const iv = setInterval(tick, 2000)   // the clock should feel live
    return () => {
      cancelled = true
      clearInterval(iv)
    }
  }, [isStreaming, appName, userId, session?.id])

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
  // F3 continuation lives SERVER-SIDE now: ADK resumability keeps the resumed
  // run rooted at the coordinator, and the Turn Broker auto-continues when a
  // handback lacks a coordinator reply — for every driver, not just this UI.
  // (The client-side "Continue." hack that lived here is retired.)

  function attachStream(make: (cb: StreamCallbacks) => () => void) {
    const gen = ++streamGen.current
    setIsStreaming(true)
    abortRef.current = make({
      onEvent: (e) => {
        if (gen !== streamGen.current) return
        // Dedupe by event id: a re-attached tail replays from a cursor and
        // may overlap events already loaded from the session.
        setEvents((prev) =>
          e.id && prev.some((x) => x.id === e.id) ? prev : [...prev, e])
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
        if (err instanceof TurnFailedError) setTurnError(err.turnError)
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

    setTurnError(null)
    attachStream((cb) =>
      streamTurnRun({ appName, userId, sessionId: session.id, message: text }, cb),
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

    setTurnError(null)
    attachStream((cb) =>
      streamTurnFunctionResponse(
        { appName, userId, sessionId: session.id, callId, toolName, response },
        cb,
      ),
    )
  }

  function handleRetryTurn() {
    if (!appName || !session) return
    setError(null)
    setTurnError(null)
    void retryLastTurn(appName, userId, session.id)
      .then((snap) =>
        attachStream((cb) => streamExistingTurn(snap.turn_id, 0, cb)),
      )
      .catch((e) => setError(`Retry failed: ${(e as Error).message}`))
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
              (IS_DESKTOP ? ", /model (pin a model for this session; default set in Settings), /rewind (rewind to a checkpoint — roll back files + conversation), /add-dir (grant a working directory outside the project)" : "") +
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
        setSettingsTab(undefined)
        setSettingsOpen(true)
        return
      case "skills":
        // 23 built-ins ship with the app and the only way to learn they exist
        // was the model calling list_skills mid-turn. `/skills` opens the
        // catalog — every discovered skill, what it is for, and its switch.
        setSettingsTab("skills")
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
        // Desktop-only: open the model palette to pin a model for THIS session
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
      case "notes":
        setNotesOpen(true)
        return
      case "mode-default":
      case "mode-acceptEdits":
      case "mode-bypass": {
        if (!appName || !session) return
        // The user's EXPLICIT posture for this session (#125). Also clears
        // any plan marker; the server treats an explicit mode as overriding
        // the isolated-sandbox relaxation (#122).
        const chosen =
          action === "mode-bypass"
            ? "bypassPermissions"
            : action === "mode-acceptEdits"
              ? "acceptEdits"
              : "default"
        patchSessionState(appName, userId, session.id, {
          permission_mode: chosen,
          plan_previous_mode: null,
        })
          .then((s) => {
            setSession(s)
            setEvents(s.events)
            setRefreshTick((t) => t + 1)
          })
          .catch((e) =>
            setError(`Failed to set permission mode: ${(e as Error).message}`),
          )
        return
      }
      case "plan":
      case "exit-plan": {
        // Direct state mutation — no LLM turn. ADK appends a synthetic
        // state-update Event so the change shows up in session.events
        // and (importantly) in session.state.permission_mode for the
        // next tool call. Values match adk_cc/permissions/modes.py:
        // PLAN="plan", DEFAULT="default".
        if (!appName || !session) return
        // Entering plan RECORDS the current mode; exiting RESTORES it (or
        // unsets, letting the env-derived default apply) — a bypass session
        // must come back as bypass, never hardcoded "default" (#122 item 1).
        const stateDelta: Record<string, unknown> =
          action === "plan"
            ? {
                permission_mode: "plan",
                plan_previous_mode:
                  permissionMode && permissionMode !== "plan"
                    ? permissionMode
                    : null,
              }
            : {
                permission_mode:
                  (session.state as Record<string, unknown> | undefined)
                    ?.plan_previous_mode ?? null,
                plan_previous_mode: null,
              }
        patchSessionState(appName, userId, session.id, stateDelta)
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

  const _st = (session?.state ?? {}) as Record<string, unknown>
  const pinnedEndpoint =
    typeof _st.model_endpoint === "string" && _st.model_endpoint ? _st.model_endpoint : null
  const pinnedModel =
    pinnedEndpoint && typeof _st.model_id === "string" && _st.model_id ? _st.model_id : null

  // Live permission mode: the newest `permission_mode` state delta in the
  // stream, falling back to the session record.
  //
  // Reading only `session.state` made the plan-mode frame appear when the TURN
  // ended, not when `enter_plan_mode` succeeded — the session object is
  // refetched after streaming completes, so for the whole plan the composer
  // still looked like a normal one (and the same lag hid the frame's removal on
  // exit). The tool writes ctx.state, which ADK emits as an event action, so
  // the change is on screen the moment it happens.
  //
  // Both spellings: the SSE stream aliases to camelCase, session history keeps
  // snake_case, and reading one silently misses half the sources.
  const permissionMode = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const actions = ((events[i] as { actions?: Record<string, unknown> }).actions
        ?? {}) as Record<string, unknown>
      const delta = (actions.stateDelta ?? actions.state_delta) as
        | Record<string, unknown>
        | undefined
      const mode = delta?.permission_mode
      if (typeof mode === "string" && mode) return mode
    }
    return typeof session?.state?.permission_mode === "string"
      ? (session.state.permission_mode as string)
      : undefined
  }, [events, session])

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
            {/* Session-id reveal (minimal): a # in the title header — this is
                the session it names, unlike a whole-list toggle in the rail. */}
            {session && (
              <button
                type="button"
                onClick={() => {
                  const v = !showSessionId
                  setShowSessionId(v)
                  localStorage.setItem("adk.showSessionIds", v ? "1" : "0")
                }}
                title={showSessionId ? "Hide session id" : "Show session id"}
                className={
                  "adk-toggle-session-ids shrink-0 rounded p-1 hover:bg-accent " +
                  (showSessionId ? "text-primary" : "text-muted-foreground/50")
                }
              >
                <Hash className="h-3 w-3" />
              </button>
            )}
            {session && showSessionId && (
              <span
                className="adk-session-id truncate font-mono text-[10px] text-muted-foreground select-all"
                title={session.id}
              >
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
        {notesOpen && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/30 p-4"
            onClick={() => setNotesOpen(false)}
          >
            <div
              className="max-h-[70vh] w-full max-w-lg overflow-auto rounded-lg border border-border bg-background p-4 shadow-xl"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-2 text-sm font-semibold">Session notes</div>
              <pre
                data-session-notes
                className="whitespace-pre-wrap text-xs text-muted-foreground"
              >
                {String(
                  (session?.state as Record<string, unknown> | undefined)
                    ?.session_notes ?? "(no notes yet — the agent records "
                    + "decisions and task state here as the session runs)",
                )}
              </pre>
            </div>
          </div>
        )}
        {error && (
          <div className="flex items-center gap-3 border-b bg-destructive/10 px-6 py-2 text-sm text-destructive">
            <span className="min-w-0 flex-1 truncate">{error}</span>
            {turnError?.rate_limited && (
              <Button size="sm" variant="outline" className="h-7 shrink-0"
                onClick={handleRetryTurn}>
                Retry turn
              </Button>
            )}
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
                modelStatus={modelStatus}
                progress={progress}
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
          sessionId={session?.id ?? null}
          userId={userId}
          footer={session ? <ContextGauge current={ctxTokens} limits={ctxLimits} /> : undefined}
          taskStrip={session ? <TaskStrip events={events} /> : undefined}
          modelChip={IS_DESKTOP ? (
            <>
              <ModelChip
                pinnedModel={pinnedModel}
                refreshKey={modelTick}
                interactive={!!(session && appName)}
                onClick={() => setModelPickerOpen(true)}
              />
              {/* W1's analysis runtime, beside the model it runs alongside.
                  Renders nothing unless there is something to say. */}
              {/* Background processes: the only ALWAYS-visible surface, so a
                  forgotten dev server stops being invisible. Opens the right
                  panel, where the dock and its logs live. */}
              <ProcessChip
                projectId={userId}
                onClick={() => setRightPanelOpen(true)}
              />
              {session && (
                <AnalysisEnvChip
                  projectId={userId}
                  sessionId={session.id}
                  refreshKey={refreshTick}
                />
              )}
            </>
          ) : undefined}
        />
      </div>
      {appName && session && (
        <RightPanel
          appName={appName}
          userId={userId}
          sessionId={session.id}
          events={events}
          open={rightPanelOpen}
          onClose={() => setRightPanelOpen(false)}
          refreshKey={refreshTick}
          onRestored={reloadSession}
        />
      )}
      <Settings
        open={settingsOpen}
        initialTab={settingsTab}
        onClose={() => { setSettingsOpen(false); setModelTick((t) => t + 1) }}
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
      {IS_DESKTOP && modelPickerOpen && appName && session && (
        <ModelPicker
          appName={appName}
          userId={userId}
          sessionId={session.id}
          pinnedEndpoint={pinnedEndpoint}
          pinnedModel={pinnedModel}
          onClose={() => setModelPickerOpen(false)}
          onPicked={(label, s) => {
            // The PATCH returns the updated session (new state + the
            // synthetic state-update event) — apply it like the plan toggle.
            setSession(s)
            setEvents(s.events)
            setRefreshTick((t) => t + 1)
            setModelTick((t) => t + 1)
            showNotice(label
              ? `✓ Model for this session → ${label} (next turn)`
              : "✓ Session model reset to the default")
          }}
        />
      )}
    </div>
  )
}
