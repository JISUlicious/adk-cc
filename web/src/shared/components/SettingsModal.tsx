import { useEffect, useState } from "react"
import {
  LogOut, User, KeyRound, Server, Boxes,
  BarChart3, Users, SlidersHorizontal, Trash2, Plus, Palette,
} from "lucide-react"
import { Button } from "./ui/button"
import { Input } from "./ui/input"
import { clearToken, maybeAdmin, markSignedOut } from "@/shared/api/auth"
import { revokeSession } from "@/shared/api/identity"
import { ApiError } from "@/shared/api/client"
import {
  AccountInfoSections, CustomVariablesSection, UserMcpSection, UserSkillsSection, ApiKeysSection,
} from "@/shared/pages/AccountPage"
import { McpAdminTab } from "./admin/McpAdminTab"
import { SkillsAdminTab } from "./admin/SkillsAdminTab"
import { UsageAdminTab } from "./admin/UsageAdminTab"
import { AuditAdminTab } from "./admin/AuditAdminTab"
import { ModelAdminTab } from "./admin/ModelAdminTab"
import { TeamSection } from "@/shared/pages/OrgPage"
import { listCredentialKeys, putCredential, deleteCredential } from "@/shared/api/admin"
import { SettingsFrame, useSecretBadges, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection, AdminBlock } from "@/shared/settings/sections"

/**
 * Web settings: the full topic-centric tab set, with org/admin controls folded
 * into per-topic tabs (role-gated by maybeAdmin). Composes the shared
 * SettingsFrame; the desktop shell composes the same frame with a subset.
 */
export function SettingsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const isAdmin = maybeAdmin()
  const miss = useSecretBadges(open)

  const tabs: SettingsTab[] = [
    { id: "account", label: "Account", icon: User,
      render: () => (<><AccountInfoSections /><CustomVariablesSection /></>) },
    { id: "appearance", label: "Appearance", icon: Palette, render: () => <ThemeSection /> },
    { id: "mcp", label: "MCP", icon: Server, badge: miss.mcp,
      render: () => (<><UserMcpSection />{isAdmin && <AdminBlock title="Org MCP servers"><McpAdminTab /></AdminBlock>}</>) },
    { id: "skills", label: "Skills", icon: Boxes, badge: miss.skill,
      render: () => (<><UserSkillsSection />{isAdmin && <AdminBlock title="Org skills"><SkillsAdminTab /></AdminBlock>}</>) },
    { id: "apikeys", label: "API keys", icon: KeyRound, render: () => <ApiKeysSection /> },
    ...(isAdmin
      ? ([
          { id: "usage", label: "Usage", icon: BarChart3,
            render: () => (<><AdminBlock title="Usage"><UsageAdminTab /></AdminBlock><AdminBlock title="Audit log"><AuditAdminTab /></AdminBlock></>) },
          { id: "team", label: "Team", icon: Users, render: () => <TeamSection /> },
          { id: "advanced", label: "Advanced", icon: SlidersHorizontal,
            render: () => (<><AdminBlock title="Model endpoints"><ModelAdminTab /></AdminBlock><OrgCredentialsSection /></>) },
        ] as SettingsTab[])
      : []),
  ]

  const footer = (
    <Button
      variant="ghost" size="sm" className="w-full justify-start text-muted-foreground"
      onClick={() => { revokeSession(); markSignedOut(); clearToken(); location.assign("/") }}
    >
      <LogOut className="h-3.5 w-3.5" /> Sign out
    </Button>
  )

  return <SettingsFrame open={open} onClose={onClose} tabs={tabs} footer={footer} />
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
