import { useCallback, useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowLeft, Copy, Check, Trash2, KeyRound, Plus, ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiError } from "@/api/client"
import { cn } from "@/lib/utils"
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

      <div className="flex-1 divide-y divide-border/60 px-4">
        {error && (
          <p className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>
        )}
        <ProfileSection me={me} onSaved={setMe} />
        <PasswordSection />
        <UserMcpSection />
        <UserSkillsSection />
        <CustomVariablesSection />
        <ApiKeysSection />
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="py-5">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {children}
    </section>
  )
}

/** Profile + password, with its own `me` fetch — for embedding in the Settings
 * modal's Account tab (API keys + theme live in their own tabs there). */
export function AccountInfoSections() {
  const [me, setMe] = useState<Me | null>(null)
  useEffect(() => { getMe().then(setMe).catch(() => {}) }, [])
  return (
    <>
      <ProfileSection me={me} onSaved={setMe} />
      <PasswordSection />
    </>
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
  // Secret inputs are write-only: the field starts empty and masks input.
  // Non-secret inputs show their current value as editable plain text.
  const original = item.value ?? ""
  const [value, setValue] = useState(item.secret ? "" : original)
  const [saved, setSaved] = useState(false)

  async function save(e: React.FormEvent) {
    e.preventDefault()
    if (!value) return
    try {
      await setSecret(item.key, value)
      if (item.secret) setValue("")  // keep non-secret value visible after save
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

  // Non-secret: only enable Save once the value actually changed.
  const canSave = item.secret ? !!value : !!value && value !== original

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
          type={item.secret ? "password" : "text"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={item.status === "unset" ? "enter value" : "update value"}
          className="flex-1"
          autoComplete="off"
        />
        <Button type="submit" size="sm" disabled={!canSave}>Save</Button>
      </form>
    </li>
  )
}

function VariableRows({ inputs, onChanged, onError }: { inputs: SecretInput[]; onChanged: () => void; onError: (m: string) => void }) {
  if (inputs.length === 0)
    return <p className="text-xs text-muted-foreground">No variables required.</p>
  return (
    <ul className="divide-y divide-border/60">
      {inputs.map((it) => <SecretRow key={it.key} item={it} onChanged={onChanged} onError={onError} />)}
    </ul>
  )
}

/** Collapsible card for one MCP server / skill, surfacing its variables inline.
 * Auto-expands when something needs setup. */
function ItemCard({ title, scopeBadge, missing, children }: {
  title: string; scopeBadge?: React.ReactNode; missing?: number; children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)  // collapsed by default; the header's
  // "needs setup" badge still flags items that want attention.
  return (
    <div className="rounded-md border border-border/60">
      <button type="button" onClick={() => setOpen((o) => !o)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent/50">
        <ChevronRight className={cn("h-3.5 w-3.5 text-muted-foreground transition-transform", open && "rotate-90")} />
        <span className="font-mono text-sm">{title}</span>
        {scopeBadge}
        {(missing ?? 0) > 0 && (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
            {missing} needs setup
          </span>
        )}
      </button>
      {open && <div className="space-y-2 border-t border-border/60 px-3 py-2">{children}</div>}
    </div>
  )
}

function inputsFor(view: SecretsView | null, kind: "mcp" | "skill", name: string): SecretInput[] {
  return view?.groups.find((g) => g.kind === kind && g.name === name)?.inputs ?? []
}

function missingOf(inputs: SecretInput[]): number {
  return inputs.filter((i) => i.status === "unset").length
}

/** Variables not owned by a specific MCP/skill — custom keys you add yourself.
 * Lives on the Account tab; variables required by a skill/MCP live under their
 * own item in the MCP / Skills tabs. */
export function CustomVariablesSection() {
  const [view, setView] = useState<SecretsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newKey, setNewKey] = useState("")
  const [newVal, setNewVal] = useState("")

  const reload = useCallback(() => {
    listSecrets().then(setView).catch((e) => setError(msg(e)))
  }, [])
  useEffect(reload, [reload])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    const k = newKey.trim()
    if (!k || !newVal) return
    try {
      await setSecret(k, newVal)
      setNewKey(""); setNewVal(""); reload()
    } catch (err) { setError(msg(err)) }
  }

  const other = view?.other ?? []
  return (
    <Section title="Custom variables">
      <p className="mb-3 text-xs text-muted-foreground">
        Extra values not tied to a specific skill or MCP server (e.g. a token the agent's plain
        <code className="mx-1 rounded bg-muted px-1">run_bash</code> uses). Variables a skill/MCP
        requires live under their own item in the MCP / Skills tabs.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {other.length > 0 && (
        <ul className="mb-3 divide-y divide-border/60">
          {other.map((it) => <SecretRow key={it.key} item={it} onChanged={reload} onError={setError} />)}
        </ul>
      )}
      <form onSubmit={add} className="flex items-center gap-2 border-t border-border/60 pt-3">
        <Input value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="CUSTOM_KEY" className="w-40 font-mono text-xs" />
        <Input type="password" value={newVal} onChange={(e) => setNewVal(e.target.value)} placeholder="value" className="flex-1" autoComplete="off" />
        <Button type="submit" size="sm" disabled={!newKey.trim() || !newVal}><Plus className="h-3.5 w-3.5" /> Add</Button>
      </form>
    </Section>
  )
}

export function ApiKeysSection() {
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

export function UserMcpSection() {
  const [servers, setServers] = useState<UserMcpServer[] | null>(null)
  const [view, setView] = useState<SecretsView | null>(null)
  const [available, setAvailable] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [transport, setTransport] = useState("http")
  const [url, setUrl] = useState("")
  const [credKey, setCredKey] = useState("")

  const reload = useCallback(() => {
    listUserMcpServers()
      .then((s) => { setServers(s); setAvailable(true) })
      .catch((e) => { if (e instanceof ApiError && e.status === 404) setAvailable(false); else setError(msg(e)) })
    listSecrets().then(setView).catch(() => {})
  }, [])
  useEffect(reload, [reload])

  async function add(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!name.trim() || !url.trim()) return
    try {
      await putUserMcpServer({ server_name: name.trim(), transport, url: url.trim(), credential_key: credKey.trim() || null })
      setName(""); setUrl(""); setCredKey(""); reload()
    } catch (err) { setError(msg(err)) }
  }
  async function remove(n: string) {
    try { await deleteUserMcpServer(n); reload() } catch (err) { setError(msg(err)) }
  }

  const servs = servers ?? []
  const byName = new Map(servs.map((s) => [s.server_name, s]))
  const groupNames = (view?.groups ?? []).filter((g) => g.kind === "mcp").map((g) => g.name)
  const names = Array.from(new Set([...servs.map((s) => s.server_name), ...groupNames]))
  // `available` gates only the add/manage UI; org/static servers' variables
  // still show (from the secrets groups) even when personal MCP isn't enabled.
  if (!available && names.length === 0) return null

  return (
    <Section title="MCP servers">
      <p className="mb-3 text-xs text-muted-foreground">
        MCP servers available to the agent — yours run alongside your org's (yours win on a name
        clash). Expand a server to set the variables it needs.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {available && (
        <form onSubmit={add} className="space-y-2">
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
            <Button type="submit" size="sm" disabled={!name.trim() || !url.trim()}><Plus className="h-3.5 w-3.5" /> Add server</Button>
          </div>
        </form>
      )}
      {names.length > 0 && (
        <div className={cn("space-y-2", available && "mt-3 border-t border-border/60 pt-3")}>
          {names.map((n) => {
            const s = byName.get(n)
            const inputs = inputsFor(view, "mcp", n)
            const personal = s && s.scope !== "tenant"
            return (
              <ItemCard key={n} title={n}
                scopeBadge={
                  <>
                    {s && <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">{s.transport}</span>}
                    {personal
                      ? <span className="rounded bg-emerald-500/15 px-1 py-0.5 text-[10px] text-emerald-600">Personal</span>
                      : <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">From org</span>}
                  </>
                }
                missing={missingOf(inputs)}>
                {s && (
                  <div className="flex items-center justify-between gap-2">
                    <p className="truncate text-xs text-muted-foreground">{s.url}{s.credential_key ? ` · token: ${s.credential_key}` : ""}</p>
                    {personal && (
                      <Button variant="ghost" size="sm" onClick={() => remove(s.server_name)} title="Remove server">
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    )}
                  </div>
                )}
                <VariableRows inputs={inputs} onChanged={reload} onError={setError} />
              </ItemCard>
            )
          })}
        </div>
      )}
    </Section>
  )
}

export function UserSkillsSection() {
  const [skills, setSkills] = useState<string[] | null>(null)
  const [view, setView] = useState<SecretsView | null>(null)
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
    listSecrets().then(setView).catch(() => {})
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

  const mine = new Set(skills ?? [])
  const groupNames = (view?.groups ?? []).filter((g) => g.kind === "skill").map((g) => g.name)
  const names = Array.from(new Set([...(skills ?? []), ...groupNames]))
  if (!available && names.length === 0) return null

  return (
    <Section title="Skills">
      <p className="mb-3 text-xs text-muted-foreground">
        Skills available to the agent. Upload a personal skill as a
        <code className="mx-1 rounded bg-muted px-1">.zip</code> (a folder with a
        <code className="mx-1 rounded bg-muted px-1">SKILL.md</code>); it runs alongside your org's.
        Expand a skill to set the variables it declares.
      </p>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}
      {available && (
        <form onSubmit={upload} className="flex items-center gap-2">
          <input ref={fileRef} type="file" accept=".zip"
                 className="flex-1 text-sm text-muted-foreground file:mr-3 file:rounded-md file:border-0 file:bg-muted file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-foreground hover:file:bg-accent" />
          <Button type="submit" size="sm"><Plus className="h-3.5 w-3.5" /> Upload</Button>
        </form>
      )}
      {names.length > 0 && (
        <div className={cn("space-y-2", available && "mt-3 border-t border-border/60 pt-3")}>
          {names.map((n) => {
            const isMine = mine.has(n)
            const inputs = inputsFor(view, "skill", n)
            return (
              <ItemCard key={n} title={n}
                scopeBadge={isMine
                  ? <span className="rounded bg-emerald-500/15 px-1 py-0.5 text-[10px] text-emerald-600">Personal</span>
                  : <span className="rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground">From org</span>}
                missing={missingOf(inputs)}>
                {isMine && (
                  <div className="flex justify-end">
                    <Button variant="ghost" size="sm" onClick={() => remove(n)} title="Remove skill">
                      <Trash2 className="h-3.5 w-3.5 text-destructive" />
                    </Button>
                  </div>
                )}
                <VariableRows inputs={inputs} onChanged={reload} onError={setError} />
              </ItemCard>
            )
          })}
        </div>
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
