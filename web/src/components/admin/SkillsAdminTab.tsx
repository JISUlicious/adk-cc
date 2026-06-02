import { useRef, useState } from "react"
import { Trash2, Upload } from "lucide-react"

import { listSkills, uploadSkill, deleteSkill } from "@/api/admin"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
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
    <div className="space-y-4">
      {error && <p className="text-sm text-destructive">{error}</p>}

      <p className="text-sm text-muted-foreground">
        Upload a skill as a <code className="rounded bg-muted px-1">.zip</code> containing
        a <code className="rounded bg-muted px-1">SKILL.md</code> manifest. Skills
        hot-reload — available on the next session.
      </p>

      <div className="rounded-md border border-border p-4 space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <label className="text-sm">
            Skill name
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-skill"
            />
          </label>
          <label className="text-sm">
            Zip file
            <Input
              ref={fileRef}
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
        </div>
        <Button size="sm" onClick={upload} disabled={busy}>
          <Upload className="mr-1 h-4 w-4" /> Upload
        </Button>
      </div>

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : data && data.length > 0 ? (
        <ul className="divide-y divide-border rounded-md border border-border">
          {data.map((s) => (
            <li key={s} className="flex items-center justify-between p-3">
              <span className="font-medium">{s}</span>
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
        <p className="text-sm text-muted-foreground">No skills uploaded.</p>
      )}
    </div>
  )
}
