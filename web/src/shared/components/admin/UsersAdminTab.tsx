import { useCallback, useEffect, useState } from "react"
import { UserPlus, Ban, RotateCcw } from "lucide-react"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ApiError } from "@/shared/api/client"
import { decodeJwtPayload, getToken } from "@/shared/api/auth"
import {
  listMembers,
  createUser,
  setMemberRole,
  disableMember,
  enableMember,
  type Member,
} from "@/shared/api/org"

/**
 * Admin → Users tab (Phase 5). Direct provisioning for org admins: create a
 * user with an initial password + role, and manage roles / disabled state.
 * Owner rows are protected (server-enforced; controls locked here too).
 * Invite-by-link lives on the Team page (/org); this is the credentialed path.
 */
const ROLES = ["member", "admin"]

export function UsersAdminTab() {
  const [members, setMembers] = useState<Member[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [name, setName] = useState("")
  const [role, setRole] = useState("member")
  const [status, setStatus] = useState<string | null>(null)

  const me = (decodeJwtPayload(getToken() ?? "")?.sub as string) || ""

  const reload = useCallback(() => {
    setLoading(true)
    listMembers()
      .then((r) => { setMembers(r.members); setError(null) })
      .catch((e) => setError(msg(e)))
      .finally(() => setLoading(false))
  }, [])
  useEffect(reload, [reload])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setStatus(null)
    try {
      await createUser(email.trim(), password, name.trim(), role)
      setStatus(`Created ${email.trim()}.`)
      setEmail(""); setPassword(""); setName("")
      reload()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function changeRole(m: Member, next: string) {
    setError(null)
    try { await setMemberRole(m.id, next); reload() } catch (err) { setError(msg(err)) }
  }
  async function toggle(m: Member) {
    setError(null)
    try {
      await (m.status === "disabled" ? enableMember(m.id) : disableMember(m.id))
      reload()
    } catch (err) { setError(msg(err)) }
  }

  return (
    <div className="space-y-6">
      {error && <p className="text-sm text-destructive">{error}</p>}

      <section className="rounded-lg border border-border p-4">
        <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
          <UserPlus className="h-4 w-4" /> Create a user
        </h2>
        <form onSubmit={create} className="flex flex-wrap items-end gap-2">
          <div className="flex-1 min-w-[180px] space-y-1">
            <label className="text-xs text-muted-foreground">Email</label>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="user@acme.io" />
          </div>
          <div className="flex-1 min-w-[160px] space-y-1">
            <label className="text-xs text-muted-foreground">Initial password</label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="min 8 chars" />
          </div>
          <div className="min-w-[120px] space-y-1">
            <label className="text-xs text-muted-foreground">Name</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="optional" />
          </div>
          <div className="min-w-[110px] space-y-1">
            <label className="block text-xs text-muted-foreground">Role</label>
            <select value={role} onChange={(e) => setRole(e.target.value)}
                    className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm">
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <Button type="submit">Create</Button>
        </form>
        {status && <p className="mt-2 text-xs text-muted-foreground">{status}</p>}
      </section>

      <section className="rounded-lg border border-border">
        <h2 className="border-b border-border px-4 py-2.5 text-sm font-semibold">
          Users {!loading && `(${members.length})`}
        </h2>
        {loading ? (
          <p className="px-4 py-3 text-sm text-muted-foreground">Loading…</p>
        ) : (
          <ul className="divide-y divide-border">
            {members.map((m) => {
              const isMe = m.id === me
              const isOwner = m.roles.includes("owner")
              const disabled = m.status === "disabled"
              return (
                <li key={m.id} className="flex items-center gap-3 px-4 py-2.5">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">
                      {m.email}
                      {isMe && <span className="ml-1 text-xs text-muted-foreground">(you)</span>}
                      {disabled && <span className="ml-2 text-xs text-amber-600">disabled</span>}
                    </p>
                    {m.name && <p className="truncate text-xs text-muted-foreground">{m.name}</p>}
                  </div>
                  {isOwner ? (
                    <span className="rounded-md bg-amber-100 px-2 py-1 text-xs font-medium text-amber-800">owner</span>
                  ) : (
                    <select value={m.roles[0] || "member"} onChange={(e) => changeRole(m, e.target.value)}
                            disabled={isMe}
                            className="h-8 rounded-md border border-input bg-background px-2 text-xs disabled:opacity-50">
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  )}
                  <Button variant="outline" size="sm" onClick={() => toggle(m)} disabled={isMe || isOwner}>
                    {disabled ? <RotateCcw className="h-3.5 w-3.5" /> : <Ban className="h-3.5 w-3.5" />}
                    {disabled ? "Enable" : "Disable"}
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </section>
    </div>
  )
}

function msg(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    if (err.status === 403) return "Admin access required."
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
