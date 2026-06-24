import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowLeft, Copy, Check, Trash2, KeyRound } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiError } from "@/api/client"
import {
  getMe,
  updateProfile,
  changePassword,
  listApiKeys,
  createApiKey,
  revokeApiKey,
  type Me,
  type ApiKey,
} from "@/api/account"

/**
 * Account self-service (Phase 4): profile (name), change password, and personal
 * access tokens (create → shown once → revoke). All scoped to the signed-in user.
 */
export function AccountPage() {
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    getMe().then(setMe).catch((e) => setError(msg(e)))
  }, [])
  useEffect(reload, [reload])

  return (
    <div className="mx-auto flex min-h-screen max-w-2xl flex-col">
      <header className="flex items-center gap-3 border-b border-border/60 px-4 py-3">
        <Link to="/">
          <Button variant="ghost" size="icon" title="Back to chat">
            <ArrowLeft className="h-5 w-5" />
          </Button>
        </Link>
        <h1 className="text-lg font-semibold">Account</h1>
      </header>

      <div className="flex-1 space-y-6 p-4">
        {error && (
          <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>
        )}
        <ProfileSection me={me} onSaved={setMe} />
        <PasswordSection />
        <ApiKeysSection />
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {children}
    </section>
  )
}

function ProfileSection({ me, onSaved }: { me: Me | null; onSaved: (m: Me) => void }) {
  const [name, setName] = useState("")
  const [status, setStatus] = useState<string | null>(null)
  useEffect(() => setName(me?.name ?? ""), [me])

  async function save(e: React.FormEvent) {
    e.preventDefault()
    setStatus(null)
    try {
      const m = await updateProfile(name.trim())
      onSaved(m)
      setStatus("Saved.")
    } catch (err) {
      setStatus(msg(err))
    }
  }

  if (!me) return <Section title="Profile"><p className="text-sm text-muted-foreground">Loading…</p></Section>
  return (
    <Section title="Profile">
      <form onSubmit={save} className="space-y-3">
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">Email</label>
          <Input value={me.email} disabled readOnly />
        </div>
        <div className="space-y-1">
          <label className="text-xs text-muted-foreground">Name</label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" />
        </div>
        <div className="flex items-center gap-3">
          <Button type="submit">Save</Button>
          {status && <span className="text-xs text-muted-foreground">{status}</span>}
          <span className="ml-auto text-xs text-muted-foreground">
            org: {me.tenant} · {me.roles.join(", ") || "member"}
          </span>
        </div>
      </form>
    </Section>
  )
}

function PasswordSection() {
  const [cur, setCur] = useState("")
  const [next, setNext] = useState("")
  const [status, setStatus] = useState<string | null>(null)
  const [ok, setOk] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setStatus(null)
    setOk(false)
    try {
      await changePassword(cur, next)
      setOk(true)
      setStatus("Password changed.")
      setCur("")
      setNext("")
    } catch (err) {
      setStatus(msg(err))
    }
  }

  return (
    <Section title="Change password">
      <form onSubmit={submit} className="space-y-3">
        <Input type="password" value={cur} onChange={(e) => setCur(e.target.value)}
               placeholder="Current password" autoComplete="current-password" />
        <Input type="password" value={next} onChange={(e) => setNext(e.target.value)}
               placeholder="New password (min 8 chars)" autoComplete="new-password" />
        <div className="flex items-center gap-3">
          <Button type="submit" disabled={!cur || !next}>Update password</Button>
          {status && (
            <span className={`text-xs ${ok ? "text-muted-foreground" : "text-destructive"}`}>{status}</span>
          )}
        </div>
      </form>
    </Section>
  )
}

function ApiKeysSection() {
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [name, setName] = useState("")
  const [fresh, setFresh] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    listApiKeys().then((r) => setKeys(r.keys)).catch((e) => setError(msg(e)))
  }, [])
  useEffect(reload, [reload])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const k = await createApiKey(name.trim() || "api key")
      setFresh(k.token)
      setName("")
      reload()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function revoke(id: string) {
    setError(null)
    try {
      await revokeApiKey(id)
      reload()
    } catch (err) {
      setError(msg(err))
    }
  }

  async function copy() {
    if (!fresh) return
    try {
      await navigator.clipboard.writeText(fresh)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked */
    }
  }

  return (
    <Section title="API keys">
      <p className="mb-3 text-xs text-muted-foreground">
        Personal access tokens for programmatic API access (Bearer). Shown once at creation.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      <form onSubmit={create} className="mb-3 flex items-center gap-2">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="key name (e.g. ci)" className="flex-1" />
        <Button type="submit"><KeyRound className="h-3.5 w-3.5" /> Create key</Button>
      </form>
      {fresh && (
        <div className="mb-3 rounded-md bg-muted/50 p-2">
          <p className="mb-1 text-xs text-muted-foreground">Copy your new token now — it won't be shown again:</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 truncate text-xs">{fresh}</code>
            <Button variant="outline" size="sm" onClick={copy}>
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />} Copy
            </Button>
          </div>
        </div>
      )}
      {keys.length === 0 ? (
        <p className="text-sm text-muted-foreground">No API keys.</p>
      ) : (
        <ul className="divide-y divide-border">
          {keys.map((k) => (
            <li key={k.id} className="flex items-center gap-3 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">{k.name}</p>
                <p className="text-xs text-muted-foreground">
                  created {k.created?.slice(0, 10) || "—"}
                  {k.last_used ? ` · last used ${k.last_used.slice(0, 10)}` : " · never used"}
                </p>
              </div>
              <Button variant="outline" size="sm" onClick={() => revoke(k.id)}>
                <Trash2 className="h-3.5 w-3.5 text-destructive" /> Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function msg(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
