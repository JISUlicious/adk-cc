import { useMemo } from "react"
import {
  ClipboardList,
  HelpCircle,
  LogOut,
  Plus,
  Settings,
  SunMoon,
} from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * Inline slash-command picker shown above the composer when the input
 * starts with `/`. Commands are UI-only sugar: each one either runs an
 * `action` callback (open settings, new session, toggle theme, …) or
 * inserts a `message` that gets sent to the agent so the agent itself
 * acts on it (`enter plan mode`, `exit plan mode`, …).
 *
 * adk-cc doesn't have a backend slash protocol — these are UI
 * shortcuts the user discovers via auto-complete.
 */

export type SlashAction =
  | "help"
  | "clear"
  | "settings"
  | "theme"
  | "signout"
  | "plan"
  | "exit-plan"

export interface SlashCommand {
  /** What the user types after `/`. Match is prefix-insensitive. */
  name: string
  /** One-line summary shown in the picker. */
  description: string
  icon: typeof Plus
  /** Either action (UI-only) or message (text sent to the agent). */
  kind: { type: "action"; action: SlashAction } | { type: "message"; text: string }
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: "help",
    description: "Show available slash commands",
    icon: HelpCircle,
    kind: { type: "action", action: "help" },
  },
  {
    name: "clear",
    description: "Start a fresh session in the current agent",
    icon: Plus,
    kind: { type: "action", action: "clear" },
  },
  {
    name: "plan",
    description: "Switch the session to plan mode",
    icon: ClipboardList,
    kind: { type: "action", action: "plan" },
  },
  {
    name: "exit-plan",
    description: "Switch the session back to default mode",
    icon: ClipboardList,
    kind: { type: "action", action: "exit-plan" },
  },
  {
    name: "theme",
    description: "Cycle theme (light → dark → system)",
    icon: SunMoon,
    kind: { type: "action", action: "theme" },
  },
  {
    name: "settings",
    description: "Open settings",
    icon: Settings,
    kind: { type: "action", action: "settings" },
  },
  {
    name: "signout",
    description: "Sign out and clear the stored token",
    icon: LogOut,
    kind: { type: "action", action: "signout" },
  },
]

export function SlashCommandMenu({
  query,
  selectedIndex,
  onPick,
}: {
  /** Text after `/` so we can prefix-match commands. */
  query: string
  /** Which row is keyboard-focused (Up/Down). */
  selectedIndex: number
  onPick: (cmd: SlashCommand) => void
}) {
  const filtered = useMemo(() => filterSlash(query), [query])
  if (filtered.length === 0) return null

  return (
    <div className="rounded-md border border-border bg-popover shadow-md text-sm overflow-hidden">
      <ul role="listbox">
        {filtered.map((cmd, i) => {
          const Icon = cmd.icon
          return (
            <li
              key={cmd.name}
              role="option"
              aria-selected={i === selectedIndex}
              className={cn(
                "flex items-center gap-2 px-3 py-2 cursor-pointer",
                i === selectedIndex && "bg-accent",
              )}
              onMouseDown={(e) => {
                // mousedown not click so we beat the textarea blur
                e.preventDefault()
                onPick(cmd)
              }}
            >
              <Icon className="h-4 w-4 text-muted-foreground" />
              <span className="font-mono text-xs">/{cmd.name}</span>
              <span className="text-muted-foreground text-xs ml-2 truncate">
                {cmd.description}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** Prefix-match commands against `/foo` typed text. Returns the
 * filtered list; the caller is responsible for clamping the selection
 * index. */
export function filterSlash(query: string): SlashCommand[] {
  const q = query.trim().toLowerCase()
  if (!q) return SLASH_COMMANDS
  return SLASH_COMMANDS.filter((c) => c.name.toLowerCase().startsWith(q))
}
