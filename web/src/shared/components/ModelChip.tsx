import { useEffect, useState } from "react"
import { Cpu } from "lucide-react"
import { listDesktopModels } from "@/shared/api/desktop-settings"
import { IS_DESKTOP } from "@/shared/lib/platform"

/**
 * Current-model chip for the composer meta row: shows which model the next
 * turn will use, right where the user is typing. Clicking opens the `/model`
 * palette (the same searchable switcher). Desktop-only — the model registry
 * API lives under /desktop/settings; renders nothing elsewhere or until the
 * registry answers.
 *
 * `refreshKey`: bump to re-read the active model (after a palette pick or
 * when the Settings dialog closes — both can change it).
 */

function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}

export function ModelChip({ refreshKey, onClick }: {
  refreshKey?: number
  onClick?: () => void
}) {
  const [label, setLabel] = useState<string | null>(null)
  const [provider, setProvider] = useState<string>("")

  useEffect(() => {
    if (!IS_DESKTOP) return
    let alive = true
    listDesktopModels()
      .then((r) => {
        if (!alive) return
        const active = r.endpoints.find((e) => e.name === r.active)
        setLabel(active ? baseName(active.model) : null)
        setProvider(active?.name ?? "")
      })
      .catch(() => alive && setLabel(null))
    return () => { alive = false }
  }, [refreshKey])

  if (!IS_DESKTOP || !label) return null

  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex max-w-[14rem] items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary hover:bg-primary/20"
      title={`Active model: ${provider} · ${label} — click to switch (/model)`}
    >
      <Cpu className="h-3 w-3 shrink-0" />
      <span className="truncate font-mono">{label}</span>
    </button>
  )
}
