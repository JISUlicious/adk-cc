import { type ReactNode } from "react"
import { Moon, Sun, Monitor } from "lucide-react"
import { useTheme, type ThemeMode } from "@/shared/lib/theme"
import { cn } from "@/shared/lib/utils"

/** Theme picker (light / dark / system). Shared by both shells' Appearance tab. */
export function ThemeSection() {
  const [mode, setMode] = useTheme()
  const opt = (value: ThemeMode, label: string, Icon: typeof Sun) => (
    <button
      type="button"
      onClick={() => setMode(value)}
      className={cn(
        "flex flex-1 flex-col items-center gap-1 rounded-md border px-2 py-3 text-xs transition-colors",
        value === mode ? "border-primary bg-brand-tint" : "border-input hover:bg-accent",
      )}
    >
      <Icon className="h-4 w-4" />
      {label}
    </button>
  )
  return (
    <section className="py-5">
      <h3 className="mb-3 text-sm font-semibold">Appearance</h3>
      <div className="flex gap-2">
        {opt("light", "Light", Sun)}
        {opt("dark", "Dark", Moon)}
        {opt("system", "System", Monitor)}
      </div>
    </section>
  )
}

/** A titled section with an "admin · org" pill — used to frame org/admin
 *  controls inside the web shell's settings tabs. */
export function AdminBlock({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="py-5">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
          admin · org
        </span>
      </div>
      {children}
    </section>
  )
}
