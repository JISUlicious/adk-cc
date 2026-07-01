import { type ReactNode } from "react"
import { X } from "lucide-react"
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
}

/**
 * Shared chrome for the right-side panel: a static column at lg+, a
 * slide-in drawer below lg. Mirrors TaskSidebar's responsive shell so the
 * two right-rail surfaces feel identical. Concrete panels supply the title,
 * an optional header-right slot (e.g. a refresh button), and the body.
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
          // lg+: static column.
          "lg:static lg:z-auto lg:w-[22rem] lg:translate-x-0 lg:transition-none",
        )}
      >
        <div className="adk-right-panel-header flex items-center gap-2 px-3 py-3 border-b border-border/60">
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
      </aside>
    </>
  )
}
