import { useEffect, useState } from "react"
import {
  X, Moon, Sun, Monitor, LogOut, User, KeyRound, Server, Boxes,
  BarChart3, Users, SlidersHorizontal, Trash2, Plus,
} from "lucide-react"
import { Button } from "./ui/button"
import { Input } from "./ui/input"
import { useTheme, type ThemeMode } from "@/lib/theme"
import { clearToken, maybeAdmin, markSignedOut } from "@/api/auth"
import { ApiError } from "@/api/client"
import { cn } from "@/lib/utils"
import { AccountInfoSections, SecretsSection, UserMcpSection, UserSkillsSection } from "@/pages/AccountPage"
import { McpAdminTab } from "./admin/McpAdminTab"
import { SkillsAdminTab } from "./admin/SkillsAdminTab"
import { UsageAdminTab } from "./admin/UsageAdminTab"
import { AuditAdminTab } from "./admin/AuditAdminTab"
import { ModelAdminTab } from "./admin/ModelAdminTab"
import { TeamSection } from "@/pages/OrgPage"
import { listCredentialKeys, putCredential, deleteCredential } from "@/api/admin"

/**
 * Topic-centric settings. The gear opens this wide tabbed modal; each tab holds
 * the user's personal controls plus the org/admin controls (when the caller is
 * an admin). Replaces the old link-list popup + the standalone Admin page
 * (which lives on only as a deep-link route).
 */

type TabId = "account" | "secrets" | "mcp" | "skills" | "usage" | "team" | "advanced"

const TABS: { id: TabId; label: string; icon: typeof User; admin: boolean }[] = [
  { id: "account", label: "Account", icon: User, admin: false },
  { id: "secrets", label: "Secrets", icon: KeyRound, admin: false },
  { id: "mcp", label: "MCP", icon: Server, admin: false },
  { id: "skills", label: "Skills", icon: Boxes, admin: false },
  { id: "usage", label: "Usage", icon: BarChart3, admin: true },
  { id: "team", label: "Team", icon: Users, admin: true },
  { id: "advanced", label: "Advanced", icon: SlidersHorizontal, admin: true },
]

export function SettingsModal({
  open,
  onClose,
  secretsMissing = 0,
}: {
  open: boolean
  onClose: () => void
  secretsMissing?: number
}) {
  const isAdmin = maybeAdmin()
  const [tab, setTab] = useState<TabId>("account")

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null
  const tabs = TABS.filter((t) => !t.admin || isAdmin)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="flex h-[80vh] w-full max-w-3xl overflow-hidden rounded-lg border border-border bg-background shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        {/* sidebar */}
        <aside className="flex w-44 flex-col border-r border-border/60 bg-muted/30">
          <div className="flex items-center justify-between px-3 py-3">
            <h2 className="font-medium">Settings</h2>
          </div>
          <nav className="flex-1 space-y-0.5 px-2">
            {tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
                  tab === t.id ? "bg-brand-tint font-medium text-foreground" : "text-muted-foreground hover:bg-accent",
                )}
              >
                <t.icon className="h-3.5 w-3.5" />
                {t.label}
                {t.id === "secrets" && secretsMissing > 0 && (
                  <span className="ml-auto flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-medium text-white">
                    {secretsMissing}
                  </span>
                )}
              </button>
            ))}
          </nav>
          <div className="border-t border-border/60 p-2">
            <Button
              variant="ghost" size="sm" className="w-full justify-start text-muted-foreground"
              onClick={() => { markSignedOut(); clearToken(); location.assign("/") }}
            >
              <LogOut className="h-3.5 w-3.5" /> Sign out
            </Button>
          </div>
        </aside>

        {/* content */}
        <div className="relative flex-1 overflow-y-auto">
          <button
            type="button" onClick={onClose} aria-label="Close"
            className="absolute right-3 top-3 z-10 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
          <div className="space-y-5 p-5">
            {tab === "account" && (<><ThemeSection /><AccountInfoSections /></>)}
            {tab === "secrets" && (<><SecretsSection />{isAdmin && <OrgCredentialsSection />}</>)}
            {tab === "mcp" && (<><UserMcpSection />{isAdmin && <AdminBlock title="Org MCP servers"><McpAdminTab /></AdminBlock>}</>)}
            {tab === "skills" && (<><UserSkillsSection />{isAdmin && <AdminBlock title="Org skills"><SkillsAdminTab /></AdminBlock>}</>)}
            {tab === "usage" && (<><AdminBlock title="Usage"><UsageAdminTab /></AdminBlock><AdminBlock title="Audit log"><AuditAdminTab /></AdminBlock></>)}
            {tab === "team" && (<TeamSection />)}
            {tab === "advanced" && (<AdminBlock title="Model endpoints"><ModelAdminTab /></AdminBlock>)}
          </div>
        </div>
      </div>
    </div>
  )
}

function AdminBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border p-4">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">admin · org</span>
      </div>
      {children}
    </section>
  )
}

function ThemeSection() {
  const [mode, setMode] = useTheme()
  const opt = (value: ThemeMode, label: string, Icon: typeof Sun) => (
    <button
      type="button" onClick={() => setMode(value)}
      className={cn(
        "flex flex-1 flex-col items-center gap-1 rounded-md border px-2 py-3 text-xs transition-colors",
        value === mode ? "border-primary bg-brand-tint" : "border-input hover:bg-accent",
      )}
    >
      <Icon className="h-4 w-4" />
      {label}
    </button>
  )
  return (
    <section className="rounded-lg border border-border p-4">
      <h3 className="mb-3 text-sm font-semibold">Appearance</h3>
      <div className="flex gap-2">
        {opt("light", "Light", Sun)}
        {opt("dark", "Dark", Moon)}
        {opt("system", "System", Monitor)}
      </div>
    </section>
  )
}

function OrgCredentialsSection() {
  const [keys, setKeys] = useState<string[]>([])
  const [k, setK] = useState("")
  const [v, setV] = useState("")
  const [error, setError] = useState<string | null>(null)

  function reload() {
    listCredentialKeys().then(setKeys).catch((e) => setError(errMsg(e)))
  }
  useEffect(reload, [])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!k.trim() || !v) return
    try { await putCredential(k.trim(), v); setK(""); setV(""); reload() } catch (err) { setError(errMsg(err)) }
  }
  async function remove(key: string) {
    try { await deleteCredential(key); reload() } catch (err) { setError(errMsg(err)) }
  }

  return (
    <AdminBlock title="Org credentials">
      <p className="mb-3 text-xs text-muted-foreground">
        Tenant-shared secrets — every member inherits these unless they set their own. Names only;
        values are write-only.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {keys.length > 0 && (
        <ul className="mb-3 divide-y divide-border">
          {keys.map((key) => (
            <li key={key} className="flex items-center gap-2 py-2">
              <code className="flex-1 truncate text-sm">{key}</code>
              <Button variant="ghost" size="sm" onClick={() => remove(key)} title="Remove">
                <Trash2 className="h-3.5 w-3.5 text-destructive" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={add} className="flex items-center gap-2 border-t border-border/60 pt-3">
        <Input value={k} onChange={(e) => setK(e.target.value)} placeholder="KEY" className="w-40 font-mono text-xs" />
        <Input type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="value" className="flex-1" autoComplete="off" />
        <Button type="submit" size="sm" disabled={!k.trim() || !v}><Plus className="h-3.5 w-3.5" /> Add</Button>
      </form>
    </AdminBlock>
  )
}

function errMsg(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
