import { useState, type ReactNode } from "react"
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
import { getToken, setToken, getUser } from "@/api/auth"

/**
 * Wraps the app. Renders a login form when no token is stored OR the
 * stored token fails its first probe; renders children once auth works.
 *
 * Production OIDC redirect: future. v1 form-pastes a JWT (works with
 * either JwtAuthExtractor or BearerTokenExtractor — same Authorization
 * header on the wire).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [token, setLocalToken] = useState<string | null>(getToken())
  const [user, setLocalUser] = useState<string>(getUser())
  const [verifying, setVerifying] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (token) {
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
      setLocalToken(t)
      setLocalUser(u)
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
