import { useState } from "react"
import { CheckCircle2, Circle, Plus, Trash2 } from "lucide-react"

import {
  listModelEndpoints,
  putModelEndpoint,
  deleteModelEndpoint,
  activateModelEndpoint,
  type ModelEndpoint,
} from "@/shared/api/admin"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { useAsync } from "./useAsync"

const BLANK: ModelEndpoint = {
  name: "",
  model: "",
  api_base: "",
  api_key: "", // actual key; empty = keyless (local model servers)
}

export function ModelAdminTab() {
  const { data, error, loading, reload, setError } = useAsync(listModelEndpoints)
  const [draft, setDraft] = useState<ModelEndpoint | null>(null)
  const [busy, setBusy] = useState(false)

  async function save() {
    if (!draft) return
    if (!draft.name.trim() || !draft.model.trim() || !draft.api_base.trim()) {
      setError("name, model, and api_base are required")
      return
    }
    setBusy(true)
    try {
      await putModelEndpoint(draft)
      setDraft(null)
      await reload()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function activate(name: string) {
    setBusy(true)
    try {
      await activateModelEndpoint(name)
      await reload()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(name: string) {
    setBusy(true)
    try {
      await deleteModelEndpoint(name)
      await reload()
    } catch (e) {
      // surface the 409 last/active guard message
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const endpoints = data?.endpoints ?? []
  const activeName = data?.active ?? null

  return (
    <div className="space-y-4">
      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Model backends. Activating one switches the live model — no restart.
          API keys are read from the named env var (never stored here).
        </p>
        <Button size="sm" onClick={() => setDraft({ ...BLANK })} disabled={!!draft}>
          <Plus className="mr-1 h-4 w-4" /> Add
        </Button>
      </div>

      {draft && (
        <div className="rounded-md border border-border p-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="text-sm">
              Name
              <Input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="claude"
              />
            </label>
            <label className="text-sm">
              API key
              <Input
                type="password"
                autoComplete="off"
                value={draft.api_key ?? ""}
                onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
                placeholder="empty = keyless (local server)"
              />
            </label>
          </div>
          <label className="text-sm block">
            Model id
            <Input
              value={draft.model}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              placeholder="anthropic/claude-sonnet-4-5"
            />
          </label>
          <label className="text-sm block">
            API base
            <Input
              value={draft.api_base}
              onChange={(e) => setDraft({ ...draft, api_base: e.target.value })}
              placeholder="https://host:port/v1"
            />
          </label>
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={busy}>Save</Button>
            <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : endpoints.length > 0 ? (
        <ul className="divide-y divide-border rounded-md border border-border">
          {endpoints.map((e) => {
            const isActive = e.name === activeName
            return (
              <li key={e.name} className="flex items-center justify-between p-3">
                <div className="flex items-center gap-3 min-w-0">
                  <button
                    onClick={() => !isActive && activate(e.name)}
                    disabled={busy || isActive}
                    title={isActive ? "Active" : "Activate"}
                    aria-label={isActive ? "Active" : `Activate ${e.name}`}
                  >
                    {isActive ? (
                      <CheckCircle2 className="h-5 w-5 text-primary" />
                    ) : (
                      <Circle className="h-5 w-5 text-muted-foreground hover:text-foreground" />
                    )}
                  </button>
                  <div className="min-w-0">
                    <p className="font-medium">
                      {e.name}
                      {isActive && (
                        <span className="ml-2 rounded bg-primary/10 px-1.5 py-0.5 text-xs text-primary">
                          active
                        </span>
                      )}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {e.model} · {e.api_base} · key:{" "}
                      {e.key_source === "inline" ? "set" : e.key_source === "env" ? `env ${e.api_key_env}` : "keyless"}
                      {e.api_key_present === false ? " (missing!)" : ""}
                    </p>
                  </div>
                </div>
                <div className="flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setDraft({ ...e })}
                  >
                    Edit
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => remove(e.name)}
                    disabled={busy || isActive}
                    title={isActive ? "Activate another first" : "Delete"}
                    aria-label={`Delete ${e.name}`}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                </div>
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No model endpoints.</p>
      )}
    </div>
  )
}
