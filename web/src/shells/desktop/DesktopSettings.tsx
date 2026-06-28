import { Palette } from "lucide-react"
import { SettingsFrame, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"

/**
 * Desktop settings. Currently just Appearance — the per-project tooling tabs
 * (MCP / Skills / Variables) depend on the /auth/secrets + /auth/mcp-servers +
 * /auth/skills routes, which mount only with an identity provider. Making them
 * work per-project in no-auth desktop mode is a focused follow-up (mount those
 * credential/registry routes with a project-scoped resolver); they're omitted
 * here rather than shown non-functional. Composes the same shared SettingsFrame
 * as the web shell.
 */
export function DesktopSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const tabs: SettingsTab[] = [
    { id: "appearance", label: "Appearance", icon: Palette, render: () => <ThemeSection /> },
  ]
  return <SettingsFrame open={open} onClose={onClose} tabs={tabs} />
}
