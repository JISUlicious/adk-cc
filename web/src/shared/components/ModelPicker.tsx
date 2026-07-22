import { useEffect, useMemo, useState } from "react"
import { Check, Cpu, RotateCcw } from "lucide-react"
import { listDesktopModels, type DesktopModel } from "@/shared/api/desktop-settings"
import { patchSessionState, type Session } from "@/shared/api/sessions"
import { ApiError } from "@/shared/api/client"

/**
 * `/model` command palette — pins a model for THIS SESSION.
 *
 * Invoked from inside a session (slash command / composer chip), so the pick
 * writes the session's `model_endpoint`/`model_id` state via the standard
 * no-turn state PATCH (same path as the plan-mode toggle) — it does NOT touch
 * the registry: the global default stays whatever Settings → Models says, and
 * every other session keeps following it. A "reset" row returns the session
 * to the default. Desktop only (uses /desktop/settings/models for the list).
 */

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return (e.body as { detail?: string } | undefined)?.detail || e.message
  return (e as Error)?.message || String(e)
}
function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}

interface Row { provider: string; model: string; isDefault: boolean; keyMissing: boolean }

export function ModelPicker({ appName, userId, sessionId, pinnedEndpoint, pinnedModel, onClose, onPicked }: {
  appName: string
  userId: string
  sessionId: string
  /** The session's current pin (from session.state), if any. */
  pinnedEndpoint?: string | null
  pinnedModel?: string | null
  onClose: () => void
  /** label = short model name, or null for "reset to default". The patched
   *  Session is handed back so the caller can refresh its state/events. */
  onPicked?: (label: string | null, session: Session) => void
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
      return models.map((m) => ({
        provider: e.name,
        model: m,
        isDefault: activeName === e.name && e.model === m,
        keyMissing: e.api_key_present === false,
      }))
    })
    const s = q.trim().toLowerCase()
    return s ? all.filter((r) => baseName(r.model).toLowerCase().includes(s) || r.provider.toLowerCase().includes(s)) : all
  }, [endpoints, activeName, q])

  const pinned = !!pinnedEndpoint
  const isCurrent = (r: Row) =>
    pinned ? r.provider === pinnedEndpoint && r.model === pinnedModel : r.isDefault

  async function patch(delta: Record<string, unknown>, label: string | null) {
    setBusy(true); setErr(null)
    try {
      const s = await patchSessionState(appName, userId, sessionId, delta)
      onPicked?.(label, s)
      onClose()
    } catch (e) { setErr(errMsg(e)); setBusy(false) }
  }
  const pick = (r: Row) =>
    patch({ model_endpoint: r.provider, model_id: r.model }, baseName(r.model))
  const reset = () => patch({ model_endpoint: null, model_id: null }, null)

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 pt-[12vh]" onMouseDown={onClose}>
      <div className="w-[min(90vw,440px)] overflow-hidden rounded-lg border border-border bg-popover shadow-xl" onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 border-b border-border/60 px-3 py-2">
          <Cpu className="h-4 w-4 text-primary" />
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Switch model for this session…"
            className="w-full bg-transparent text-sm outline-none"
          />
        </div>
        <ul className="max-h-[50vh] overflow-y-auto py-1">
          {pinned && !q.trim() && (
            <li>
              <button
                type="button"
                disabled={busy}
                onClick={reset}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-muted-foreground hover:bg-accent disabled:opacity-50"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                <span>Reset to default model</span>
              </button>
            </li>
          )}
          {rows.length === 0 && (
            <li className="px-3 py-6 text-center text-xs text-muted-foreground">
              {endpoints.length ? "No matching models." : "No providers configured — add one in Settings → Models."}
            </li>
          )}
          {rows.map((r) => (
            <li key={`${r.provider}:${r.model}`}>
              <button
                type="button"
                disabled={busy || r.keyMissing}
                onClick={() => pick(r)}
                title={r.keyMissing ? `${r.provider} has no API key configured` : undefined}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-accent disabled:opacity-50"
              >
                <Check className={"h-3.5 w-3.5 " + (isCurrent(r) ? "text-green-600" : "text-transparent")} />
                <span className="font-mono text-xs">{baseName(r.model)}</span>
                {r.keyMissing && <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-600">no key</span>}
                <span className="ml-auto flex items-center gap-1.5 text-[10px] text-muted-foreground">
                  {r.isDefault && <span className="rounded bg-muted px-1">default</span>}
                  {r.provider}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <p className="border-t border-border/60 px-3 py-1.5 text-[10px] text-muted-foreground">
          Applies to <span className="font-medium">this session</span> only — the default model is set in Settings → Models.
        </p>
        {err && <p className="border-t border-border/60 px-3 py-2 text-xs text-destructive">{err}</p>}
      </div>
    </div>
  )
}
