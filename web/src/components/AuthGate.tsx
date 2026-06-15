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
import { getToken, setToken, getUser, onAuthCleared } from "@/api/auth"

// Auto-login identity used when the server is in no-auth dev mode
// (ADK_CC_ALLOW_NO_AUTH=1). The token value is irrelevant to such a server —
// any non-empty string is accepted — so we mint a placeholder and skip the
// login form entirely. The user id defaults to whatever was last used.
const DEV_TOKEN = "dev"

/**
 * Wraps the app. Renders a login form when auth is required and no valid
 * token is stored, or when any later API call returns 401 (token revoked/
 * expired mid-session). Children render only once a token has been VERIFIED
 * against the server — presence alone is not trusted, so a stale/invalid
 * token can't render a broken, 401-ing page.
 *
 * Dev convenience: on first load with no token, we probe `/list-apps`
 * WITHOUT auth. If the server accepts it (ADK_CC_ALLOW_NO_AUTH=1), we
 * auto-sign-in with a placeholder token and skip the login form. If it 401s
 * (real auth configured), the login form shows exactly as before. This is
 * behavior-detected, not build-flag'd — the bundle is built in prod mode
 * even for local FastAPI-served dev, so import.meta.env.DEV is unreliable.
 *
 * Production OIDC redirect: future. v1 form-pastes a JWT (works with
 * either JwtAuthExtractor or BearerTokenExtractor — same Authorization
 * header on the wire).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setLocalUser] = useState<string>(getUser())
  const [verifying, setVerifying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // null = not yet checked (verifying a stored token OR probing no-auth);
  // true = verified good; false = needs login.
  const [verified, setVerified] = useState<boolean | null>(null)

  // Bootstrap on mount. Two paths, neither renders children until verified:
  //   - stored token  → verify it against /list-apps; clear + show form on fail
  //   - no token      → probe /list-apps WITHOUT auth; 200 ⇒ no-auth dev mode,
  //                     auto-sign-in and skip the form; failure ⇒ show form
  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      if (getToken()) {
        try {
          await apiFetch<string[]>("/list-apps")
          if (!cancelled) setVerified(true)
        } catch {
          if (cancelled) return
          setToken("", "")
          setVerified(false)
        }
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
        if (!cancelled) setVerified(false) // real auth required → login form
      }
    }
    if (verified === null) bootstrap()
    return () => {
      cancelled = true
    }
  }, [verified])

  // A 401 from ANY later API call clears the token (apiFetch) and fires
  // this callback — bounce back to the login form instead of leaving a
  // broken page showing 401 errors.
  useEffect(() => {
    return onAuthCleared(() => {
      setVerified(false)
    })
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

  async function handleLogin(e: React.FormEvent) {
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
    // Pre-flight against /list-apps, which every ADK FastAPI exposes
    // and which the auth middleware gates. If this returns 200 we
    // know the token works against the wire-level auth extractor.
    try {
      setToken(t, u)
      await apiFetch<string[]>("/list-apps")
      setLocalUser(u)
      setVerified(true)
    } catch (err) {
      setToken("", "") // clear cleanly
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
        <CardHeader>
          <CardTitle>Sign in to adk-cc</CardTitle>
          <CardDescription>
            Paste a Bearer token. JWT (production) or static dev token
            both work — same Authorization header on the wire.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-2">
              <label
                htmlFor="user"
                className="text-sm font-medium text-foreground"
              >
                User id
              </label>
              <Input
                id="user"
                name="user"
                defaultValue={user}
                placeholder="alice"
                autoComplete="username"
              />
              <p className="text-xs text-muted-foreground">
                Used as the URL segment under{" "}
                <code className="font-mono">/apps/&lt;app&gt;/users/&lt;user&gt;/sessions</code>
                . Defaults to <code className="font-mono">alice</code>.
              </p>
            </div>
            <div className="space-y-2">
              <label
                htmlFor="token"
                className="text-sm font-medium text-foreground"
              >
                Bearer token
              </label>
              <Input
                id="token"
                name="token"
                type="password"
                placeholder="eyJhbGciOiJIUzI1NiI…"
                autoComplete="current-password"
              />
            </div>
            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}
            <Button type="submit" disabled={verifying} className="w-full">
              {verifying ? "Verifying…" : "Sign in"}
            </Button>
            <p className="text-xs text-muted-foreground">
              Dev: set{" "}
              <code className="font-mono">ADK_CC_ALLOW_NO_AUTH=1</code>{" "}
              on the server and any non-empty token works.
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
