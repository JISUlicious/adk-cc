import { useCallback, useEffect, useRef, useState } from "react"
import {
  Palette, KeyRound, Server, Boxes, Cpu, Check, Trash2, Plus, FolderTree, Sparkles,
  ChevronDown, ChevronRight, RefreshCw,
} from "lucide-react"
import { SettingsFrame, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ApiError } from "@/shared/api/client"
import {
  listDesktopModels, setDesktopModel, activateDesktopModel, deleteDesktopModel, type DesktopModel,
  getCodexStatus, connectCodex, disconnectCodex, type CodexStatus,
  startCodexLogin, getCodexLoginStatus, codexSignout, getCodexModels, discoverModels,
  selectModel, refreshModels,
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

/** Connect your ChatGPT subscription as the active model — inference runs on
 *  your Plus/Pro plan quota, not an API key. One "Connect" button authenticates
 *  (browser OAuth, or an existing Codex CLI login) and registers with the first
 *  discovered model; the model is picked afterwards from the dropdown. */
function CodexConnect({ onChange }: { onChange: () => void }) {
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [models, setModels] = useState<string[]>([])
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const load = useCallback(() => {
    getCodexStatus().then((s) => alive.current && setStatus(s)).catch((e) => alive.current && setErr(errMsg(e)))
  }, [])
  useEffect(load, [load])
  useEffect(() => {
    if (!status?.registered) return
    getCodexModels().then((r) => alive.current && setModels(r.models)).catch(() => {})
  }, [status?.registered])

  async function run(fn: () => Promise<CodexStatus | null | void>) {
    setBusy(true); setErr(null)
    try { const s = await fn(); if (s && alive.current) setStatus(s); onChange() }
    catch (e) { if (alive.current) setErr(errMsg(e)) } finally { if (alive.current) setBusy(false) }
  }
  const connectDefault = () => run(() => connectCodex())          // register with the first discovered model
  const switchModel = (m: string) => run(() => connectCodex(m))   // m = base model name
  const disconnect = () => run(async () => { await disconnectCodex(); load() })
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
          const s = await connectCodex() // register with the first discovered model
          if (alive.current) setStatus(s)
          onChange(); break
        }
        if (st.state === "error") { if (alive.current) setErr("Sign-in failed: " + (st.error || "unknown")); break }
      }
    } catch (e) { if (alive.current) setErr(errMsg(e)) }
    finally { if (alive.current) { setSigningIn(false); setAuthUrl(null) } }
  }
  // The one action: authenticate if there's no login yet, else just connect.
  const handleConnect = () => { if (status?.connected) void connectDefault(); else void signIn() }

  if (!status) return null
  const via = status.mode === "own" ? "browser sign-in" : status.mode === "cli" ? "Codex CLI" : "login file"
  return (
    <div className="space-y-2 rounded-md border border-primary/40 bg-brand-tint p-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        <span className="text-sm font-medium">ChatGPT subscription</span>
        {status.active && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">active</span>
        )}
      </div>
      {signingIn ? (
        <p className="text-xs text-muted-foreground">
          Authenticating in your browser…{" "}
          {authUrl && <a href={authUrl} target="_blank" rel="noreferrer" className="text-primary underline">open the page</a>}
        </p>
      ) : status.registered ? (
        <>
          <p className="text-xs text-muted-foreground">
            Connected via {via} · plan <b className="uppercase">{status.plan ?? "?"}</b>
            {status.account_id_tail && <> · account …{status.account_id_tail}</>}
            {status.expired && <span className="text-destructive"> · token expired</span>}
          </p>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">Model</span>
            <select
              value={status.model || ""}
              disabled={busy}
              onChange={(e) => switchModel(e.target.value)}
              className="rounded border border-input bg-background px-1.5 py-1 font-mono text-xs"
              title="Switch the active model"
            >
              {(models.length ? models : status.model ? [status.model] : []).map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            {status.active && <span className="text-xs text-muted-foreground">· active</span>}
            <Button size="sm" variant="outline" className="ml-auto" disabled={busy} onClick={disconnect}>Disconnect</Button>
          </div>
          <div className="flex items-center gap-3">
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
          </div>
        </>
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
              ? "Uses your Plus/Pro plan (not an API key). Connects on your first model — change it below after."
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
  const [form, setForm] = useState<DesktopModel>({ name: "", model: "", api_base: "", api_key_env: "ADK_CC_API_KEY" })
  const [err, setErr] = useState<string | null>(null)
  const reload = useCallback(() => {
    listDesktopModels().then((r) => { setEndpoints(r.endpoints); setActive(r.active) }).catch((e) => setErr(errMsg(e)))
  }, [])
  useEffect(reload, [reload])
  async function guard(name: string, fn: () => Promise<unknown>) {
    setBusy(name); setErr(null)
    try { await fn(); reload() } catch (e) { setErr(errMsg(e)) } finally { setBusy(null) }
  }
  const pickModel = (name: string, model: string) => guard(name, () => selectModel(name, model))
  const refresh = (name: string) => guard(name, () => refreshModels(name))
  const activate = (name: string) => guard(name, () => activateDesktopModel(name))
  const del = (name: string) => guard(name, () => deleteDesktopModel(name))
  // Add a provider by URL only: check the connection, load its models from
  // /v1/models, and default to the first — no model typed by hand.
  async function add() {
    const name = form.name.trim(), api_base = form.api_base.trim(), api_key_env = form.api_key_env.trim()
    if (!name || !api_base) { setErr("Enter a name and provider URL."); return }
    setBusy(name); setErr(null)
    try {
      const r = await discoverModels(api_base, api_key_env)
      if (!r.models.length) { setErr("Provider returned no models — check the URL and key."); return }
      const full = r.models.map((m) => (m.includes("/") ? m : `openai/${m}`))
      await setDesktopModel({ name, model: full[0], api_base, api_key_env, models: full })
      setForm({ name: "", model: "", api_base: "", api_key_env: "ADK_CC_API_KEY" }); reload()
    } catch (e) { setErr(errMsg(e)) } finally { setBusy(null) }
  }
  // The chatgpt-codex provider is managed by the card above — hide its raw entry.
  const providers = endpoints.filter((e) => e.name !== "chatgpt-codex")
  return (
    <div className="space-y-3 py-1">
      <CodexConnect onChange={reload} />
      <p className="text-xs text-muted-foreground">Model providers are global — the active model serves every project's agent (next turn). Click a provider to pick its model; also switchable in chat with <code>/model</code>.</p>
      <div className="space-y-1">
        {providers.map((e) => {
          const open = expanded === e.name
          const models = e.models ?? []
          return (
            <div key={e.name} className="rounded-md border border-border/60">
              <div className="flex items-center gap-2 px-2 py-1.5 text-sm">
                <button onClick={() => activate(e.name)} disabled={busy === e.name} title={active === e.name ? "Active" : "Activate"} className={active === e.name ? "text-green-600" : "text-muted-foreground hover:text-foreground"}>
                  <Check className="h-4 w-4" />
                </button>
                <button onClick={() => setExpanded(open ? null : e.name)} className="flex min-w-0 flex-1 items-center gap-2 text-left">
                  {open ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" /> : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />}
                  <span className="font-medium">{e.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{baseName(e.model)}</span>
                </button>
                {!e.api_key_present && <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-600">no key</span>}
                <button onClick={() => del(e.name)} disabled={busy === e.name} className="text-muted-foreground hover:text-destructive" title="Remove"><Trash2 className="h-3.5 w-3.5" /></button>
              </div>
              {open && (
                <div className="space-y-2 border-t border-border/50 px-2 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-muted-foreground">Model</span>
                    <select
                      value={e.model}
                      disabled={busy === e.name}
                      onChange={(ev) => pickModel(e.name, ev.target.value)}
                      className="min-w-0 flex-1 rounded border border-input bg-background px-1.5 py-1 font-mono text-xs"
                    >
                      {(models.length ? models : [e.model]).map((m) => <option key={m} value={m}>{baseName(m)}</option>)}
                    </select>
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
        <Input value={form.api_key_env} onChange={(e) => setForm({ ...form, api_key_env: e.target.value })} placeholder="API_KEY env (blank = keyless)" className="font-mono text-xs" />
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
  ]
  return <SettingsFrame open={open} onClose={onClose} tabs={tabs} />
}
