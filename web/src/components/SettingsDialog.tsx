import { useEffect } from "react"
import { X, Moon, Sun, Monitor, LogOut, Shield, Network, Users, User } from "lucide-react"
import { Button } from "./ui/button"
import { useTheme, type ThemeMode } from "@/lib/theme"
import { clearToken, getToken, getUser, maybeAdmin, markSignedOut } from "@/api/auth"
import { cn } from "@/lib/utils"

/**
 * Lightweight modal — no Radix / shadcn Dialog dependency. Backdrop
 * + centered card, Escape closes. Holds settings the user can change
 * without touching the server: theme + sign-out.
 *
 * Read-only rows surface state the user might want to verify (user
 * id, masked token). Phase 5+ could grow this into a permission-mode
 * selector that posts an enter_plan_mode / settings-update call, but
 * for now it's purely client-side.
 */

export function SettingsDialog({
  open,
  onClose,
  secretsMissing = 0,
}: {
  open: boolean
  onClose: () => void
  secretsMissing?: number
}) {
  const [mode, setMode] = useTheme()

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null

  const token = getToken() ?? ""
  const masked =
    token.length > 12
      ? token.slice(0, 6) + "…" + token.slice(-4)
      : token.length > 0
        ? "(short token)"
        : "(none)"

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg border border-border bg-background shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="font-medium">Settings</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="p-4 space-y-5">
          <section className="space-y-2">
            <label className="text-xs font-medium text-muted-foreground">
              Theme
            </label>
            <div className="flex gap-2">
              <ThemeOption
                value="light"
                active={mode}
                onPick={setMode}
                label="Light"
                Icon={Sun}
              />
              <ThemeOption
                value="dark"
                active={mode}
                onPick={setMode}
                label="Dark"
                Icon={Moon}
              />
              <ThemeOption
                value="system"
                active={mode}
                onPick={setMode}
                label="System"
                Icon={Monitor}
              />
            </div>
          </section>

          <section className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Identity
            </label>
            <ReadOnlyRow label="User id" value={getUser()} />
            <ReadOnlyRow label="Bearer token" value={masked} mono />
          </section>

          <section className="pt-2 border-t space-y-2">
            <a href="/account">
              <Button variant="outline" size="sm" className="relative w-full">
                <User className="h-3.5 w-3.5" />
                Account
                {secretsMissing > 0 && (
                  <span className="absolute right-2 top-1/2 flex h-4 min-w-4 -translate-y-1/2 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-medium text-white">
                    {secretsMissing}
                  </span>
                )}
              </Button>
            </a>
            <a href="/knowledge">
              <Button variant="outline" size="sm" className="w-full">
                <Network className="h-3.5 w-3.5" />
                Knowledge graph
              </Button>
            </a>
          </section>

          {maybeAdmin() && (
            <section className="pt-2 border-t space-y-2">
              <a href="/org">
                <Button variant="outline" size="sm" className="w-full">
                  <Users className="h-3.5 w-3.5" />
                  Team
                </Button>
              </a>
              <a href="/admin">
                <Button variant="outline" size="sm" className="w-full">
                  <Shield className="h-3.5 w-3.5" />
                  Admin panel
                </Button>
              </a>
            </section>
          )}

          <section className="pt-2 border-t">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                markSignedOut()
                clearToken()
                // Go to the root, not reload-in-place — otherwise signing out
                // from /admin or /org re-prompts there and lands you back on
                // that page after sign-in instead of the chat home.
                location.assign("/")
              }}
              className="w-full"
            >
              <LogOut className="h-3.5 w-3.5" />
              Sign out
            </Button>
          </section>
        </div>
      </div>
    </div>
  )
}

function ThemeOption({
  value,
  active,
  onPick,
  label,
  Icon,
}: {
  value: ThemeMode
  active: ThemeMode
  onPick: (m: ThemeMode) => void
  label: string
  Icon: typeof Sun
}) {
  return (
    <button
      type="button"
      onClick={() => onPick(value)}
      className={cn(
        "flex-1 flex flex-col items-center gap-1 rounded-md border px-2 py-3 text-xs transition-colors",
        value === active
          ? "border-primary bg-brand-tint"
          : "border-input hover:bg-accent",
      )}
    >
      <Icon className="h-4 w-4" />
      {label}
    </button>
  )
}

function ReadOnlyRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-baseline gap-3">
      <span className="text-xs text-muted-foreground w-24">{label}</span>
      <span className={cn("text-sm", mono && "font-mono text-xs")}>
        {value}
      </span>
    </div>
  )
}
