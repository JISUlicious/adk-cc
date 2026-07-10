import { useEffect, useState } from "react"
import { useParams } from "react-router-dom"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/components/ui/card"
import { ApiError } from "@/shared/api/client"
import { setToken } from "@/shared/api/auth"
import { getReset, completeReset } from "@/shared/api/identity"

/**
 * Public password-reset page (route /reset-password/:token, OUTSIDE the
 * AuthGate) — the one-time link an admin hands out. Looks the token up, lets
 * the holder set a new password, then signs them straight in (possession of
 * the link is the proof). Mirrors AcceptInvitePage.
 */
export function ResetPasswordPage() {
  const { token = "" } = useParams()
  const [email, setEmail] = useState("")
  const [state, setState] = useState<"loading" | "ready" | "invalid">("loading")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getReset(token)
      .then((info) => {
        if (cancelled) return
        setEmail(info.email)
        setState("ready")
      })
      .catch(() => {
        if (!cancelled) setState("invalid")
      })
    return () => {
      cancelled = true
    }
  }, [token])

  async function handleReset(e: React.FormEvent) {
    e.preventDefault()
    const data = new FormData(e.currentTarget as HTMLFormElement)
    const password = String(data.get("password") || "")
    const confirm = String(data.get("confirm") || "")
    if (!password) {
      setError("Password is required")
      return
    }
    if (password !== confirm) {
      setError("Passwords don't match")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const res = await completeReset(token, password)
      setToken(res.access_token, res.user.id, res.refresh_token)
      // Hard navigation so the app boots fresh with the new token.
      window.location.assign("/")
    } catch (err) {
      setError(resetErr(err))
      setSubmitting(false)
    }
  }

  if (state === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Loading reset link…</p>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        {state === "invalid" ? (
          <>
            <CardHeader>
              <CardTitle>Reset link not found</CardTitle>
              <CardDescription>
                This reset link is invalid, has already been used, or has expired.
                Ask your admin for a new one.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <a href="/">
                <Button variant="outline" className="w-full">Go to sign in</Button>
              </a>
            </CardContent>
          </>
        ) : (
          <>
            <CardHeader>
              <CardTitle>Reset your password</CardTitle>
              <CardDescription>
                Set a new password for <strong>{email}</strong>. Every existing
                session will be signed out.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleReset} className="space-y-4">
                <div className="space-y-2">
                  <label htmlFor="password" className="text-sm font-medium text-foreground">
                    New password
                  </label>
                  <Input
                    id="password"
                    name="password"
                    type="password"
                    placeholder="at least 8 characters"
                    autoComplete="new-password"
                    autoFocus
                  />
                </div>
                <div className="space-y-2">
                  <label htmlFor="confirm" className="text-sm font-medium text-foreground">
                    Confirm password
                  </label>
                  <Input
                    id="confirm"
                    name="confirm"
                    type="password"
                    placeholder="repeat it"
                    autoComplete="new-password"
                  />
                </div>
                {error && <p className="text-sm text-destructive">{error}</p>}
                <Button type="submit" disabled={submitting} className="w-full">
                  {submitting ? "Resetting…" : "Reset password"}
                </Button>
              </form>
            </CardContent>
          </>
        )}
      </Card>
    </div>
  )
}

function resetErr(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
