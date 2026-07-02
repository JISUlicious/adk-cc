import { useEffect, useState, type ReactNode } from "react"
import { PanelRightClose, PanelRightOpen, X } from "lucide-react"
import { cn } from "@/shared/lib/utils"

/**
 * Props every right-side panel receives from ChatPage. The concrete panel
 * (artifacts on web, file tree on desktop) is injected per-shell via
 * ChatPage's `RightPanel` prop; `open`/`onClose` drive the mobile drawer.
 */
export type RightPanelProps = {
  appName: string
  userId: string
  sessionId: string
  open: boolean
  onClose: () => void
  /** Bumped by ChatPage after every turn — panels reload to pick up
   * artifacts/files the agent just produced, without a manual refresh. */
  refreshKey?: number
}

const COLLAPSE_KEY = "adk_cc.rightPanel.collapsed"

/**
 * Shared chrome for the right-side panel: a static column at lg+, a slide-in
 * drawer below lg. Collapsible on desktop — a header button shrinks it to a
 * thin rail with an expand button (state persisted in localStorage), so it can
 * be tucked away without losing the affordance to bring it back. On mobile it
 * stays a drawer (collapse doesn't apply; `open`/`onClose` govern). Concrete
 * panels supply the title, an optional header-right slot, and the body.
 */
export function RightPanelShell({
  title,
  open,
  onClose,
  headerRight,
  children,
}: {
  title: string
  open: boolean
  onClose: () => void
  headerRight?: ReactNode
  children: ReactNode
}) {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1"
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0")
    } catch {
      /* private mode / disabled storage — collapse just won't persist */
    }
  }, [collapsed])

  return (
    <>
      {/* Mobile backdrop — tap to dismiss. */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-foreground/30 lg:hidden"
          aria-hidden
          onClick={onClose}
        />
      )}
      <aside
        className={cn(
          "adk-right-panel flex flex-col border-l border-border/60",
          "bg-muted shadow-xl lg:bg-muted/40 lg:shadow-none",
          // Mobile: fixed drawer sliding in from the right.
          "fixed inset-y-0 right-0 z-40 w-80 max-w-[85vw] transform transition-transform duration-200 ease-out",
          open ? "translate-x-0" : "translate-x-full",
          // lg+: static column; width follows the collapse toggle.
          "lg:static lg:z-auto lg:translate-x-0 lg:transition-none",
          collapsed ? "lg:w-10" : "lg:w-[22rem]",
        )}
      >
        {/* Collapsed rail (desktop only): just an expand button. */}
        {collapsed && (
          <button
            type="button"
            onClick={() => setCollapsed(false)}
            className="adk-right-panel-expand hidden lg:flex items-center justify-center py-3 text-muted-foreground hover:bg-accent"
            title={`Show ${title}`}
            aria-label={`Show ${title}`}
          >
            <PanelRightOpen className="h-4 w-4" />
          </button>
        )}
        {/* Header + body — hidden on desktop when collapsed; always shown on
            mobile (the drawer has no collapsed state). */}
        <div className={cn("flex min-h-0 flex-1 flex-col", collapsed && "lg:hidden")}>
          <div className="adk-right-panel-header flex items-center gap-2 px-3 py-3 border-b border-border/60">
            {/* Desktop collapse button. */}
            <button
              type="button"
              onClick={() => setCollapsed(true)}
              className="hidden lg:inline-flex rounded-md p-1 text-muted-foreground hover:bg-accent"
              title={`Hide ${title}`}
              aria-label={`Hide ${title}`}
            >
              <PanelRightClose className="h-4 w-4" />
            </button>
            <span className="text-xs font-medium">{title}</span>
            <div className="ml-auto flex items-center gap-1">
              {headerRight}
              <button
                type="button"
                onClick={onClose}
                className="lg:hidden rounded-md p-1 text-muted-foreground hover:bg-accent"
                title="Close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          <div className="adk-right-panel-body min-h-0 flex-1 overflow-y-auto">
            {children}
          </div>
        </div>
      </aside>
    </>
  )
}
