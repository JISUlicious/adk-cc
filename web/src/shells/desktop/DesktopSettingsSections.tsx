import { useCallback, useEffect, useRef, useState, type ReactNode, type ChangeEvent } from "react"
import { ChevronRight, Trash2, Upload, Plus } from "lucide-react"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { listProjects, type Project } from "@/shared/api/projects"
import { ApiError } from "@/shared/api/client"
import { FolderPickerButton } from "@/shared/components/FolderPickerButton"
import {
  type Scope,
  listDesktopSecrets, setDesktopSecret, deleteDesktopSecret,
  listDesktopMcp, setDesktopMcp, deleteDesktopMcp, type DesktopMcpServer,
  listDesktopSkills, uploadDesktopSkill, deleteDesktopSkill, addDesktopSkillFromDir,
  listWorkingDirs, addWorkingDir, removeWorkingDir,
} from "@/shared/api/desktop-settings"

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return (e.body as { detail?: string } | undefined)?.detail || e.message
  return (e as Error)?.message || String(e)
}

/** A scope-aware tab body: the Global section first, then a folded list with one
 *  collapsible row per project — a project's values load only when it's opened. */
export function LayeredTab({
  render,
  blurb,
}: {
  render: (scope: Scope, projectId?: string) => ReactNode
  blurb: string
}) {
  const [projects, setProjects] = useState<Project[]>([])
  const [open, setOpen] = useState<Set<string>>(new Set())
  useEffect(() => {
    listProjects().then((r) => setProjects(r.projects)).catch(() => {})
  }, [])
  return (
    <div className="space-y-5 py-1">
      <p className="text-xs text-muted-foreground">{blurb}</p>
      <section>
        <h3 className="mb-2 text-sm font-medium">Global <span className="text-xs font-normal text-muted-foreground">· all projects</span></h3>
        {render("global")}
      </section>
      <section>
        <h3 className="mb-1 text-sm font-medium">Per-project <span className="text-xs font-normal text-muted-foreground">· overrides global by name</span></h3>
        {projects.length === 0 && <p className="text-xs text-muted-foreground">No projects yet.</p>}
        <div className="divide-y divide-border/50">
          {projects.map((p) => {
            const isOpen = open.has(p.id)
            return (
              <div key={p.id}>
                <button
                  type="button"
                  onClick={() => setOpen((s) => { const n = new Set(s); n.has(p.id) ? n.delete(p.id) : n.add(p.id); return n })}
                  className="flex w-full items-center gap-1.5 py-2 text-left text-sm hover:text-foreground"
                >
                  <ChevronRight className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${isOpen ? "rotate-90" : ""}`} />
                  <span className="truncate font-medium" title={p.repo_path}>{p.name}</span>
                </button>
                {isOpen && <div className="pb-3 pl-5">{render("project", p.id)}</div>}
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}

// ============================ Secrets ============================
export function SecretsScope({ scope, projectId }: { scope: Scope; projectId?: string }) {
  const [keys, setKeys] = useState<string[]>([])
  const [inherited, setInherited] = useState<string[]>([])
  const [k, setK] = useState(""); const [v, setV] = useState("")
  const [err, setErr] = useState<string | null>(null)
  const reload = useCallback(() => {
    listDesktopSecrets(scope, projectId)
      .then((r) => { setKeys(r.keys); setInherited(r.inherited) })
      .catch((e) => setErr(errMsg(e)))
  }, [scope, projectId])
  useEffect(reload, [reload])
  async function add() {
    if (!k.trim() || !v) return
    setErr(null)
    try { await setDesktopSecret(k.trim(), v, scope, projectId); setK(""); setV(""); reload() }
    catch (e) { setErr(errMsg(e)) }
  }
  async function del(key: string) {
    setErr(null)
    try { await deleteDesktopSecret(key, scope, projectId); reload() } catch (e) { setErr(errMsg(e)) }
  }
  const inheritedOnly = inherited.filter((i) => !keys.includes(i))
  return (
    <div className="space-y-1.5">
      {keys.length === 0 && inheritedOnly.length === 0 && <p className="text-xs text-muted-foreground">None set.</p>}
      {keys.map((key) => (
        <div key={key} className="flex items-center gap-2 text-sm">
          <span className="font-mono">{key}</span>
          <span className="text-xs text-muted-foreground">••••••</span>
          <button onClick={() => del(key)} className="ml-auto text-muted-foreground hover:text-destructive" title="Delete"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      {inheritedOnly.map((key) => (
        <div key={key} className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="font-mono">{key}</span>
          <span className="rounded bg-muted px-1 text-[10px]">from global</span>
        </div>
      ))}
      <div className="flex gap-1 pt-1">
        <Input value={k} onChange={(e) => setK(e.target.value)} placeholder="KEY" className="w-36 font-mono text-xs" />
        <Input type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="value" className="flex-1" autoComplete="off" />
        <Button size="sm" variant="outline" onClick={add} title="Set secret"><Plus className="h-3.5 w-3.5" /></Button>
      </div>
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

// ============================ MCP ============================
export function McpScope({ scope, projectId }: { scope: Scope; projectId?: string }) {
  const [servers, setServers] = useState<DesktopMcpServer[]>([])
  const [form, setForm] = useState<DesktopMcpServer>({ server_name: "", transport: "http", url: "", credential_key: "" })
  const [err, setErr] = useState<string | null>(null)
  const reload = useCallback(() => {
    listDesktopMcp(scope, projectId).then((r) => setServers(r.servers)).catch((e) => setErr(errMsg(e)))
  }, [scope, projectId])
  useEffect(reload, [reload])
  async function add() {
    if (!form.server_name.trim() || !form.url.trim()) return
    setErr(null)
    try {
      await setDesktopMcp({ ...form, server_name: form.server_name.trim() }, scope, projectId)
      setForm({ server_name: "", transport: "http", url: "", credential_key: "" }); reload()
    } catch (e) { setErr(errMsg(e)) }
  }
  async function del(name: string) {
    setErr(null)
    try { await deleteDesktopMcp(name, scope, projectId); reload() } catch (e) { setErr(errMsg(e)) }
  }
  return (
    <div className="space-y-1.5">
      {servers.length === 0 && <p className="text-xs text-muted-foreground">None set.</p>}
      {servers.map((s) => (
        <div key={s.server_name} className="flex items-center gap-2 text-sm">
          <span className="font-medium">{s.server_name}</span>
          <span className="rounded bg-muted px-1 text-[10px] uppercase">{s.transport}</span>
          <span className="truncate font-mono text-xs text-muted-foreground" title={s.url}>{s.url}</span>
          <button onClick={() => del(s.server_name)} className="ml-auto shrink-0 text-muted-foreground hover:text-destructive" title="Remove"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <div className="grid grid-cols-[7rem_5rem_1fr_auto] gap-1 pt-1">
        <Input value={form.server_name} onChange={(e) => setForm({ ...form, server_name: e.target.value })} placeholder="name" className="text-xs" />
        <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })} className="rounded-md border border-input bg-background px-1 text-xs">
          <option value="http">http</option><option value="sse">sse</option><option value="stdio">stdio</option>
        </select>
        <Input value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://… or stdio command" className="text-xs" />
        <Button size="sm" variant="outline" onClick={add}><Plus className="h-3.5 w-3.5" /></Button>
      </div>
      <Input value={form.credential_key ?? ""} onChange={(e) => setForm({ ...form, credential_key: e.target.value })} placeholder="credential_key (optional — a secret key above)" className="text-xs" />
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

// ============================ Skills ============================
export function SkillsScope({ scope, projectId }: { scope: Scope; projectId?: string }) {
  const [skills, setSkills] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const reload = useCallback(() => {
    listDesktopSkills(scope, projectId).then((r) => setSkills(r.skills)).catch((e) => setErr(errMsg(e)))
  }, [scope, projectId])
  useEffect(reload, [reload])
  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    setErr(null); setBusy(true)
    try {
      const name = f.name.replace(/\.zip$/i, "")
      await uploadDesktopSkill(name, await f.arrayBuffer(), scope, projectId)
      reload()
    } catch (e2) { setErr(errMsg(e2)) } finally { setBusy(false); if (fileRef.current) fileRef.current.value = "" }
  }
  async function addFromDir(path: string) {
    setErr(null); setBusy(true)
    try {
      await addDesktopSkillFromDir(path, scope, projectId)
      reload()
    } catch (e) { setErr(errMsg(e)) } finally { setBusy(false) }
  }
  async function del(name: string) {
    setErr(null)
    try { await deleteDesktopSkill(name, scope, projectId); reload() } catch (e) { setErr(errMsg(e)) }
  }
  return (
    <div className="space-y-1.5">
      {skills.length === 0 && <p className="text-xs text-muted-foreground">None installed.</p>}
      {skills.map((s) => (
        <div key={s} className="flex items-center gap-2 text-sm">
          <span className="font-mono">{s}</span>
          <button onClick={() => del(s)} className="ml-auto text-muted-foreground hover:text-destructive" title="Remove"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <div className="flex flex-wrap items-start gap-2 pt-1">
        <input ref={fileRef} type="file" accept=".zip" onChange={onFile} className="hidden" />
        <FolderPickerButton label="Add skill folder" placeholder="/absolute/path/to/skill-folder" busy={busy} onPick={addFromDir} />
        <Button size="sm" variant="outline" disabled={busy} onClick={() => fileRef.current?.click()}>
          <Upload className="h-3.5 w-3.5" /> {busy ? "Uploading…" : "Upload .zip"}
        </Button>
      </div>
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}

// ---- Working directories (per project; persistent granted scope) ----
export function WorkingDirsScope({ scope, projectId }: { scope: Scope; projectId?: string }) {
  const [projectRoot, setProjectRoot] = useState<string | null>(null)
  const [dirs, setDirs] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const reload = useCallback(() => {
    if (scope !== "project" || !projectId) return
    listWorkingDirs(projectId)
      .then((r) => { setProjectRoot(r.project_root); setDirs(r.dirs) })
      .catch((e) => setErr(errMsg(e)))
  }, [scope, projectId])
  useEffect(reload, [reload])

  // Working directories are per-project only — nothing to show at global scope.
  if (scope !== "project" || !projectId) {
    return (
      <p className="text-xs text-muted-foreground">
        Working directories are per-project — open a project below to grant one.
      </p>
    )
  }

  async function addDir(path: string) {
    setErr(null); setBusy(true)
    try { setDirs((await addWorkingDir(path, projectId!)).dirs) }  // use the returned list
    catch (e) { setErr(errMsg(e)) } finally { setBusy(false) }
  }
  async function del(path: string) {
    setErr(null)
    try { setDirs((await removeWorkingDir(path, projectId!)).dirs) } catch (e) { setErr(errMsg(e)) }
  }
  return (
    <div className="space-y-1.5">
      {projectRoot && (
        <div className="flex items-center gap-2 text-sm">
          <span className="font-mono text-muted-foreground">{projectRoot}</span>
          <span className="ml-auto text-xs text-muted-foreground">project (always)</span>
        </div>
      )}
      {dirs.length === 0 && <p className="text-xs text-muted-foreground">No extra directories granted.</p>}
      {dirs.map((d) => (
        <div key={d} className="flex items-center gap-2 text-sm">
          <span className="font-mono">{d}</span>
          <button onClick={() => del(d)} className="ml-auto text-muted-foreground hover:text-destructive" title="Revoke"><Trash2 className="h-3.5 w-3.5" /></button>
        </div>
      ))}
      <div className="pt-1">
        <FolderPickerButton label="Add directory" placeholder="/absolute/path/to/directory" busy={busy} onPick={addDir} />
      </div>
      {err && <p className="text-xs text-destructive">{err}</p>}
    </div>
  )
}
