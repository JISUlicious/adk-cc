import { Palette, Server, Boxes, SlidersHorizontal } from "lucide-react"
import {
  CustomVariablesSection, UserMcpSection, UserSkillsSection,
} from "@/shared/pages/AccountPage"
import { SettingsFrame, useSecretBadges, type SettingsTab } from "@/shared/settings/SettingsFrame"
import { ThemeSection } from "@/shared/settings/sections"

/**
 * Desktop settings: the curated subset — Appearance + the per-project personal
 * tooling (MCP / Skills / Variables). No account/admin/team tabs, no sign-out
 * (single local user, no login). Composes the same shared SettingsFrame as web.
 */
export function DesktopSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const miss = useSecretBadges(open)
  const tabs: SettingsTab[] = [
    { id: "appearance", label: "Appearance", icon: Palette, render: () => <ThemeSection /> },
    { id: "mcp", label: "MCP", icon: Server, badge: miss.mcp, render: () => <UserMcpSection /> },
    { id: "skills", label: "Skills", icon: Boxes, badge: miss.skill, render: () => <UserSkillsSection /> },
    { id: "variables", label: "Variables", icon: SlidersHorizontal, render: () => <CustomVariablesSection /> },
  ]
  return <SettingsFrame open={open} onClose={onClose} tabs={tabs} />
}
