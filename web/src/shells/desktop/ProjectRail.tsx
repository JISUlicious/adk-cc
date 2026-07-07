import { useCallback, useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { Plus, X, Settings as SettingsIcon, ChevronRight, FolderPlus, Trash2, Network } from "lucide-react"
import {
  createSession, deleteSession, listApps, listSessions, type Session,
} from "@/shared/api/sessions"
import { listProjects, addProject, removeProject, removeSessionWorktree, type Project } from "@/shared/api/projects"
import { Button } from "@/shared/components/ui/button"
import { cn } from "@/shared/lib/utils"
import { SessionList } from "@/shared/sessions/SessionList"
import { type RailProps } from "@/shared/components/SessionRail"

/** The Tauri global IPC `invoke`, available only inside the desktop app AND when
 *  the webview was granted IPC for our sidecar origin (tauri.conf `withGlobalTauri`
 *  + a capability listing the remote URL). Null in a plain browser or when IPC
 *  isn't injected — callers then fall back to a typed-path input, because
 *  `window.prompt` is a no-op in the macOS WKWebView Tauri uses. */
function tauriInvoke(): ((cmd: string, args?: unknown) => Promise<unknown>) | null {
  const t = (window as unknown as {
    __TAURI__?: { core?: { invoke: (c: string, a?: unknown) => Promise<unknown> } }
  }).__TAURI__
  return t?.core?.invoke ?? null
}

/**
 * Desktop rail — two levels: projects (L1) → that project's sessions (L2).
 * Each project is a distinct ADK user_id, so selecting a session also switches
 * the active user_id via setUserId so ChatPage's thread ops scope to it.
 */
export function ProjectRail({
  userId, setUserId, appName, onAppChange, sessionId, onSelect, refreshTick,
  open, onClose, onOpenSettings, secretsMissing = 0,
}: RailProps) {
  const [projects, setProjects] = useState<Project[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [sessionsByProject, setSessionsByProject] = useState<Record<string, Session[]>>({})
  const [error, setError] = useState<string | null>(null)
  const [pathPrompt, setPathPrompt] = useState(false)
  const [pathInput, setPathInput] = useState("")
  // Projects mid auto-create, so a double-expand can't spawn two empty sessions.
  const autoCreating = useRef<Set<string>>(new Set())

  useEffect(() => {
    let cancelled = false
    listApps()
      .then((xs) => { if (!cancelled && appName === null && xs.length > 0) onAppChange(xs[0]) })
      .catch((e) => { if (!cancelled) setError(`Failed to load apps: ${e.message}`) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const reloadProjects = useCallback(() => {
    listProjects().then((r) => setProjects(r.projects)).catch((e) => setError(`Failed to load projects: ${e.message}`))
  }, [])
  useEffect(reloadProjects, [reloadProjects])

  const loadSessions = useCallback(async (projectId: string): Promise<Session[]> => {
    if (!appName) return []
    try {
      const xs = await listSessions(appName, projectId)
      xs.sort((a, b) => (b.lastUpdateTime || 0) - (a.lastUpdateTime || 0))
      setSessionsByProject((m) => ({ ...m, [projectId]: xs }))
      return xs
    } catch (e) {
      setError(`Failed to load sessions: ${(e as Error).message}`)
      return []
    }
  }, [appName])

  // Select a project: load its sessions and, if it has NO history, drop the user
  // straight into a fresh session (rather than an empty "click +" list). Plain
  // function (not memoized) so it always closes over the current newSession.
  async function openProject(projectId: string) {
    const xs = await loadSessions(projectId)
    if (xs.length === 0 && !autoCreating.current.has(projectId)) {
      autoCreating.current.add(projectId)
      try {
        await newSession(projectId)
      } finally {
        autoCreating.current.delete(projectId)
      }
    }
  }

  // Reload the active project's sessions when a turn lands (refreshTick) or app loads.
  // Deferred to a microtask so the setState isn't synchronous in the effect body.
  useEffect(() => {
    if (userId && expanded.has(userId)) void Promise.resolve().then(() => loadSessions(userId))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appName, refreshTick])

  function toggle(projectId: string) {
    const wasOpen = expanded.has(projectId)
    setExpanded((prev) => {
      const next = new Set(prev)
      if (wasOpen) next.delete(projectId)
      else next.add(projectId)
      return next
    })
    if (!wasOpen) void openProject(projectId) // expanding = selecting → auto-open
  }

  async function doAddProject(path: string) {
    try {
      const { project } = await addProject(path)
      reloadProjects()
      setExpanded((prev) => new Set(prev).add(project.id))
      // A new project has no history → openProject drops the user into a fresh session.
      await openProject(project.id)
    } catch (e) {
      setError(`Failed to add project: ${(e as Error).message}`)
    }
  }

  async function addNew() {
    setError(null)
    const invoke = tauriInvoke()
    if (invoke) {
      // Native folder picker (Tauri dialog plugin).
      try {
        const picked = await invoke("plugin:dialog|open", { options: { directory: true, multiple: false } })
        if (typeof picked === "string") await doAddProject(picked)
      } catch (e) {
        setError(`Folder picker failed: ${(e as Error).message}`)
      }
    } else {
      // No native IPC → reveal a typed-path input (window.prompt is a no-op here).
      setPathInput("")
      setPathPrompt((v) => !v)
    }
  }

  async function submitPath() {
    const p = pathInput.trim()
    setPathPrompt(false)
    if (p) await doAddProject(p)
  }

  async function removeProj(p: Project) {
    if (!confirm(`Remove project "${p.name}"? (its folder is left untouched)`)) return
    try {
      await removeProject(p.id)
      reloadProjects()
      if (userId === p.id) onSelect(null)
    } catch (e) {
      setError(`Failed to remove project: ${(e as Error).message}`)
    }
  }

  async function newSession(projectId: string) {
    if (!appName) return
    try {
      const s = await createSession(appName, projectId, {})
      setSessionsByProject((m) => ({ ...m, [projectId]: [s, ...(m[projectId] ?? [])] }))
      setExpanded((prev) => new Set(prev).add(projectId))
      setUserId?.(projectId)
      onSelect(s)
    } catch (e) {
      setError(`Failed to create session: ${(e as Error).message}`)
    }
  }

  async function deleteSess(projectId: string, s: Session) {
    if (!confirm(`Delete session ${s.id.slice(0, 8)}…?`)) return
    try {
      await deleteSession(appName!, projectId, s.id)
      await removeSessionWorktree(projectId, s.id).catch(() => {})
      setSessionsByProject((m) => ({ ...m, [projectId]: (m[projectId] ?? []).filter((x) => x.id !== s.id) }))
      if (userId === projectId && sessionId === s.id) onSelect(null)
    } catch (e) {
      setError(`Failed to delete: ${(e as Error).message}`)
    }
  }

  return (
    <>
      {open && (
        <div className="fixed inset-0 z-30 bg-foreground/30 lg:hidden" aria-hidden onClick={onClose} />
      )}
      <aside
        className={cn(
          "adk-rail flex w-72 max-w-[85vw] flex-col border-r border-border/60",
          "bg-muted shadow-xl lg:bg-muted/40 lg:shadow-none",
          "fixed inset-y-0 left-0 z-40 transform transition-transform duration-200 ease-out",
          "lg:static lg:z-auto lg:max-w-none lg:translate-x-0 lg:transition-none",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <button
          type="button" onClick={onClose} title="Close"
          className="absolute right-2 top-2 z-10 rounded-md p-1.5 text-muted-foreground hover:bg-accent lg:hidden"
        >
          <X className="h-4 w-4" />
        </button>
        <div className="flex items-center gap-2 px-4 py-3.5">
          <img src="/favicon.svg" alt="" className="h-6 w-6 shrink-0" />
          <span className="text-base font-semibold tracking-tight">adk-cc</span>
        </div>
        <div className="adk-rail-header flex items-center justify-between px-4 py-2">
          <span className="text-xs font-medium text-muted-foreground">Projects</span>
          <Button size="sm" variant="outline" onClick={addNew} title="Add a project folder" className="adk-add-project">
            <FolderPlus className="h-3.5 w-3.5" /> Add
          </Button>
        </div>
        {pathPrompt && (
          <div className="flex gap-1 px-4 pb-2">
            <input
              autoFocus
              value={pathInput}
              onChange={(e) => setPathInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submitPath()
                else if (e.key === "Escape") setPathPrompt(false)
              }}
              placeholder="/absolute/path/to/project"
              className="min-w-0 flex-1 rounded border border-input bg-background px-2 py-1 font-mono text-xs"
            />
            <Button size="sm" variant="outline" onClick={submitPath}>Add</Button>
          </div>
        )}
        {error && <p className="px-4 py-2 text-xs text-destructive">{error}</p>}

        <div className="flex-1 overflow-y-auto">
          {projects.length === 0 && (
            <p className="px-4 py-3 text-xs text-muted-foreground">
              No projects yet. Click <span className="font-mono">Add</span> to open a folder.
            </p>
          )}
          {projects.map((p) => {
            const isOpen = expanded.has(p.id)
            const sessions = sessionsByProject[p.id] ?? []
            return (
              <div key={p.id} className="adk-project-row border-b border-border/40">
                {/* L1: project row */}
                <div className="group flex items-center gap-1 px-2 py-2 hover:bg-accent/60">
                  <button type="button" onClick={() => toggle(p.id)} className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
                    <ChevronRight className={cn("h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform", isOpen && "rotate-90")} />
                    <span className="truncate text-sm font-medium" title={p.repo_path}>{p.name}</span>
                  </button>
                  <button
                    type="button" onClick={() => newSession(p.id)} title="New session in this project"
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-foreground"
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button" onClick={() => removeProj(p)} title="Remove project"
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
                {/* L2: sessions (lazy) */}
                {isOpen && (
                  <div className="pb-1 pl-3">
                    <SessionList
                      sessions={sessions}
                      selectedId={userId === p.id ? sessionId : null}
                      onSelect={(s) => { setUserId?.(p.id); onSelect(s) }}
                      onDelete={(s) => deleteSess(p.id, s)}
                      emptyHint={<>No sessions — click <span className="font-mono">+</span> on the project.</>}
                    />
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {/* Footer: knowledge graph + settings — no identity / sign-out on desktop. */}
        <div className="border-t border-border/60 p-2">
          <Link
            to={userId ? `/knowledge?user=${encodeURIComponent(userId)}` : "/knowledge"}
            className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm text-muted-foreground hover:bg-accent"
            title="Knowledge graph — wiki + this project's memory"
          >
            <Network className="h-4 w-4" />
            Knowledge
          </Link>
          <button
            type="button" onClick={onOpenSettings}
            className="adk-rail-settings flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm text-muted-foreground hover:bg-accent"
            title={secretsMissing > 0 ? `Settings — ${secretsMissing} value(s) need setup` : "Settings"}
          >
            <span className="relative">
              <SettingsIcon className="h-4 w-4" />
              {secretsMissing > 0 && (
                <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[9px] font-medium text-white">
                  {secretsMissing}
                </span>
              )}
            </span>
            Settings
          </button>
        </div>
      </aside>
    </>
  )
}
