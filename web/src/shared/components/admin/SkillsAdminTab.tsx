import { useRef, useState } from "react"
import { Trash2, Upload } from "lucide-react"

import { listSkills, uploadSkill, deleteSkill } from "@/shared/api/admin"
import { Button } from "@/shared/components/ui/button"
import { Input } from "@/shared/components/ui/input"
import { useAsync } from "./useAsync"

export function SkillsAdminTab() {
  const { data, error, loading, reload, setError } = useAsync(listSkills)
  const [name, setName] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  async function upload() {
    if (!name.trim() || !file) {
      setError("a skill name and a .zip file are required")
      return
    }
    setBusy(true)
    try {
      await uploadSkill(name.trim(), file)
      setName("")
      setFile(null)
      if (fileRef.current) fileRef.current.value = ""
      await reload()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(skill: string) {
    setBusy(true)
    try {
      await deleteSkill(skill)
      await reload()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      {error && <p className="mb-2 text-sm text-destructive">{error}</p>}

      <p className="mb-3 text-xs text-muted-foreground">
        Upload a skill as a <code className="rounded bg-muted px-1">.zip</code> containing
        a <code className="rounded bg-muted px-1">SKILL.md</code> manifest. Skills
        hot-reload — available on the next session.
      </p>

      <form onSubmit={(e) => { e.preventDefault(); upload() }} className="flex items-center gap-2">
        <Input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="skill name"
          className="w-40 font-mono text-xs"
        />
        <input
          ref={fileRef}
          type="file"
          accept=".zip,application/zip"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="flex-1 text-sm text-muted-foreground file:mr-3 file:rounded-md file:border-0 file:bg-muted file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-foreground hover:file:bg-accent"
        />
        <Button type="submit" size="sm" disabled={busy}>
          <Upload className="h-3.5 w-3.5" /> Upload
        </Button>
      </form>

      {loading ? (
        <p className="mt-3 text-sm text-muted-foreground">Loading…</p>
      ) : data && data.length > 0 ? (
        <ul className="mt-3 divide-y divide-border/60 border-t border-border/60 pt-1">
          {data.map((s) => (
            <li key={s} className="flex items-center justify-between py-2.5">
              <span className="text-sm font-medium">{s}</span>
              <Button
                size="icon"
                variant="ghost"
                onClick={() => remove(s)}
                disabled={busy}
                aria-label={`Delete ${s}`}
              >
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-muted-foreground">No skills uploaded.</p>
      )}
    </div>
  )
}
