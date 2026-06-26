import { useCallback, useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowLeft, Copy, Check, Trash2, KeyRound, Plus } from "lucide-react"
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
  listSecrets,
  setSecret,
  deleteSecret,
  listUserMcpServers,
  putUserMcpServer,
  deleteUserMcpServer,
  listUserSkills,
  uploadUserSkill,
  deleteUserSkill,
  type Me,
  type ApiKey,
  type SecretInput,
  type SecretGroup,
  type SecretsView,
  type UserMcpServer,
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
        <SecretsSection />
        <UserMcpSection />
        <UserSkillsSection />
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

function SecretStatusBadge({ status, required }: { status: SecretInput["status"]; required: boolean }) {
  if (status === "user")
    return <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600">Set</span>
  if (status === "tenant")
    return <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">From org</span>
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${required ? "bg-amber-500/15 text-amber-600" : "bg-muted text-muted-foreground"}`}>
      {required ? "Needs setup" : "Not set"}
    </span>
  )
}

function SecretRow({ item, onChanged, onError }: { item: SecretInput; onChanged: () => void; onError: (m: string) => void }) {
  const [value, setValue] = useState("")
  const [saved, setSaved] = useState(false)

  async function save(e: React.FormEvent) {
    e.preventDefault()
    if (!value) return
    try {
      await setSecret(item.key, value)
      setValue("")
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
      onChanged()
    } catch (err) {
      onError(msg(err))
    }
  }

  async function remove() {
    try {
      await deleteSecret(item.key)
      onChanged()
    } catch (err) {
      onError(msg(err))
    }
  }

  return (
    <li className="py-2">
      <div className="flex items-center gap-2">
        <code className="text-sm">{item.key}</code>
        <SecretStatusBadge status={item.status} required={item.required} />
        {saved && <Check className="h-3.5 w-3.5 text-emerald-600" />}
        {item.status === "user" && (
          <Button variant="ghost" size="sm" className="ml-auto" onClick={remove} title="Remove your value">
            <Trash2 className="h-3.5 w-3.5 text-destructive" />
          </Button>
        )}
      </div>
      {item.description && <p className="mt-0.5 text-xs text-muted-foreground">{item.description}</p>}
      <form onSubmit={save} className="mt-1 flex items-center gap-2">
        <Input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={item.status === "unset" ? "enter value" : "update value"}
          className="flex-1"
          autoComplete="off"
        />
        <Button type="submit" size="sm" disabled={!value}>Save</Button>
      </form>
    </li>
  )
}

function SecretGroupCard({ group, onChanged, onError }: { group: SecretGroup; onChanged: () => void; onError: (m: string) => void }) {
  const label = group.kind === "mcp" ? `MCP · ${group.name}` : `Skill · ${group.name}`
  return (
    <div className="rounded-md border border-border/60 p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="text-xs font-medium">{label}</span>
        {group.missing > 0 && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
            {group.missing} needs setup
          </span>
        )}
      </div>
      <ul className="divide-y divide-border/60">
        {group.inputs.map((it) => (
          <SecretRow key={it.key} item={it} onChanged={onChanged} onError={onError} />
        ))}
      </ul>
    </div>
  )
}

function SecretsSection() {
  const [view, setView] = useState<SecretsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newKey, setNewKey] = useState("")
  const [newVal, setNewVal] = useState("")

  const reload = useCallback(() => {
    listSecrets().then(setView).catch((e) => setError(msg(e)))
  }, [])
  useEffect(reload, [reload])

  async function addCustom(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    const k = newKey.trim()
    if (!k || !newVal) return
    try {
      await setSecret(k, newVal)
      setNewKey("")
      setNewVal("")
      reload()
    } catch (err) {
      setError(msg(err))
    }
  }

  const groups = view?.groups ?? []
  const other = view?.other ?? []
  const missing = view?.missing_required ?? 0

  return (
    <Section title="Secrets">
      <p className="mb-3 text-xs text-muted-foreground">
        Credentials your skills and MCP servers need (API keys, tokens), grouped by what requires
        them. Stored encrypted, resolved per request, and <strong>never shown again or sent to the
        model</strong>. Your personal value overrides any your org provides.
        {missing > 0 && (
          <span className="ml-1 font-medium text-amber-600">{missing} required value{missing === 1 ? "" : "s"} not set.</span>
        )}
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {groups.length === 0 && other.length === 0 ? (
        <p className="text-sm text-muted-foreground">No secrets required or set.</p>
      ) : (
        <div className="space-y-3">
          {groups.map((g) => (
            <SecretGroupCard key={`${g.kind}:${g.name}`} group={g} onChanged={reload} onError={setError} />
          ))}
          {other.length > 0 && (
            <div className="rounded-md border border-border/60 p-3">
              <div className="mb-1 text-xs font-medium text-muted-foreground">Other</div>
              <ul className="divide-y divide-border/60">
                {other.map((it) => (
                  <SecretRow key={it.key} item={it} onChanged={reload} onError={setError} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      <form onSubmit={addCustom} className="mt-3 flex items-center gap-2 border-t border-border/60 pt-3">
        <Input
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="CUSTOM_KEY"
          className="w-40 font-mono text-xs"
        />
        <Input
          type="password"
          value={newVal}
          onChange={(e) => setNewVal(e.target.value)}
          placeholder="value"
          className="flex-1"
          autoComplete="off"
        />
        <Button type="submit" size="sm" disabled={!newKey.trim() || !newVal}>
          <Plus className="h-3.5 w-3.5" /> Add
        </Button>
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

function UserMcpSection() {
  const [servers, setServers] = useState<UserMcpServer[] | null>(null)
  const [available, setAvailable] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [transport, setTransport] = useState("http")
  const [url, setUrl] = useState("")
  const [credKey, setCredKey] = useState("")

  const reload = useCallback(() => {
    listUserMcpServers()
      .then((s) => { setServers(s); setAvailable(true) })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) setAvailable(false)
        else setError(msg(e))
      })
  }, [])
  useEffect(reload, [reload])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!name.trim() || !url.trim()) return
    try {
      await putUserMcpServer({ server_name: name.trim(), transport, url: url.trim(), credential_key: credKey.trim() || null })
      setName(""); setUrl(""); setCredKey("")
      reload()
    } catch (err) { setError(msg(err)) }
  }
  async function remove(n: string) {
    try { await deleteUserMcpServer(n); reload() } catch (err) { setError(msg(err)) }
  }

  if (!available) return null
  const list = servers ?? []
  return (
    <Section title="Your MCP servers">
      <p className="mb-3 text-xs text-muted-foreground">
        Personal MCP servers, used alongside your org's (yours win on a name clash). Put each
        server's token in <strong>Secrets</strong> above under its <code>credential_key</code>.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {list.length > 0 && (
        <ul className="mb-3 divide-y divide-border">
          {list.map((s) => (
            <li key={s.server_name} className="flex items-center gap-2 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">
                  <span className="font-mono">{s.server_name}</span>{" "}
                  <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">{s.transport}</span>{" "}
                  {s.scope === "tenant"
                    ? <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">From org</span>
                    : <span className="rounded bg-emerald-500/15 px-1 py-0.5 text-[10px] text-emerald-600">Personal</span>}
                </p>
                <p className="truncate text-xs text-muted-foreground">{s.url}{s.credential_key ? ` · token: ${s.credential_key}` : ""}</p>
              </div>
              {s.scope !== "tenant" && (
                <Button variant="ghost" size="sm" onClick={() => remove(s.server_name)} title="Remove">
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={add} className="space-y-2 border-t border-border/60 pt-3">
        <div className="flex items-center gap-2">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="server name" className="w-40 font-mono text-xs" />
          <select value={transport} onChange={(e) => setTransport(e.target.value)}
                  className="h-9 rounded-md border border-input bg-background px-2 text-sm">
            <option value="http">http</option>
            <option value="sse">sse</option>
            <option value="stdio">stdio</option>
          </select>
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://… or command" className="flex-1" />
        </div>
        <div className="flex items-center gap-2">
          <Input value={credKey} onChange={(e) => setCredKey(e.target.value)} placeholder="credential_key (optional)" className="flex-1 font-mono text-xs" />
          <Button type="submit" size="sm" disabled={!name.trim() || !url.trim()}><Plus className="h-3.5 w-3.5" /> Add</Button>
        </div>
      </form>
    </Section>
  )
}

function UserSkillsSection() {
  const [skills, setSkills] = useState<string[] | null>(null)
  const [available, setAvailable] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = useCallback(() => {
    listUserSkills()
      .then((s) => { setSkills(s); setAvailable(true) })
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) setAvailable(false)
        else setError(msg(e))
      })
  }, [])
  useEffect(reload, [reload])

  async function upload(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    const f = fileRef.current?.files?.[0]
    if (!f) return
    const name = f.name.replace(/\.zip$/i, "")
    try {
      await uploadUserSkill(name, f)
      if (fileRef.current) fileRef.current.value = ""
      reload()
    } catch (err) { setError(msg(err)) }
  }
  async function remove(n: string) {
    try { await deleteUserSkill(n); reload() } catch (err) { setError(msg(err)) }
  }

  if (!available) return null
  const list = skills ?? []
  return (
    <Section title="Your skills">
      <p className="mb-3 text-xs text-muted-foreground">
        Upload a skill as a <code className="rounded bg-muted px-1">.zip</code> (a folder with a
        <code className="rounded bg-muted px-1">SKILL.md</code>). Used alongside your org's skills
        (yours win on a name clash). Any secrets it declares appear in <strong>Secrets</strong> above.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {list.length > 0 && (
        <ul className="mb-3 divide-y divide-border">
          {list.map((n) => (
            <li key={n} className="flex items-center gap-2 py-2">
              <span className="flex-1 truncate text-sm font-mono">{n}</span>
              <Button variant="ghost" size="sm" onClick={() => remove(n)} title="Remove">
                <Trash2 className="h-3.5 w-3.5 text-destructive" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={upload} className="flex items-center gap-2 border-t border-border/60 pt-3">
        <input ref={fileRef} type="file" accept=".zip" className="flex-1 text-sm" />
        <Button type="submit" size="sm"><Plus className="h-3.5 w-3.5" /> Upload</Button>
      </form>
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
