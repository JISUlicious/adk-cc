import { useCallback, useEffect, useRef, useState } from "react"
import { Palette, KeyRound, Server, Boxes, Cpu, Check, Trash2, Plus, FolderTree, Sparkles } from "lucide-react"
import { SettingsFrame, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ApiError } from "@/shared/api/client"
import {
  listDesktopModels, setDesktopModel, activateDesktopModel, deleteDesktopModel, type DesktopModel,
  getCodexStatus, connectCodex, disconnectCodex, type CodexStatus,
  startCodexLogin, getCodexLoginStatus, codexSignout, getCodexModels, discoverModels,
} from "@/shared/api/desktop-settings"
import { LayeredTab, SecretsScope, McpScope, SkillsScope, WorkingDirsScope } from "./DesktopSettingsSections"

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return (e.body as { detail?: string } | undefined)?.detail || e.message
  return (e as Error)?.message || String(e)
}

/** Connect your ChatGPT subscription (via the Codex CLI login) as the active
 *  model — inference runs on your Plus/Pro plan quota, not an API key. */
function CodexConnect({ onChange }: { onChange: () => void }) {
  const [status, setStatus] = useState<CodexStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState("gpt-5.5")
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])
  const load = useCallback(() => {
    getCodexStatus().then((s) => alive.current && setStatus(s)).catch((e) => alive.current && setErr(errMsg(e)))
  }, [])
  useEffect(load, [load])
  // Discover the account's models once connected (for the picker).
  useEffect(() => {
    if (!status?.connected) return
    getCodexModels().then((r) => {
      if (!alive.current) return
      setModels(r.models)
      setModel((m) => status.model || (r.models.includes(m) ? m : r.models[0] || m))
    }).catch(() => {})
  }, [status?.connected, status?.model])

  async function connect(m = model) {
    setBusy(true); setErr(null)
    try { const s = await connectCodex(m, "medium"); if (alive.current) { setStatus(s); setModel(m) }; onChange() }
    catch (e) { if (alive.current) setErr(errMsg(e)) } finally { if (alive.current) setBusy(false) }
  }
  async function disconnect() {
    setBusy(true); setErr(null)
    try { await disconnectCodex(); load(); onChange() }
    catch (e) { if (alive.current) setErr(errMsg(e)) } finally { if (alive.current) setBusy(false) }
  }
  async function signOut() {
    setBusy(true); setErr(null)
    try { const s = await codexSignout(); if (alive.current) setStatus(s); onChange() }
    catch (e) { if (alive.current) setErr(errMsg(e)) } finally { if (alive.current) setBusy(false) }
  }
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
          const s = await connectCodex("gpt-5.5", "medium")
          if (alive.current) setStatus(s)
          onChange(); break
        }
        if (st.state === "error") { if (alive.current) setErr("Sign-in failed: " + (st.error || "unknown")); break }
      }
    } catch (e) { if (alive.current) setErr(errMsg(e)) }
    finally { if (alive.current) { setSigningIn(false); setAuthUrl(null) } }
  }

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
          Complete sign-in in your browser…{" "}
          {authUrl && <a href={authUrl} target="_blank" rel="noreferrer" className="text-primary underline">open the page</a>}
        </p>
      ) : status.connected ? (
        <>
          <p className="text-xs text-muted-foreground">
            Signed in via {via} · plan <b className="uppercase">{status.plan ?? "?"}</b>
            {status.account_id_tail && <> · account …{status.account_id_tail}</>}
            {status.expired && <span className="text-destructive"> · token expired — sign in again</span>}
          </p>
          <div className="flex items-center gap-2">
            <span className="text-xs text-muted-foreground">{status.registered ? "Model" : "Choose a model"}</span>
            {/* Post-connection step: picking a model registers + activates it. */}
            <select
              value={status.registered ? model : ""}
              disabled={busy || (!models.length && !status.registered)}
              onChange={(e) => { const v = e.target.value; if (v) { setModel(v); void connect(v) } }}
              className="rounded border border-input bg-background px-1.5 py-1 font-mono text-xs"
              title={status.registered ? "Switch the active model" : "Pick a model to start using your plan"}
            >
              {!status.registered && (
                <option value="" disabled>{models.length ? "Select a model…" : "Loading models…"}</option>
              )}
              {(models.length ? models : status.model ? [status.model] : []).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            {status.registered && (
              <span className="text-xs text-muted-foreground">{status.active ? "· active" : "· registered"}</span>
            )}
            {status.registered && (
              <Button size="sm" variant="outline" className="ml-auto" disabled={busy} onClick={disconnect}>Disconnect</Button>
            )}
          </div>
          {/* Update / re-do the connection: switch the CLI login to your own
              browser login, refresh an expired token, or use another account. */}
          <div className="flex items-center gap-3">
            {status.expired ? (
              <Button size="sm" disabled={busy} onClick={signIn}><Sparkles className="h-3.5 w-3.5" /> Sign in again</Button>
            ) : (
              <button className="text-[10px] text-primary hover:underline disabled:opacity-50" disabled={busy} onClick={signIn}>
                {status.mode === "cli" ? "Sign in with ChatGPT (browser)" : "Re-authenticate in browser"}
              </button>
            )}
            {status.mode === "own" && (
              <button className="text-[10px] text-muted-foreground hover:text-destructive disabled:opacity-50" disabled={busy} onClick={signOut}>sign out</button>
            )}
          </div>
        </>
      ) : (
        <>
          <Button size="sm" disabled={busy} onClick={signIn}><Sparkles className="h-3.5 w-3.5" /> Sign in with ChatGPT</Button>
          <p className="text-[11px] text-muted-foreground">Opens your browser · uses your Plus/Pro plan, not an API key. (Or run <code>codex login</code> in a terminal.)</p>
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
  const [form, setForm] = useState<DesktopModel>({ name: "", model: "", api_base: "", api_key_env: "ADK_CC_API_KEY" })
  const [err, setErr] = useState<string | null>(null)
  const [discovered, setDiscovered] = useState<string[]>([])
  const [discovering, setDiscovering] = useState(false)
  const reload = useCallback(() => {
    listDesktopModels().then((r) => { setEndpoints(r.endpoints); setActive(r.active) }).catch((e) => setErr(errMsg(e)))
  }, [])
  useEffect(reload, [reload])
  async function discover() {
    if (!form.api_base.trim()) { setErr("Enter the provider api_base first."); return }
    setDiscovering(true); setErr(null)
    try {
      const r = await discoverModels(form.api_base.trim(), form.api_key_env.trim())
      setDiscovered(r.models)
      if (!r.models.length) setErr("Provider returned no models.")
    } catch (e) { setErr(errMsg(e)) } finally { setDiscovering(false) }
  }
  async function add() {
    if (!form.name.trim() || !form.model.trim() || !form.api_base.trim()) return
    setErr(null)
    try {
      await setDesktopModel({ ...form, name: form.name.trim() })
      setForm({ name: "", model: "", api_base: "", api_key_env: "ADK_CC_API_KEY" }); reload()
    } catch (e) { setErr(errMsg(e)) }
  }
  async function activate(name: string) { setErr(null); try { await activateDesktopModel(name); reload() } catch (e) { setErr(errMsg(e)) } }
  async function del(name: string) { setErr(null); try { await deleteDesktopModel(name); reload() } catch (e) { setErr(errMsg(e)) } }
  return (
    <div className="space-y-3 py-1">
      <CodexConnect onChange={reload} />
      <p className="text-xs text-muted-foreground">Model endpoints are global — the active one serves every project's agent; switching takes effect on the next turn.</p>
      <div className="space-y-1.5">
        {endpoints.map((e) => (
          <div key={e.name} className="flex items-center gap-2 text-sm">
            <button onClick={() => activate(e.name)} title={active === e.name ? "Active" : "Activate"} className={active === e.name ? "text-green-600" : "text-muted-foreground hover:text-foreground"}>
              <Check className="h-4 w-4" />
            </button>
            <span className="font-medium">{e.name}</span>
            <span className="truncate font-mono text-xs text-muted-foreground" title={`${e.model} @ ${e.api_base}`}>{e.model}</span>
            {!e.api_key_present && <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-600">no key</span>}
            <button onClick={() => del(e.name)} className="ml-auto text-muted-foreground hover:text-destructive" title="Remove"><Trash2 className="h-3.5 w-3.5" /></button>
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-1 border-t border-border/50 pt-2">
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="name" className="text-xs" />
        <Input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder="openai/model-id" className="font-mono text-xs" list="adk-discovered-models" />
        <Input value={form.api_base} onChange={(e) => setForm({ ...form, api_base: e.target.value })} placeholder="https://host:port/v1" className="text-xs" />
        <Input value={form.api_key_env} onChange={(e) => setForm({ ...form, api_key_env: e.target.value })} placeholder="API_KEY env (blank = keyless)" className="font-mono text-xs" />
      </div>
      <datalist id="adk-discovered-models">{discovered.map((m) => <option key={m} value={m} />)}</datalist>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={add}><Plus className="h-3.5 w-3.5" /> Add endpoint</Button>
        <Button size="sm" variant="ghost" onClick={discover} disabled={discovering} title="List this provider's models (GET api_base/models)">
          {discovering ? "Discovering…" : "Discover models"}
        </Button>
        {discovered.length > 0 && <span className="text-[10px] text-muted-foreground">{discovered.length} models — pick from the model field</span>}
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
