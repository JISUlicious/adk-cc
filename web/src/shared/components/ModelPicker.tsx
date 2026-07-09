import { useEffect, useMemo, useState } from "react"
import { Check, Cpu } from "lucide-react"
import { listDesktopModels, selectModel, type DesktopModel } from "@/shared/api/desktop-settings"
import { ApiError } from "@/shared/api/client"

/**
 * `/model` command palette — switch the active model across ALL providers.
 * Lists every provider's models (grouped), the active one checked; picking one
 * sets that provider active + its model (POST select-model). Desktop only
 * (uses /desktop/settings/models).
 */

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return (e.body as { detail?: string } | undefined)?.detail || e.message
  return (e as Error)?.message || String(e)
}
function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}

interface Row { provider: string; model: string; active: boolean }

export function ModelPicker({ onClose, onPicked }: {
  onClose: () => void
  onPicked?: (label: string) => void
}) {
  const [endpoints, setEndpoints] = useState<DesktopModel[]>([])
  const [activeName, setActiveName] = useState<string | null>(null)
  const [q, setQ] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    listDesktopModels()
      .then((r) => { if (alive) { setEndpoints(r.endpoints); setActiveName(r.active) } })
      .catch((e) => alive && setErr(errMsg(e)))
    return () => { alive = false }
  }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  const rows: Row[] = useMemo(() => {
    const all = endpoints.flatMap((e) => {
      const models = e.models?.length ? e.models : [e.model]
      return models.map((m) => ({ provider: e.name, model: m, active: activeName === e.name && e.model === m }))
    })
    const s = q.trim().toLowerCase()
    return s ? all.filter((r) => baseName(r.model).toLowerCase().includes(s) || r.provider.toLowerCase().includes(s)) : all
  }, [endpoints, activeName, q])

  async function pick(r: Row) {
    setBusy(true); setErr(null)
    try { await selectModel(r.provider, r.model); onPicked?.(baseName(r.model)); onClose() }
    catch (e) { setErr(errMsg(e)); setBusy(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 pt-[12vh]" onMouseDown={onClose}>
      <div className="w-[min(90vw,440px)] overflow-hidden rounded-lg border border-border bg-popover shadow-xl" onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border/60 px-3 py-2">
          <Cpu className="h-4 w-4 text-primary" />
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Switch model…"
            className="w-full bg-transparent text-sm outline-none"
          />
        </div>
        <ul className="max-h-[50vh] overflow-y-auto py-1">
          {rows.length === 0 && (
            <li className="px-3 py-6 text-center text-xs text-muted-foreground">
              {endpoints.length ? "No matching models." : "No providers configured — add one in Settings → Models."}
            </li>
          )}
          {rows.map((r) => (
            <li key={`${r.provider}:${r.model}`}>
              <button
                type="button"
                disabled={busy}
                onClick={() => pick(r)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-accent disabled:opacity-50"
              >
                <Check className={"h-3.5 w-3.5 " + (r.active ? "text-green-600" : "text-transparent")} />
                <span className="font-mono text-xs">{baseName(r.model)}</span>
                <span className="ml-auto text-[10px] text-muted-foreground">{r.provider}</span>
              </button>
            </li>
          ))}
        </ul>
        {err && <p className="border-t border-border/60 px-3 py-2 text-xs text-destructive">{err}</p>}
      </div>
    </div>
  )
}
