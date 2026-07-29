import { type ReactNode } from "react"
import { Moon, Sun, Monitor } from "lucide-react"
import { useTheme, type ThemeMode } from "@/shared/lib/theme"
import { UI_SCALES, useUiScale, type UiScale } from "@/shared/lib/uiScale"
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

/** UI scale picker. Sits with the theme because both are local device
 *  preferences the user changes for the same reason: this screen, right now.
 *  Scales the root font size, so spacing and icons move with the type rather
 *  than the text growing inside boxes that stayed put. */
export function UiScaleSection() {
  const [scale, setScale] = useUiScale()
  const opt = (value: UiScale, label: string, hint: string) => (
    <button
      key={value}
      type="button"
      onClick={() => setScale(value)}
      className={cn(
        "flex flex-1 flex-col items-center gap-0.5 rounded-md border px-2 py-3 transition-colors",
        value === scale ? "border-primary bg-brand-tint" : "border-input hover:bg-accent",
      )}
    >
      {/* Each label previews its own step, so the choice is visible before
          committing to it rather than after the whole app resizes. */}
      <span style={{ fontSize: hint }} className="font-medium leading-none">
        Aa
      </span>
      <span className="text-xs">{label}</span>
    </button>
  )
  return (
    <section className="py-5">
      <h3 className="mb-1 text-sm font-semibold">Text size</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        Scales the whole interface. Stored on this device.
      </p>
      <div className="flex gap-2">
        {UI_SCALES.map((s) => opt(s.value, s.label, s.hint))}
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
