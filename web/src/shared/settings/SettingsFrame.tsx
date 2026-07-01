import { useEffect, useState, type ReactNode } from "react"
import { X } from "lucide-react"
import type { LucideIcon } from "lucide-react"
import { cn } from "@/shared/lib/utils"
import { listSecrets } from "@/shared/api/account"

export interface SettingsTab {
  id: string
  label: string
  icon: LucideIcon
  /** Amber "needs setup" count shown on the nav item. */
  badge?: number
  render: () => ReactNode
}

/**
 * Shared chrome for the tabbed Settings modal: sidebar nav (with badges),
 * scrolling content with a soft top fade, a pinned close button, and an
 * optional sidebar footer (the web shell puts Sign-out there; desktop omits
 * it). Each shell supplies its own `tabs` — the bodies are shared components.
 */
export function SettingsFrame({
  open,
  onClose,
  tabs,
  initialTab,
  footer,
}: {
  open: boolean
  onClose: () => void
  tabs: SettingsTab[]
  initialTab?: string
  footer?: ReactNode
}) {
  const [tabId, setTabId] = useState(initialTab ?? tabs[0]?.id)

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null
  const active = tabs.find((t) => t.id === tabId) ?? tabs[0]

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        className="adk-settings flex h-[80vh] w-full max-w-3xl overflow-hidden rounded-lg border border-border bg-background shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        {/* sidebar */}
        <aside className="adk-settings-nav flex w-44 flex-col border-r border-border/60 bg-muted/30">
          <div className="flex items-center justify-between px-3 py-3">
            <h2 className="font-medium">Settings</h2>
          </div>
          <nav className="flex-1 space-y-0.5 px-2">
            {tabs.map((t) => (
              <button
                key={t.id}
                onClick={() => setTabId(t.id)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors",
                  t.id === active?.id
                    ? "bg-brand-tint font-medium text-foreground"
                    : "text-muted-foreground hover:bg-accent",
                )}
              >
                <t.icon className="h-3.5 w-3.5" />
                {t.label}
                {(t.badge ?? 0) > 0 && (
                  <span className="ml-auto flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-[10px] font-medium text-white">
                    {t.badge}
                  </span>
                )}
              </button>
            ))}
          </nav>
          {footer && <div className="border-t border-border/60 p-2">{footer}</div>}
        </aside>

        {/* content */}
        <div className="adk-settings-content relative flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex-1 divide-y divide-border/60 overflow-y-auto px-5 pt-9">
            {active?.render()}
          </div>
          {/* soft fade at the top — `right-2.5` keeps it off the scrollbar gutter. */}
          <div className="faded-header-edge pointer-events-none absolute left-0 right-2.5 top-0 h-10" />
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="absolute right-3 top-4 z-10 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  )
}

/** Shared "needs setup" badge counts (MCP / Skills) for the settings nav. */
export function useSecretBadges(open: boolean) {
  const [miss, setMiss] = useState<{ mcp: number; skill: number }>({ mcp: 0, skill: 0 })
  useEffect(() => {
    if (!open) return
    listSecrets()
      .then((v) => {
        const sum = (k: "mcp" | "skill") =>
          v.groups.filter((g) => g.kind === k).reduce((a, g) => a + g.missing, 0)
        setMiss({ mcp: sum("mcp"), skill: sum("skill") })
      })
      .catch(() => {})
  }, [open])
  return miss
}
