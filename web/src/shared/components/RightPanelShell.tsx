import { useEffect, useState, type CSSProperties, type ReactNode } from "react"
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
  /** Called after the panel restores a checkpoint, so ChatPage can reload the
   * thread (a rewind rolls back the conversation too, not just files). */
  onRestored?: () => void
}

const COLLAPSE_KEY = "adk_cc.rightPanel.collapsed"
const WIDTH_KEY = "adk_cc.rightPanel.width"
const MIN_W = 260
const MAX_W = 760
const DEFAULT_W = 352 // 22rem

/**
 * Shared chrome for the right-side panel: a static column at lg+, a slide-in
 * drawer below lg.
 *
 *  - Collapsible: a header button shrinks it to a thin rail with an expand
 *    button (persisted), so it can be tucked away without losing the affordance.
 *  - Resizable: a drag handle on the left edge sets the column width at lg+
 *    (persisted, clamped). Mobile stays a fixed-width drawer.
 *
 * Concrete panels supply the title, an optional header-right slot, and the body.
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
  const [width, setWidth] = useState<number>(() => {
    try {
      const v = parseInt(localStorage.getItem(WIDTH_KEY) || "", 10)
      return Number.isFinite(v) && v >= MIN_W && v <= MAX_W ? v : DEFAULT_W
    } catch {
      return DEFAULT_W
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0")
    } catch {
      /* private mode — collapse just won't persist */
    }
  }, [collapsed])
  useEffect(() => {
    try {
      localStorage.setItem(WIDTH_KEY, String(width))
    } catch {
      /* private mode — width just won't persist */
    }
  }, [width])

  // Drag the left edge to resize (lg+ only). Dragging left widens the panel.
  function onResizeStart(e: React.MouseEvent) {
    e.preventDefault()
    const startX = e.clientX
    const startW = width
    function onMove(ev: MouseEvent) {
      setWidth(Math.min(MAX_W, Math.max(MIN_W, startW + (startX - ev.clientX))))
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove)
      document.removeEventListener("mouseup", onUp)
      document.body.style.userSelect = ""
      document.body.style.cursor = ""
    }
    document.body.style.userSelect = "none"
    document.body.style.cursor = "col-resize"
    document.addEventListener("mousemove", onMove)
    document.addEventListener("mouseup", onUp)
  }

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
        style={{ "--rp-w": `${width}px` } as CSSProperties}
        className={cn(
          "adk-right-panel flex flex-col border-l border-border/60",
          "bg-muted shadow-xl lg:bg-muted/40 lg:shadow-none",
          // Mobile: fixed drawer sliding in from the right.
          "fixed inset-y-0 right-0 z-40 w-80 max-w-[85vw] transform transition-transform duration-200 ease-out",
          open ? "translate-x-0" : "translate-x-full",
          // lg+: in-flow column (relative so the resize handle can anchor to it);
          // width follows the collapse toggle / the resizable width var.
          "lg:relative lg:z-auto lg:translate-x-0 lg:transition-none",
          collapsed ? "lg:w-10" : "lg:w-[var(--rp-w)]",
        )}
      >
        {/* Resize handle (desktop, expanded only) — sits on the left edge. */}
        {!collapsed && (
          <div
            onMouseDown={onResizeStart}
            className="adk-right-panel-resize absolute left-0 top-0 z-10 hidden h-full w-1.5 -ml-0.5 cursor-col-resize hover:bg-primary/30 lg:block"
            title="Drag to resize"
            aria-hidden
          />
        )}
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
