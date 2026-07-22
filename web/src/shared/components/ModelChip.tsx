import { useEffect, useState } from "react"
import { Cpu, Pin } from "lucide-react"
import { listDesktopModels } from "@/shared/api/desktop-settings"
import { IS_DESKTOP } from "@/shared/lib/platform"
import { cn } from "@/shared/lib/utils"

/**
 * Current-model chip for the composer meta row: shows which model the next
 * turn will use, right where the user is typing.
 *
 * Two sources, session pin first: when the session carries a `/model` pin
 * (`pinnedModel`, prop-driven from session.state — updates instantly after a
 * patch), it wins and is marked with a pin glyph; otherwise the GLOBAL
 * default is fetched from the registry. Clicking opens the per-session
 * palette (`interactive` only when a session is active — the pin is session
 * state, so there's nothing to pin without one). Desktop-only.
 *
 * `refreshKey`: bump to re-read the global default (palette pick, Settings
 * close — both can change it).
 */

function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}

export function ModelChip({ pinnedModel, refreshKey, interactive = true, onClick }: {
  /** Full model id pinned on THIS session (session.state.model_id), if any. */
  pinnedModel?: string | null
  refreshKey?: number
  interactive?: boolean
  onClick?: () => void
}) {
  const [globalLabel, setGlobalLabel] = useState<string | null>(null)
  const [provider, setProvider] = useState<string>("")

  useEffect(() => {
    if (!IS_DESKTOP) return
    let alive = true
    listDesktopModels()
      .then((r) => {
        if (!alive) return
        const active = r.endpoints.find((e) => e.name === r.active)
        setGlobalLabel(active ? baseName(active.model) : null)
        setProvider(active?.name ?? "")
      })
      .catch(() => alive && setGlobalLabel(null))
    return () => { alive = false }
  }, [refreshKey])

  const label = pinnedModel ? baseName(pinnedModel) : globalLabel
  if (!IS_DESKTOP || !label) return null

  const title = pinnedModel
    ? `Model pinned for this session: ${label} — click to switch (/model)`
    : `Default model: ${provider} · ${label}` +
      (interactive ? " — click to pin one for this session (/model)" : " (set in Settings → Models)")

  return (
    <button
      type="button"
      onClick={interactive ? onClick : undefined}
      disabled={!interactive}
      className={cn(
        "inline-flex max-w-[14rem] items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary",
        interactive && "hover:bg-primary/20",
        !interactive && "cursor-default",
      )}
      title={title}
    >
      {pinnedModel ? <Pin className="h-3 w-3 shrink-0" /> : <Cpu className="h-3 w-3 shrink-0" />}
      <span className="truncate font-mono">{label}</span>
    </button>
  )
}
