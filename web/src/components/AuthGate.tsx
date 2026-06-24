import { useEffect, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { apiFetch, ApiError } from "@/api/client"
import { getToken, setToken, getUser, onAuthCleared, isSignedOut, clearSignedOut } from "@/api/auth"
import {
  fetchAuthConfig,
  login as pwLogin,
  signup as pwSignup,
  type AuthConfig,
} from "@/api/identity"

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
  const [mode, setMode] = useState<"login" | "signup">("login")

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
    async function bootstrap() {
      if (getToken()) {
        try {
          await apiFetch<string[]>("/list-apps")
          if (!cancelled) setVerified(true)
        } catch {
          if (cancelled) return
          setToken("", "")
          await loadConfig()
          if (!cancelled) setVerified(false)
        }
        return
      }
      // Explicit sign-out: do NOT silently auto-login via no-auth dev mode —
      // show the login form and stay there until the user signs in again.
      if (isSignedOut()) {
        await loadConfig()
        if (!cancelled) setVerified(false)
        return
      }
      // No token — does the server accept unauthenticated requests?
      try {
        await apiFetch<string[]>("/list-apps", { noAuth: true })
        if (cancelled) return
        const u = getUser()
        setToken(DEV_TOKEN, u)
        setLocalUser(u)
        setVerified(true)
      } catch {
        if (cancelled) return
        await loadConfig()
        if (!cancelled) setVerified(false) // real auth required → login form
      }
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

  const passwordMode = !!authConfig?.password
  const canSignup = !!authConfig?.registration

  // --- email + password (in-house identity provider) ---
  async function handlePassword(e: React.FormEvent) {
    e.preventDefault()
    const data = new FormData(e.currentTarget as HTMLFormElement)
    const email = String(data.get("email") || "").trim()
    const password = String(data.get("password") || "")
    const name = String(data.get("name") || "").trim()
    const org = String(data.get("org") || "").trim()
    if (!email || !password) {
      setError("Email and password are required")
      return
    }
    setVerifying(true)
    setError(null)
    try {
      const res =
        mode === "signup"
          ? await pwSignup({ email, password, name, org })
          : await pwLogin(email, password)
      setToken(res.access_token, res.user.id)
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

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        {passwordMode ? (
          <>
            <CardHeader>
              <CardTitle>
                {mode === "signup" ? "Create your adk-cc account" : "Sign in to adk-cc"}
              </CardTitle>
              <CardDescription>
                {mode === "signup"
                  ? "Sign up with your email and a password."
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
                    placeholder={mode === "signup" ? "at least 8 characters" : "••••••••"}
                    autoComplete={mode === "signup" ? "new-password" : "current-password"}
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
                {error && <p className="text-sm text-destructive">{error}</p>}
                <Button type="submit" disabled={verifying} className="w-full">
                  {verifying
                    ? mode === "signup"
                      ? "Creating…"
                      : "Signing in…"
                    : mode === "signup"
                      ? "Create account"
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
              </form>
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
