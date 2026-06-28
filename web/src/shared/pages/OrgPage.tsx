import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowLeft, Copy, Check, Ban, RotateCcw } from "lucide-react"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { ApiError } from "@/shared/api/client"
import { decodeJwtPayload, getToken } from "@/shared/api/auth"
import {
  listMembers,
  listInvites,
  createInvite,
  revokeInvite,
  setMemberRole,
  disableMember,
  enableMember,
  type Member,
  type PendingInvite,
} from "@/shared/api/org"

/**
 * Team / org management (Phase 3). Admin-only — the server gates every /orgs/*
 * call on the admin role and scopes it to the caller's own tenant. Lets an
 * admin invite members (by shareable link), change roles, and disable/enable
 * accounts. The "last admin" is protected server-side (the action 400s).
 */
const ROLES = ["member", "admin"]

/** Team management (invites + members + roles), embeddable in the Settings
 * modal's Team tab AND wrapped by OrgPage as a deep-link page. */
export function TeamSection() {
  const [members, setMembers] = useState<Member[]>([])
  const [invites, setInvites] = useState<PendingInvite[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [email, setEmail] = useState("")
  const [role, setRole] = useState("member")
  const [inviteLink, setInviteLink] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const payload = decodeJwtPayload(getToken() ?? "")
  const me = (payload?.sub as string) || ""

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([listMembers(), listInvites()])
      .then(([m, i]) => {
        setMembers(m.members)
        setInvites(i.invites)
        setError(null)
      })
      .catch((e) => setError(msg(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(load, [load])

  async function copy(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      /* clipboard blocked — ignore */
    }
  }

  async function handleInvite(e: React.FormEvent) {
    e.preventDefault()
    if (!email.trim()) return
    setError(null)
    try {
      const inv = await createInvite(email.trim(), role)
      setInviteLink(inv.url)
      setEmail("")
      load()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function changeRole(m: Member, next: string) {
    setError(null)
    try {
      await setMemberRole(m.id, next)
      load()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function toggleStatus(m: Member) {
    setError(null)
    try {
      await (m.status === "disabled" ? enableMember(m.id) : disableMember(m.id))
      load()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function revoke(token: string) {
    setError(null)
    try {
      await revokeInvite(token)
      load()
    } catch (err) {
      setError(msg(err))
    }
  }

  return (
      <div className="divide-y divide-border/60">
        {error && (
          <p className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>
        )}

        {/* Invite */}
        <section className="py-5">
          <h2 className="mb-3 text-sm font-semibold">Invite a member</h2>
          <form onSubmit={handleInvite} className="flex flex-wrap items-center gap-2">
            <Input
              type="email"
              placeholder="teammate@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="min-w-[220px] flex-1"
            />
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="h-9 rounded-md border border-input bg-background px-2 text-sm"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
            <Button type="submit">Create invite</Button>
          </form>
          {inviteLink && (
            <div className="mt-3 flex items-center gap-2 rounded-md bg-muted/50 p-2">
              <code className="flex-1 truncate text-xs">{inviteLink}</code>
              <Button variant="outline" size="sm" onClick={() => copy(inviteLink, "new")}>
                {copied === "new" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                Copy link
              </Button>
            </div>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            Share the link with the invitee — they set their own password to join.
          </p>
        </section>

        {/* Members */}
        <section className="py-5">
          <h2 className="mb-3 text-sm font-semibold">
            Members {!loading && `(${members.length})`}
          </h2>
          {loading ? (
            <p className="py-3 text-sm text-muted-foreground">Loading…</p>
          ) : (
            <ul className="divide-y divide-border">
              {members.map((m) => {
                const isMe = m.id === me
                const isOwner = m.roles.includes("owner")
                const disabled = m.status === "disabled"
                return (
                  <li key={m.id} className="flex items-center gap-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm">
                        {m.email}
                        {isMe && <span className="ml-1 text-xs text-muted-foreground">(you)</span>}
                        {disabled && <span className="ml-2 text-xs text-amber-600">disabled</span>}
                      </p>
                      {m.name && <p className="truncate text-xs text-muted-foreground">{m.name}</p>}
                    </div>
                    {isOwner ? (
                      <span
                        className="rounded-md bg-amber-100 px-2 py-1 text-xs font-medium text-amber-800"
                        title="The team owner — can't be reassigned or disabled"
                      >
                        owner
                      </span>
                    ) : (
                      <select
                        value={m.roles[0] || "member"}
                        onChange={(e) => changeRole(m, e.target.value)}
                        disabled={isMe}
                        className="h-8 rounded-md border border-input bg-background px-2 text-xs disabled:opacity-50"
                        title={isMe ? "You can't change your own role" : "Change role"}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>{r}</option>
                        ))}
                      </select>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => toggleStatus(m)}
                      disabled={isMe || isOwner}
                      title={
                        isOwner
                          ? "The owner can't be disabled"
                          : isMe
                            ? "You can't disable yourself"
                            : disabled
                              ? "Re-enable"
                              : "Disable"
                      }
                    >
                      {disabled ? <RotateCcw className="h-3.5 w-3.5" /> : <Ban className="h-3.5 w-3.5" />}
                      {disabled ? "Enable" : "Disable"}
                    </Button>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        {/* Pending invites */}
        {invites.length > 0 && (
          <section className="rounded-lg border border-border">
            <h2 className="border-b border-border px-4 py-2.5 text-sm font-semibold">
              Pending invites ({invites.length})
            </h2>
            <ul className="divide-y divide-border">
              {invites.map((inv) => {
                const url = `${window.location.origin}/invite/${inv.token}`
                return (
                  <li key={inv.token} className="flex items-center gap-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm">{inv.email}</p>
                      <p className="text-xs text-muted-foreground">{inv.role}</p>
                    </div>
                    <Button variant="outline" size="sm" onClick={() => copy(url, inv.token)}>
                      {copied === inv.token ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                      Copy link
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => revoke(inv.token)}>
                      Revoke
                    </Button>
                  </li>
                )
              })}
            </ul>
          </section>
        )}
      </div>
  )
}

export function OrgPage() {
  const org = (decodeJwtPayload(getToken() ?? "")?.tenant as string) || ""
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col">
      <header className="flex items-center gap-3 border-b border-border/60 px-4 py-3">
        <Link to="/">
          <Button variant="ghost" size="icon" title="Back to chat">
            <ArrowLeft className="h-5 w-5" />
          </Button>
        </Link>
        <h1 className="text-lg font-semibold">Team</h1>
        {org && <span className="text-sm text-muted-foreground">· {org}</span>}
      </header>
      <div className="flex-1 p-4">
        <TeamSection />
      </div>
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
