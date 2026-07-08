import { useState } from "react"
import { FolderPlus } from "lucide-react"
import { Button } from "./ui/button"
import { Input } from "./ui/input"
import { pickDirectory } from "@/shared/lib/tauri"

/**
 * "Add a folder" button shared by the desktop Settings sections (Skills,
 * Working directories) and `/add-dir`. Uses the native Tauri folder picker when
 * IPC is present; when it is NOT (plain browser / capability ungranted) it falls
 * back to a typed-path `<Input>`. A CANCELLED native dialog does nothing (it does
 * not pop the manual input) — see `pickDirectory`'s null-vs-undefined contract.
 */
export function FolderPickerButton({
  label,
  placeholder,
  busy = false,
  onPick,
}: {
  label: string
  placeholder: string
  busy?: boolean
  onPick: (path: string) => void | Promise<void>
}) {
  const [typing, setTyping] = useState(false)
  const [text, setText] = useState("")

  async function click() {
    const picked = await pickDirectory()
    if (picked === undefined) {
      // No native IPC → offer a typed-path input.
      setText("")
      setTyping(true)
    } else if (picked) {
      await onPick(picked) // null = user cancelled → no-op
    }
  }

  async function submit() {
    const t = text.trim()
    setTyping(false)
    if (t) await onPick(t)
  }

  return (
    <div className="space-y-1.5">
      <Button size="sm" variant="outline" disabled={busy} onClick={click} title={label}>
        <FolderPlus className="h-3.5 w-3.5" /> {busy ? "Adding…" : label}
      </Button>
      {typing && (
        <div className="flex gap-1">
          <Input
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit()
              else if (e.key === "Escape") setTyping(false)
            }}
            placeholder={placeholder}
            className="h-8 flex-1 font-mono text-xs"
          />
          <Button size="sm" variant="outline" onClick={submit}>
            Add
          </Button>
        </div>
      )}
    </div>
  )
}
