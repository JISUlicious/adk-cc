import { useCallback, useEffect, useRef, useState } from "react"
import {
  Palette, KeyRound, Server, Boxes, Cpu, Check, Trash2, Plus, FolderTree, Sparkles,
  ChevronDown, ChevronRight, RefreshCw, ShieldCheck, Download,
} from "lucide-react"
import { SettingsFrame, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ModelCombobox } from "@/shared/components/ModelCombobox"
import { ApiError } from "@/shared/api/client"
import {
  listDesktopModels, setDesktopModel, activateDesktopModel, deleteDesktopModel, type DesktopModel,
  getCodexStatus, connectCodex, disconnectCodex, type CodexStatus,
  startCodexLogin, getCodexLoginStatus, codexSignout, discoverModels,
  selectModel, refreshModels,
  getSandbox, setSandbox, pullSandboxImage, type SandboxStatus,
} from "@/shared/api/desktop-settings"

/** Base model name for display (strip the provider routing prefix). */
function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}
import { LayeredTab, SecretsScope, McpScope, SkillsScope, WorkingDirsScope } from "./DesktopSettingsSections"

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return (e.body as { detail?: string } | undefined)?.detail || e.message
  return (e as Error)?.message || String(e)
}

/** ChatGPT-subscription connect card. Before connecting: a single "Connect with
 *  ChatGPT subscription" button (browser OAuth or an existing Codex CLI login).
 *  Once connected it collapses to a minimal status + auth actions — the provider
 *  itself is listed and MANAGED (model / activate / remove) in the providers
 *  list below, so it's a one-click activate like any other provider. `refreshTick`
 *  re-fetches the card whenever that list changes (add / remove / activate). */
function CodexConnect({ onChange, refreshTick }: { onChange: () => void; refreshTick: number }) {
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const load = useCallback(() => {
    getCodexStatus().then((s) => alive.current && setStatus(s)).catch((e) => alive.current && setErr(errMsg(e)))
  }, [])
  useEffect(() => { load() }, [load, refreshTick])

  async function run(fn: () => Promise<CodexStatus | null | void>) {
    setBusy(true); setErr(null)
    try { const s = await fn(); if (s && alive.current) setStatus(s); onChange() }
    catch (e) { if (alive.current) setErr(errMsg(e)) } finally { if (alive.current) setBusy(false) }
  }
  const connectDefault = () => run(() => connectCodex()) // register with the first discovered model
  const signOut = () => run(() => codexSignout())

  async function signIn() {
    setSigningIn(true); setErr(null); setAuthUrl(null)
    try {
      const { auth_url } = await startCodexLogin()
      setAuthUrl(auth_url)
      window.open(auth_url, "_blank", "noopener")
      for (let i = 0; i < 150 && alive.current; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        const st = await getCodexLoginStatus()
        if (st.state === "done") {
          const s = await connectCodex()
          if (alive.current) setStatus(s)
          onChange(); break
        }
        if (st.state === "error") { if (alive.current) setErr("Sign-in failed: " + (st.error || "unknown")); break }
      }
    } catch (e) { if (alive.current) setErr(errMsg(e)) }
    finally { if (alive.current) { setSigningIn(false); setAuthUrl(null) } }
  }
  const handleConnect = () => { if (status?.connected) void connectDefault(); else void signIn() }

  if (!status) return null
  const via = status.mode === "own" ? "browser sign-in" : status.mode === "cli" ? "Codex CLI" : "login file"
  return (
    <div className="space-y-2 rounded-md border border-primary/40 bg-brand-tint p-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        <span className="text-sm font-medium">ChatGPT subscription</span>
      </div>
      {signingIn ? (
        <p className="text-xs text-muted-foreground">
          Authenticating in your browser…{" "}
          {authUrl && <a href={authUrl} target="_blank" rel="noreferrer" className="text-primary underline">open the page</a>}
        </p>
      ) : status.registered ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-xs text-muted-foreground">
            Connected · plan <b className="uppercase">{status.plan ?? "?"}</b>
            {status.account_id_tail && <> · account …{status.account_id_tail}</>}
            {status.expired && <span className="text-destructive"> · token expired</span>}
          </span>
          {status.expired ? (
            <Button size="sm" disabled={busy} onClick={signIn}><Sparkles className="h-3.5 w-3.5" /> Re-authenticate</Button>
          ) : (
            <button className="text-[10px] text-primary hover:underline disabled:opacity-50" disabled={busy} onClick={signIn}>
              {status.mode === "cli" ? "Authenticate with browser instead" : "Re-authenticate in browser"}
            </button>
          )}
          {status.mode === "own" && (
            <button className="text-[10px] text-muted-foreground hover:text-destructive disabled:opacity-50" disabled={busy} onClick={signOut}>sign out</button>
          )}
          <span className="w-full text-[10px] text-muted-foreground">Listed as a provider below — pick its model, activate, or remove it there.</span>
        </div>
      ) : (
        <>
          {status.connected && (
            <p className="text-xs text-muted-foreground">Login detected via {via} · plan <b className="uppercase">{status.plan ?? "?"}</b>.</p>
          )}
          <Button size="sm" disabled={busy} onClick={handleConnect}>
            <Sparkles className="h-3.5 w-3.5" /> {busy ? "Connecting…" : "Connect with ChatGPT subscription"}
          </Button>
          <p className="text-[11px] text-muted-foreground">
            {status.connected
              ? "Uses your Plus/Pro plan (not an API key). Connects on your first model — pick it in the list below."
              : "Authenticates in your browser · uses your Plus/Pro plan, not an API key."}
          </p>
        </>
      )}
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

/** Model endpoints — global only (one active model serves every project; switching
 *  takes effect on the next turn since SelectableLlm re-reads the registry). */
function ModelsSection() {
  const [endpoints, setEndpoints] = useState<DesktopModel[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [form, setForm] = useState<DesktopModel>({ name: "", model: "", api_base: "", api_key: "" })
  const [err, setErr] = useState<string | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const reload = useCallback(() => {
    listDesktopModels().then((r) => { setEndpoints(r.endpoints); setActive(r.active) }).catch((e) => setErr(errMsg(e)))
  }, [])
  useEffect(reload, [reload])
  // Re-fetch the ChatGPT card after any registry mutation (e.g. removing the
  // ChatGPT provider must bring its "Connect" button back).
  const bump = () => setRefreshTick((t) => t + 1)
  async function guard(name: string, fn: () => Promise<unknown>) {
    setBusy(name); setErr(null)
    try { await fn(); reload(); bump() } catch (e) { setErr(errMsg(e)) } finally { setBusy(null) }
  }
  const pickModel = (name: string, model: string) => guard(name, () => selectModel(name, model))
  const refresh = (name: string) => guard(name, () => refreshModels(name))
  const activate = (name: string) => guard(name, () => activateDesktopModel(name))
  // Removing the ChatGPT provider = disconnect it (switches active away + drops
  // the endpoint), so the card's "Connect" button returns.
  const del = (name: string) => guard(name, () => (name === "chatgpt-codex" ? disconnectCodex() : deleteDesktopModel(name)))
  // Add a provider by URL only: check the connection, load its models from
  // /v1/models, and default to the first — no model typed by hand.
  async function add() {
    // The actual api key is entered directly; blank = keyless (local model
    // servers that need no auth) — both are valid, so only name+URL gate.
    const name = form.name.trim(), api_base = form.api_base.trim(), api_key = (form.api_key ?? "").trim()
    if (!name || !api_base) { setErr("Enter a name and provider URL."); return }
    setBusy(name); setErr(null)
    try {
      const r = await discoverModels(api_base, api_key)
      if (!r.models.length) { setErr("Provider returned no models — check the URL and key."); return }
      const full = r.models.map((m) => (m.includes("/") ? m : `openai/${m}`))
      await setDesktopModel({ name, model: full[0], api_base, api_key, models: full })
      setForm({ name: "", model: "", api_base: "", api_key: "" }); reload(); bump()
    } catch (e) { setErr(errMsg(e)) } finally { setBusy(null) }
  }
  return (
    <div className="space-y-3 py-1">
      <CodexConnect onChange={reload} refreshTick={refreshTick} />
      <p className="text-xs text-muted-foreground">Model providers are global — the active model serves every project's agent (next turn). Click a provider to pick its model; also switchable in chat with <code>/model</code>.</p>
      <div className="space-y-1">
        {endpoints.map((e) => {
          const open = expanded === e.name
          const models = e.models ?? []
          const isCodex = e.name === "chatgpt-codex"
          return (
            <div key={e.name} className="rounded-md border border-border/60">
              <div className="flex items-center gap-2 px-2 py-1.5 text-sm">
                <button onClick={() => activate(e.name)} disabled={busy === e.name} title={active === e.name ? "Active" : "Activate"} className={active === e.name ? "text-green-600" : "text-muted-foreground hover:text-foreground"}>
                  <Check className="h-4 w-4" />
                </button>
                <button onClick={() => setExpanded(open ? null : e.name)} className="flex min-w-0 flex-1 items-center gap-2 text-left">
                  {open ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" /> : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />}
                  {isCodex && <Sparkles className="h-3.5 w-3.5 text-primary" />}
                  <span className="font-medium">{isCodex ? "ChatGPT" : e.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{baseName(e.model)}</span>
                </button>
                {!e.api_key_present && <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-600">no key</span>}
                <button onClick={() => del(e.name)} disabled={busy === e.name} className="text-muted-foreground hover:text-destructive" title={isCodex ? "Disconnect ChatGPT" : "Remove"}><Trash2 className="h-3.5 w-3.5" /></button>
              </div>
              {open && (
                <div className="space-y-2 border-t border-border/50 px-2 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">Model</span>
                    <ModelCombobox
                      options={models.length ? models : [e.model]}
                      value={e.model}
                      disabled={busy === e.name}
                      onPick={(m) => pickModel(e.name, m)}
                      className="flex-1"
                    />
                    <Button size="sm" variant="ghost" disabled={busy === e.name} onClick={() => refresh(e.name)} title="Re-discover this provider's models">
                      <RefreshCw className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  <div className="font-mono text-[10px] text-muted-foreground">{e.model} @ {e.api_base}</div>
                </div>
              )}
            </div>
          )
        })}
      </div>
      <div className="grid grid-cols-3 gap-1 border-t border-border/50 pt-2">
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="name" className="text-xs" />
        <Input value={form.api_base} onChange={(e) => setForm({ ...form, api_base: e.target.value })} placeholder="https://host:port/v1" className="text-xs" />
        <Input type="password" autoComplete="off" value={form.api_key ?? ""} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder="API key (blank = keyless)" className="font-mono text-xs" />
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={add} disabled={busy === form.name.trim() && !!form.name.trim()}>
          <Plus className="h-3.5 w-3.5" /> Add provider
        </Button>
        <span className="text-[10px] text-muted-foreground">Checks the connection, loads models from <code>/v1/models</code>, defaults to the first.</span>
      </div>
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

/**
 * Container sandbox (desktop-local Docker/Podman). Opt-in: run the agent's shell
 * inside a container that mounts the project in-place but isolates the host. Host
 * execution stays the default; the toggle applies to NEW chats.
 */
function Toggle({ on, disabled, onClick }: { on: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button" role="switch" aria-checked={on} disabled={disabled} onClick={onClick}
      className={"relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-40 " +
        (on ? "bg-primary" : "bg-input")}
    >
      <span className={"absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all " + (on ? "left-[22px]" : "left-0.5")} />
    </button>
  )
}

function SandboxSection() {
  const [s, setS] = useState<SandboxStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [pulling, setPulling] = useState(false)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const load = useCallback(() => {
    getSandbox().then((v) => alive.current && setS(v)).catch((e) => alive.current && setErr(errMsg(e)))
  }, [])
  useEffect(load, [load])

  async function patch(p: Partial<Pick<SandboxStatus, "mode" | "network">>) {
    setBusy(true); setErr(null)
    try { const v = await setSandbox(p); if (alive.current) setS(v) }
    catch (e) { if (alive.current) setErr(errMsg(e)) }
    finally { if (alive.current) setBusy(false) }
  }
  async function pull() {
    setPulling(true); setErr(null)
    try { const v = await pullSandboxImage(); if (alive.current) setS(v) }
    catch (e) { if (alive.current) setErr(errMsg(e)) }
    finally { if (alive.current) setPulling(false) }
  }

  if (!s) return <div className="text-xs text-muted-foreground">Loading…</div>
  const on = s.mode === "container"
  const p = s.env_pinned
  return (
    <div className="space-y-5">
      <p className="text-xs text-muted-foreground">
        Run the agent's shell commands inside a local container. The project is mounted
        in-place (edits still land in your real files), but a bad command can't escape to
        the rest of your machine, the network (when locked), or past the resource limits.
        Applies to new chats.
      </p>

      {/* runtime status */}
      <div className="rounded-md border border-border/60 px-3 py-2 text-sm">
        {s.available ? (
          <span className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-green-600" />
            {s.runtime?.name === "podman" ? "Podman" : "Docker"} {s.runtime?.version} detected
          </span>
        ) : (
          <span className="text-muted-foreground">
            No container runtime found — install/start Docker or Podman. Commands run on the host.
          </span>
        )}
      </div>

      {/* opted-in but the runtime isn't available → honest fallback warning */}
      {on && !s.available && (
        <p className="rounded-md bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          Sandbox is enabled but no runtime is available — commands currently run on the
          host. Start Docker/Podman{s.require ? "" : ", or turn this off"} to fix it.
        </p>
      )}

      {/* mode toggle — only gate turning ON (turning OFF never needs a runtime) */}
      <label className="flex items-center justify-between gap-3">
        <span className="text-sm">
          <span className="font-medium">Container sandbox</span>
          <span className="block text-xs text-muted-foreground">
            {on
              ? (s.available ? "Shell runs in a container." : "Requested — waiting on a runtime.")
              : "Shell runs directly on the host (default)."}
            {p.mode && " · pinned by ADK_CC_SANDBOX_MODE"}
          </span>
        </span>
        <Toggle on={on} disabled={busy || p.mode || (!s.available && !on)}
                onClick={() => patch({ mode: on ? "host" : "container" })} />
      </label>

      {/* network toggle — only meaningful when sandboxed */}
      {on && (
        <label className="flex items-center justify-between gap-3">
          <span className="text-sm">
            <span className="font-medium">Allow network</span>
            <span className="block text-xs text-muted-foreground">
              {s.network ? "pip/npm/git/curl work." : "Locked down (no egress)."}
              {p.network && " · pinned by ADK_CC_SANDBOX_NETWORK"}
            </span>
          </span>
          <Toggle on={s.network} disabled={busy || p.network}
                  onClick={() => patch({ network: !s.network })} />
        </label>
      )}

      {/* image */}
      {on && (
        <div className="flex items-center justify-between gap-3">
          <span className="min-w-0 text-sm">
            <span className="font-medium">Image</span>
            <code className="ml-2 truncate text-xs text-muted-foreground">{s.image}</code>
            <span className={"ml-2 text-xs " + (s.image_present ? "text-green-600" : "text-amber-600")}>
              {s.image_present ? "· present" : "· not pulled"}
            </span>
          </span>
          {!s.image_present && s.available && (
            <Button variant="outline" size="sm" disabled={pulling || s.pulling} onClick={pull}>
              <Download className="h-3.5 w-3.5" />
              {pulling || s.pulling ? "Pulling…" : "Pull"}
            </Button>
          )}
        </div>
      )}

      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

/**
 * Desktop settings — single-user / no-login. Appearance + layered global +
 * per-project MCP / Skills / Secrets (backed by /desktop/settings/*, mapped onto
 * the agent's tenant∪user credential union), plus global-only model endpoints.
 */
export function DesktopSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const tabs: SettingsTab[] = [
    { id: "appearance", label: "Appearance", icon: Palette, render: () => <ThemeSection /> },
    {
      id: "secrets", label: "Secrets", icon: KeyRound,
      render: () => <LayeredTab blurb="Credentials + variables the agent can read (e.g. a token its run_bash uses, or a value a skill/MCP server needs)." render={(s, p) => <SecretsScope scope={s} projectId={p} />} />,
    },
    {
      id: "mcp", label: "MCP", icon: Server,
      render: () => <LayeredTab blurb="MCP servers exposed to the agent as tools. credential_key references a secret from the Secrets tab." render={(s, p) => <McpScope scope={s} projectId={p} />} />,
    },
    {
      id: "skills", label: "Skills", icon: Boxes,
      render: () => <LayeredTab blurb="Skill bundles (a folder or .zip with a SKILL.md / manifest) the agent can load." render={(s, p) => <SkillsScope scope={s} projectId={p} />} />,
    },
    {
      id: "working-dirs", label: "Working dirs", icon: FolderTree,
      render: () => <LayeredTab blurb="Directories outside the project the agent may read/write in (like Claude Code's added directories). Secret paths stay protected." render={(s, p) => <WorkingDirsScope scope={s} projectId={p} />} />,
    },
    { id: "models", label: "Models", icon: Cpu, render: () => <ModelsSection /> },
    { id: "sandbox", label: "Sandbox", icon: ShieldCheck, render: () => <SandboxSection /> },
  ]
  return <SettingsFrame open={open} onClose={onClose} tabs={tabs} />
}
