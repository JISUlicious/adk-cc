import { useState } from "react"
import { Plus, Trash2 } from "lucide-react"

import {
  listMcpServers,
  putMcpServer,
  deleteMcpServer,
  type McpServer,
} from "@/api/admin"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useAsync } from "./useAsync"

const BLANK: McpServer = {
  server_name: "",
  transport: "stdio",
  url: "",
  credential_key: "",
}

export function McpAdminTab() {
  const { data, error, loading, reload, setError } = useAsync(listMcpServers)
  const [draft, setDraft] = useState<McpServer | null>(null)
  const [busy, setBusy] = useState(false)

  async function save() {
    if (!draft) return
    if (!draft.server_name.trim() || !draft.url.trim()) {
      setError("server_name and url are required")
      return
    }
    setBusy(true)
    try {
      await putMcpServer({
        ...draft,
        credential_key: draft.credential_key?.trim() || null,
      })
      setDraft(null)
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
      await deleteMcpServer(name)
      await reload()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          MCP servers available to the agent. Tools appear as{" "}
          <code className="rounded bg-muted px-1">mcp__&lt;name&gt;__*</code>.
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
                value={draft.server_name}
                onChange={(e) => setDraft({ ...draft, server_name: e.target.value })}
                placeholder="github"
              />
            </label>
            <label className="text-sm">
              Transport
              <select
                className="flex h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                value={draft.transport}
                onChange={(e) => setDraft({ ...draft, transport: e.target.value })}
              >
                <option value="stdio">stdio</option>
                <option value="sse">sse</option>
                <option value="http">http</option>
              </select>
            </label>
          </div>
          <label className="text-sm block">
            URL / command
            <Input
              value={draft.url}
              onChange={(e) => setDraft({ ...draft, url: e.target.value })}
              placeholder="https://api.github.com/mcp  (or: python server.py)"
            />
          </label>
          <label className="text-sm block">
            Credential key (env var holding the bearer token; optional)
            <Input
              value={draft.credential_key ?? ""}
              onChange={(e) => setDraft({ ...draft, credential_key: e.target.value })}
              placeholder="GITHUB_MCP_TOKEN"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={!!draft.require_confirmation}
              onChange={(e) =>
                setDraft({ ...draft, require_confirmation: e.target.checked })
              }
            />
            Require confirmation on every call
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
      ) : data && data.length > 0 ? (
        <ul className="divide-y divide-border rounded-md border border-border">
          {data.map((s) => (
            <li key={s.server_name} className="flex items-center justify-between p-3">
              <div className="min-w-0">
                <p className="font-medium">{s.server_name}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {s.transport} · {s.url}
                  {s.credential_key ? ` · auth:${s.credential_key}` : ""}
                </p>
              </div>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setDraft({ ...BLANK, ...s })}
                >
                  Edit
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => remove(s.server_name)}
                  disabled={busy}
                  aria-label={`Delete ${s.server_name}`}
                >
                  <Trash2 className="h-4 w-4 text-destructive" />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No MCP servers configured.</p>
      )}
    </div>
  )
}
