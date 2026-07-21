import { useEffect, useMemo, useRef, useState } from "react"
import { Check, ChevronDown } from "lucide-react"
import { cn } from "@/shared/lib/utils"

/**
 * Searchable model picker: an input + anchored dropdown that filters the
 * option list as the user types (replaces the plain `<select>` — with dozens
 * of discovered models a non-searchable list is unusable).
 *
 * Closed: shows the current value (short name). Focus/typing opens the list;
 * ArrowUp/Down navigate, Enter picks, Escape reverts+closes, click-outside
 * closes. Matching is case-insensitive against both the short name and the
 * full id, so "qwen" and "openai/qwen" both hit.
 */

function baseName(id: string): string {
  const i = id.indexOf("/")
  return i >= 0 ? id.slice(i + 1) : id
}

export function ModelCombobox({ options, value, onPick, disabled, placeholder, className }: {
  options: string[]
  value: string
  onPick: (model: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState("")        // live query while open
  const [cursor, setCursor] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const listRef = useRef<HTMLUListElement>(null)

  const matches = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return options
    return options.filter(
      (m) => baseName(m).toLowerCase().includes(s) || m.toLowerCase().includes(s),
    )
  }, [options, q])

  // Keep the highlighted row in view while navigating with the keyboard.
  useEffect(() => {
    listRef.current?.children[cursor]?.scrollIntoView({ block: "nearest" })
  }, [cursor, open])

  useEffect(() => {
    if (!open) return
    const onDocDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) close()
    }
    document.addEventListener("mousedown", onDocDown)
    return () => document.removeEventListener("mousedown", onDocDown)
  }, [open])

  function close() {
    setOpen(false)
    setQ("")
    setCursor(0)
  }

  function pick(m: string) {
    close()
    if (m !== value) onPick(m)
  }

  function handleKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open && (e.key === "ArrowDown" || e.key === "Enter")) {
      e.preventDefault()
      setOpen(true)
      return
    }
    if (!open) return
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setCursor((i) => Math.min(i + 1, matches.length - 1))
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setCursor((i) => Math.max(i - 1, 0))
    } else if (e.key === "Enter") {
      e.preventDefault()
      if (matches[cursor]) pick(matches[cursor])
    } else if (e.key === "Escape" || e.key === "Tab") {
      close()
    }
  }

  return (
    <div ref={rootRef} className={cn("relative min-w-0", className)}>
      <div className="flex items-center rounded border border-input bg-background">
        <input
          value={open ? q : baseName(value)}
          disabled={disabled}
          placeholder={open ? baseName(value) : placeholder ?? "model"}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            if (!open) setOpen(true)
            setQ(e.target.value)
            setCursor(0)
          }}
          onKeyDown={handleKey}
          className="min-w-0 flex-1 bg-transparent px-1.5 py-1 font-mono text-xs outline-none disabled:opacity-50"
        />
        <button
          type="button"
          tabIndex={-1}
          disabled={disabled}
          onClick={() => (open ? close() : setOpen(true))}
          className="px-1 text-muted-foreground disabled:opacity-50"
          title="Show models"
        >
          <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
        </button>
      </div>
      {open && (
        <ul
          ref={listRef}
          className="absolute left-0 right-0 top-full z-30 mt-1 max-h-56 overflow-y-auto rounded-md border border-border bg-popover py-1 shadow-lg"
        >
          {matches.length === 0 && (
            <li className="px-2 py-2 text-center text-[11px] text-muted-foreground">No matching models.</li>
          )}
          {matches.map((m, i) => (
            <li key={m}>
              <button
                type="button"
                onMouseEnter={() => setCursor(i)}
                onClick={() => pick(m)}
                className={cn(
                  "flex w-full items-center gap-1.5 px-2 py-1 text-left font-mono text-xs",
                  i === cursor && "bg-accent",
                )}
                title={m}
              >
                <Check className={cn("h-3 w-3 shrink-0", m === value ? "text-green-600" : "text-transparent")} />
                <span className="truncate">{baseName(m)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
