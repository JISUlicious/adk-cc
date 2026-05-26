import { Button } from "@/components/ui/button"
import { clearToken, getUser } from "@/api/auth"

/**
 * Phase 0 placeholder. Phase 1 fills this with:
 *   - Session list (left rail)
 *   - Thread (center) — message bubbles + tool-call cards, SSE-streamed
 *   - Input (bottom) — multi-line composer
 *
 * The auth gate has run by the time we get here, so apiFetch / streamRun
 * will carry the token.
 */
export function ChatPage() {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <div className="flex items-center gap-3">
          <span className="text-lg font-semibold tracking-tight">
            adk-cc
          </span>
          <span className="text-xs text-muted-foreground">chat (Phase 0 stub)</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-muted-foreground">
            Signed in as <span className="font-mono">{getUser()}</span>
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              clearToken()
              location.reload()
            }}
          >
            Sign out
          </Button>
        </div>
      </header>
      <main className="flex flex-1 items-center justify-center p-12">
        <div className="max-w-md space-y-3 text-center">
          <p className="text-sm text-muted-foreground">
            Phase 0 skeleton is alive. Next phase wires session list,
            message thread, and SSE streaming.
          </p>
          <p className="text-xs text-muted-foreground">
            Build:{" "}
            <code className="font-mono">npm --prefix web run build</code>
            {"  ·  "}Dev:{" "}
            <code className="font-mono">npm --prefix web run dev</code>
          </p>
        </div>
      </main>
    </div>
  )
}
