import { useEffect, useState } from "react"
import { Maximize2, X } from "lucide-react"

/**
 * Renders an untrusted HTML string inside a sandboxed <iframe srcdoc>, with an
 * expand-to-overlay affordance. The single home for the sandbox security
 * config — HtmlArtifactPreview (artifacts) and the desktop file viewer (local
 * .html files) both render through here so the rules live in ONE place.
 *
 * Security model — the HTML is agent/user-generated or from a local worktree,
 * i.e. untrusted.
 *
 * DEFAULT (`sandbox=""`): most restrictive. No scripts run; the frame is a
 * unique opaque origin (no allow-same-origin), so it CANNOT reach the parent
 * app's DOM, cookies, localStorage, or bearer token. HTML + CSS render; JS is
 * inert.
 *
 * OPT-IN (`VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1`, baked at build time):
 * flips the sandbox to `allow-scripts` so JS-driven content renders. Untrusted
 * JS then executes in the user's browser and can make network requests, but —
 * because we keep `allow-same-origin` HARD-OFF — it still cannot read the
 * parent's token / cookies / localStorage / DOM or navigate the top window.
 * NEVER add `allow-same-origin` here: `allow-scripts` + `allow-same-origin`
 * together is the combo that would let the frame exfiltrate the token.
 */
const ALLOW_SCRIPTS =
  String(import.meta.env.VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS ?? "") === "1"
// allow-same-origin is intentionally ABSENT — see the security note above.
const SANDBOX = ALLOW_SCRIPTS ? "allow-scripts" : ""

export function SandboxedHtml({
  html,
  title,
  inlineHeight = "h-[70vh] max-h-[640px] min-h-[320px]",
}: {
  html: string
  title: string
  inlineHeight?: string
}) {
  const [expanded, setExpanded] = useState(false)

  // Escape closes the expanded overlay.
  useEffect(() => {
    if (!expanded) return
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setExpanded(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [expanded])

  const frame = (heightClass: string) => (
    <iframe
      sandbox={SANDBOX}
      srcDoc={html}
      title={`${title} (preview)`}
      className={`w-full ${heightClass} rounded-md border border-border bg-white`}
    />
  )

  const scriptNote = ALLOW_SCRIPTS ? (
    <p className="text-[10px] text-muted-foreground">
      interactive preview — scripts run in an isolated sandbox (no access to
      your session)
    </p>
  ) : null

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        {scriptNote ?? <span />}
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
          aria-label={`Expand ${title} preview`}
        >
          <Maximize2 className="h-3 w-3" />
          expand
        </button>
      </div>
      {frame(inlineHeight)}
      {expanded && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          onClick={() => setExpanded(false)}
        >
          <div
            className="flex h-[92vh] w-full max-w-5xl flex-col rounded-lg border border-border bg-background shadow-lg"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b px-4 py-2">
              <span className="truncate font-mono text-xs">{title}</span>
              <button
                type="button"
                onClick={() => setExpanded(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Close preview"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 p-2">{frame("h-full")}</div>
            {scriptNote && <div className="px-4 pb-2">{scriptNote}</div>}
          </div>
        </div>
      )}
    </div>
  )
}
