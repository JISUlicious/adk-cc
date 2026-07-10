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
import { getInvite, acceptInvite, type InvitePublic } from "@/shared/api/org"

/**
 * Public accept-invite page (route /invite/:token, OUTSIDE the AuthGate). Looks
 * the invite up by token, then lets the invitee set a password to join the org.
 * On success it stores the issued token and hard-navigates into the app.
 */
export function AcceptInvitePage() {
  const { token = "" } = useParams()
  const [invite, setInvite] = useState<InvitePublic | null>(null)
  const [state, setState] = useState<"loading" | "ready" | "invalid">("loading")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getInvite(token)
      .then((inv) => {
        if (cancelled) return
        setInvite(inv)
        setState("ready")
      })
      .catch(() => {
        if (!cancelled) setState("invalid")
      })
    return () => {
      cancelled = true
    }
  }, [token])

  async function handleAccept(e: React.FormEvent) {
    e.preventDefault()
    const data = new FormData(e.currentTarget as HTMLFormElement)
    const password = String(data.get("password") || "")
    const name = String(data.get("name") || "").trim()
    if (!password) {
      setError("Password is required")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const res = await acceptInvite(token, password, name)
      setToken(res.access_token, res.user.id, res.refresh_token)
      // Hard navigation so the app boots fresh with the new token.
      window.location.assign("/")
    } catch (err) {
      setError(acceptErr(err))
      setSubmitting(false)
    }
  }

  if (state === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background">
        <p className="text-sm text-muted-foreground">Loading invite…</p>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        {state === "invalid" ? (
          <>
            <CardHeader>
              <CardTitle>Invite not found</CardTitle>
              <CardDescription>
                This invitation is invalid, has already been used, or has expired.
                Ask your admin to send a new one.
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
              <CardTitle>Join {invite?.org}</CardTitle>
              <CardDescription>
                You were invited as <strong>{invite?.email}</strong> ({invite?.role}). Set a
                password to create your account.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleAccept} className="space-y-4">
                <div className="space-y-2">
                  <label htmlFor="name" className="text-sm font-medium text-foreground">
                    Name (optional)
                  </label>
                  <Input id="name" name="name" placeholder="Your name" autoComplete="name" />
                </div>
                <div className="space-y-2">
                  <label htmlFor="password" className="text-sm font-medium text-foreground">
                    Password
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
                {error && <p className="text-sm text-destructive">{error}</p>}
                <Button type="submit" disabled={submitting} className="w-full">
                  {submitting ? "Joining…" : "Join"}
                </Button>
              </form>
            </CardContent>
          </>
        )}
      </Card>
    </div>
  )
}

function acceptErr(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | undefined)?.detail
    if (typeof detail === "string" && detail) return detail
    return `Server returned ${err.status}.`
  }
  return "Could not reach the server."
}
