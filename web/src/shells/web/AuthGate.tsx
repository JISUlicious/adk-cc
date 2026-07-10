import { useEffect, useState, type ReactNode } from "react"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/components/ui/card"
import { apiFetch, ApiError } from "@/shared/api/client"
import { getToken, setToken, getUser, onAuthCleared, isSignedOut, clearSignedOut } from "@/shared/api/auth"
import {
  fetchAuthConfig,
  login as pwLogin,
  signup as pwSignup,
  requestAccess,
  type AuthConfig,
} from "@/shared/api/identity"

// Auto-login identity used when the server is in no-auth dev mode
// (ADK_CC_ALLOW_NO_AUTH=1). The token value is irrelevant to such a server —
// any non-empty string is accepted — so we mint a placeholder and skip the
// login form entirely. The user id defaults to whatever was last used.
const DEV_TOKEN = "dev"

/**
 * Wraps the app. Renders a login form when auth is required and no valid
 * token is stored, or when any later API call returns 401. Children render
 * only once a token has been VERIFIED against the server.
 *
 * The login form adapts to the server's auth provider, learned from
 * `GET /auth/config`:
 *   - password provider (ADK_CC_AUTH_PASSWORD=1) → email+password form, with a
 *     sign-up toggle when self-registration is enabled (multi-tenant mode).
 *   - no identity provider (external JWT / dev Bearer) → token-paste form.
 *
 * Dev convenience: on first load with no token we probe `/list-apps` WITHOUT
 * auth; a 200 means ADK_CC_ALLOW_NO_AUTH and we auto-sign-in.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setLocalUser] = useState<string>(getUser())
  const [verifying, setVerifying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // null = not yet checked; true = verified good; false = needs login.
  const [verified, setVerified] = useState<boolean | null>(null)
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null)
  const [mode, setMode] = useState<"login" | "signup" | "request">("login")
  // Email of a just-submitted access request → renders the "awaiting approval"
  // confirmation instead of the login form.
  const [requested, setRequested] = useState<string | null>(null)
  // True when the server accepts unauthenticated calls (ADK_CC_ALLOW_NO_AUTH).
  // Used so the signed-out screen offers a one-click "Continue" instead of a
  // dead-end token-paste form (you have no token to type on a no-auth server).
  const [noAuthMode, setNoAuthMode] = useState(false)
  // True once we've determined the server's auth shape (password? no-auth?).
  // Until then we never render a login form — prevents the token-paste fallback
  // from flashing before we know an email/password provider is configured.
  const [probed, setProbed] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function loadConfig() {
      try {
        const cfg = await fetchAuthConfig()
        if (!cancelled) setAuthConfig(cfg)
      } catch {
        // No in-house identity provider (external JWT / Bearer deployment) —
        // leave authConfig null so the token-paste form renders.
      }
    }
    async function isAnonOk(): Promise<boolean> {
      // Does the server accept unauthenticated requests (no-auth dev mode)?
      try {
        await apiFetch<string[]>("/list-apps", { noAuth: true })
        return true
      } catch {
        return false
      }
    }
    async function bootstrap() {
      if (getToken()) {
        try {
          await apiFetch<string[]>("/list-apps")
          if (!cancelled) setVerified(true)
          return
        } catch {
          if (cancelled) return
          setToken("", "") // stale/expired → fall through to the login flow
        }
      }
      // No (valid) token. Learn the server's auth shape: password provider?
      // anonymous-OK (no-auth dev)?
      await loadConfig()
      const anon = await isAnonOk()
      if (cancelled) return
      setNoAuthMode(anon)
      setProbed(true)
      // No-auth dev mode AND not an explicit sign-out → auto-sign-in (frictionless
      // dev). After an explicit sign-out we deliberately DON'T, so it sticks; the
      // signed-out screen then offers a one-click Continue.
      if (anon && !isSignedOut()) {
        const u = getUser()
        setToken(DEV_TOKEN, u)
        setLocalUser(u)
        setVerified(true)
        return
      }
      setVerified(false) // show the login / continue screen
    }
    if (verified === null) bootstrap()
    return () => {
      cancelled = true
    }
  }, [verified])

  // A 401 from any later API call clears the token and bounces to login.
  useEffect(() => {
    return onAuthCleared(() => setVerified(false))
  }, [])

  if (verified === null) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Connecting…</p>
      </div>
    )
  }

  if (verified) {
    return <>{children}</>
  }

  // verified === false → a login screen is due, but don't render one until the
  // auth shape is known (else the token-paste fallback flashes for a frame).
  if (!probed) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Connecting…</p>
      </div>
    )
  }

  const passwordMode = !!authConfig?.password
  const canSignup = !!authConfig?.registration
  const canRequest = !!authConfig?.access_requests

  // --- no-auth dev: one-click re-entry after sign-out ---
  function handleContinue() {
    const u = getUser()
    setToken(DEV_TOKEN, u)
    setLocalUser(u)
    clearSignedOut()
    setVerified(true)
  }

  // --- email + password (in-house identity provider) ---
  async function handlePassword(e: React.FormEvent) {
    e.preventDefault()
    const data = new FormData(e.currentTarget as HTMLFormElement)
    const email = String(data.get("email") || "").trim()
    const password = String(data.get("password") || "")
    const name = String(data.get("name") || "").trim()
    const org = String(data.get("org") || "").trim()
    const note = String(data.get("note") || "").trim()
    if (!email || !password) {
      setError("Email and password are required")
      return
    }
    setVerifying(true)
    setError(null)
    try {
      if (mode === "request") {
        await requestAccess({ email, password, name, note })
        setRequested(email)
        return
      }
      const res =
        mode === "signup"
          ? await pwSignup({ email, password, name, org })
          : await pwLogin(email, password)
      setToken(res.access_token, res.user.id, res.refresh_token)
      setLocalUser(res.user.id)
      clearSignedOut()
      setVerified(true)
    } catch (err) {
      setError(authErrMsg(err))
    } finally {
      setVerifying(false)
    }
  }

  // --- token paste (external JWT / dev Bearer) ---
  async function handleToken(e: React.FormEvent) {
    e.preventDefault()
    const data = new FormData(e.currentTarget as HTMLFormElement)
    const t = String(data.get("token") || "").trim()
    const u = String(data.get("user") || "").trim() || "alice"
    if (!t) {
      setError("Token is required")
      return
    }
    setVerifying(true)
    setError(null)
    try {
      setToken(t, u)
      await apiFetch<string[]>("/list-apps")
      setLocalUser(u)
      clearSignedOut()
      setVerified(true)
    } catch (err) {
      setToken("", "")
      if (err instanceof ApiError && err.status === 401) {
        setError("Token rejected by the server (401). Check the value.")
      } else if (err instanceof ApiError) {
        setError(`Server returned ${err.status}. Check API_URL.`)
      } else {
        setError("Could not reach the server. Is adk api_server running?")
      }
    } finally {
      setVerifying(false)
    }
  }

  // Access request just filed → confirmation instead of the login form.
  if (requested) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-6">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle>Request submitted</CardTitle>
            <CardDescription>
              Your access request for <span className="font-medium">{requested}</span> was
              sent. An admin needs to approve it before you can sign in.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              className="w-full"
              variant="outline"
              onClick={() => {
                setRequested(null)
                setMode("login")
                setError(null)
              }}
            >
              Back to sign in
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        {passwordMode ? (
          <>
            <CardHeader>
              <CardTitle>
                {mode === "signup"
                  ? "Create your adk-cc account"
                  : mode === "request"
                    ? "Request access to adk-cc"
                    : "Sign in to adk-cc"}
              </CardTitle>
              <CardDescription>
                {mode === "signup"
                  ? "Sign up with your email and a password."
                  : mode === "request"
                    ? "Choose your credentials — an admin approves your request before you can sign in."
                    : "Sign in with your email and password."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePassword} className="space-y-4">
                <Field label="Email" id="email">
                  <Input
                    id="email"
                    name="email"
                    type="email"
                    placeholder="you@example.com"
                    autoComplete="email"
                    autoFocus
                  />
                </Field>
                <Field label="Password" id="password">
                  <Input
                    id="password"
                    name="password"
                    type="password"
                    placeholder={mode === "login" ? "••••••••" : "at least 8 characters"}
                    autoComplete={mode === "login" ? "current-password" : "new-password"}
                  />
                </Field>
                {mode === "signup" && (
                  <>
                    <Field label="Name (optional)" id="name">
                      <Input id="name" name="name" placeholder="Your name" autoComplete="name" />
                    </Field>
                    <Field
                      label="Organization (optional)"
                      id="org"
                      hint="Creates your workspace. Defaults to a private one."
                    >
                      <Input id="org" name="org" placeholder="Acme Inc" autoComplete="organization" />
                    </Field>
                  </>
                )}
                {mode === "request" && (
                  <>
                    <Field label="Name (optional)" id="name">
                      <Input id="name" name="name" placeholder="Your name" autoComplete="name" />
                    </Field>
                    <Field
                      label="Note to the admin (optional)"
                      id="note"
                      hint="Shown with your request — who you are, why you need access."
                    >
                      <Input id="note" name="note" placeholder="I'm Jane from the QA team" />
                    </Field>
                  </>
                )}
                {error && <p className="text-sm text-destructive">{error}</p>}
                <Button type="submit" disabled={verifying} className="w-full">
                  {verifying
                    ? mode === "signup"
                      ? "Creating…"
                      : mode === "request"
                        ? "Requesting…"
                        : "Signing in…"
                    : mode === "signup"
                      ? "Create account"
                      : mode === "request"
                        ? "Request access"
                        : "Sign in"}
                </Button>
                {canSignup && (
                  <p className="text-center text-xs text-muted-foreground">
                    {mode === "signup" ? "Already have an account?" : "No account yet?"}{" "}
                    <button
                      type="button"
                      className="text-primary underline underline-offset-2 hover:opacity-80"
                      onClick={() => {
                        setError(null)
                        setMode(mode === "signup" ? "login" : "signup")
                      }}
                    >
                      {mode === "signup" ? "Sign in" : "Create one"}
                    </button>
                  </p>
                )}
                {canRequest && (
                  <p className="text-center text-xs text-muted-foreground">
                    {mode === "request" ? "Already have an account?" : "No account yet?"}{" "}
                    <button
                      type="button"
                      className="text-primary underline underline-offset-2 hover:opacity-80"
                      onClick={() => {
                        setError(null)
                        setMode(mode === "request" ? "login" : "request")
                      }}
                    >
                      {mode === "request" ? "Sign in" : "Request access"}
                    </button>
                  </p>
                )}
              </form>
            </CardContent>
          </>
        ) : noAuthMode ? (
          <>
            <CardHeader>
              <CardTitle>Signed out</CardTitle>
              <CardDescription>
                This is a no-auth dev server (ADK_CC_ALLOW_NO_AUTH) — there's no
                account to sign in with. Continue back into the app.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button onClick={handleContinue} className="w-full">
                Continue
              </Button>
            </CardContent>
          </>
        ) : (
          <>
            <CardHeader>
              <CardTitle>Sign in to adk-cc</CardTitle>
              <CardDescription>
                Paste a Bearer token. JWT (production) or static dev token both
                work — same Authorization header on the wire.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleToken} className="space-y-4">
                <Field
                  label="User id"
                  id="user"
                  hint="Used as the URL segment under /apps/<app>/users/<user>/sessions."
                >
                  <Input
                    id="user"
                    name="user"
                    defaultValue={user}
                    placeholder="alice"
                    autoComplete="username"
                  />
                </Field>
                <Field label="Bearer token" id="token">
                  <Input
                    id="token"
                    name="token"
                    type="password"
                    placeholder="eyJhbGciOiJSUzI1NiI…"
                    autoComplete="current-password"
                  />
                </Field>
                {error && <p className="text-sm text-destructive">{error}</p>}
                <Button type="submit" disabled={verifying} className="w-full">
                  {verifying ? "Verifying…" : "Sign in"}
                </Button>
                <p className="text-xs text-muted-foreground">
                  Dev: set <code className="font-mono">ADK_CC_ALLOW_NO_AUTH=1</code> on the
                  server and any non-empty token works.
                </p>
              </form>
            </CardContent>
          </>
        )}
      </Card>
    </div>
  )
}

/** Labeled form field with an optional hint line. */
function Field({
  label,
  id,
  hint,
  children,
}: {
  label: string
  id: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-2">
      <label htmlFor={id} className="text-sm font-medium text-foreground">
        {label}
      </label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

function authErrMsg(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    if (err.status === 401) return "Invalid email or password."
    if (err.status === 403) return "Sign-up is disabled. Contact your admin."
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server. Is the adk-cc server running?"
}
