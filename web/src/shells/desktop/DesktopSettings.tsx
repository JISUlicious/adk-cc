import { useCallback, useEffect, useState } from "react"
import { Palette, KeyRound, Server, Boxes, Cpu, Check, Trash2, Plus, FolderTree, Sparkles } from "lucide-react"
import { SettingsFrame, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ApiError } from "@/shared/api/client"
import {
  listDesktopModels, setDesktopModel, activateDesktopModel, deleteDesktopModel, type DesktopModel,
  getCodexStatus, connectCodex, disconnectCodex, type CodexStatus,
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
  const load = useCallback(() => {
    getCodexStatus().then(setStatus).catch((e) => setErr(errMsg(e)))
  }, [])
  useEffect(load, [load])
  async function connect() {
    setBusy(true); setErr(null)
    try { setStatus(await connectCodex("gpt-5.5", "medium")); onChange() }
    catch (e) { setErr(errMsg(e)) } finally { setBusy(false) }
  }
  async function disconnect() {
    setBusy(true); setErr(null)
    try { await disconnectCodex(); load(); onChange() }
    catch (e) { setErr(errMsg(e)) } finally { setBusy(false) }
  }
  if (!status) return null
  return (
    <div className="space-y-2 rounded-md border border-primary/40 bg-brand-tint p-3">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-primary" />
        <span className="text-sm font-medium">ChatGPT subscription</span>
        {status.active && (
          <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">active</span>
        )}
      </div>
      {status.connected ? (
        <>
          <p className="text-xs text-muted-foreground">
            Signed in via Codex CLI · plan <b className="uppercase">{status.plan ?? "?"}</b>
            {status.account_id_tail && <> · account …{status.account_id_tail}</>}
            {status.expired && <span className="text-destructive"> · token expired — run <code>codex login</code></span>}
          </p>
          {status.registered ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground">
                Endpoint <code className="font-mono">{status.model}</code> registered{status.active ? " · active" : ""}.
              </span>
              <Button size="sm" variant="outline" className="ml-auto" disabled={busy} onClick={disconnect}>
                {busy ? "…" : "Disconnect"}
              </Button>
            </div>
          ) : (
            <Button size="sm" disabled={busy} onClick={connect}>
              {busy ? "Connecting…" : "Connect (gpt-5.5)"}
            </Button>
          )}
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          No ChatGPT login found. Run <code>codex login</code> in a terminal, then reopen Settings.
        </p>
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
  const reload = useCallback(() => {
    listDesktopModels().then((r) => { setEndpoints(r.endpoints); setActive(r.active) }).catch((e) => setErr(errMsg(e)))
  }, [])
  useEffect(reload, [reload])
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
        <Input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} placeholder="openai/model-id" className="font-mono text-xs" />
        <Input value={form.api_base} onChange={(e) => setForm({ ...form, api_base: e.target.value })} placeholder="https://host:port/v1" className="text-xs" />
        <Input value={form.api_key_env} onChange={(e) => setForm({ ...form, api_key_env: e.target.value })} placeholder="API_KEY env (blank = keyless)" className="font-mono text-xs" />
      </div>
      <Button size="sm" variant="outline" onClick={add}><Plus className="h-3.5 w-3.5" /> Add endpoint</Button>
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
