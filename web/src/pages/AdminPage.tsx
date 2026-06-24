import { useNavigate, useParams, Link } from "react-router-dom"
import { ArrowLeft } from "lucide-react"

import { Button } from "@/components/ui/button"
import { McpAdminTab } from "@/components/admin/McpAdminTab"
import { SkillsAdminTab } from "@/components/admin/SkillsAdminTab"
import { ModelAdminTab } from "@/components/admin/ModelAdminTab"
import { UsersAdminTab } from "@/components/admin/UsersAdminTab"

const TABS = [
  { id: "users", label: "Users" },
  { id: "mcp", label: "MCP Servers" },
  { id: "skills", label: "Skills" },
  { id: "models", label: "Model Endpoints" },
] as const

type TabId = (typeof TABS)[number]["id"]

export function AdminPage() {
  const { tab } = useParams<{ tab?: string }>()
  const navigate = useNavigate()
  const active: TabId = (TABS.find((t) => t.id === tab)?.id ?? "users") as TabId

  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="mb-6 flex items-center gap-3">
        <Link to="/">
          <Button variant="ghost" size="icon" aria-label="Back to chat">
            <ArrowLeft className="h-5 w-5" />
          </Button>
        </Link>
        <h1 className="text-xl font-semibold">Admin</h1>
      </header>

      <nav className="mb-6 flex gap-1 border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => navigate(`/admin/${t.id}`)}
            className={
              "px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors " +
              (active === t.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground")
            }
          >
            {t.label}
          </button>
        ))}
      </nav>

      <main>
        {active === "users" && <UsersAdminTab />}
        {active === "mcp" && <McpAdminTab />}
        {active === "skills" && <SkillsAdminTab />}
        {active === "models" && <ModelAdminTab />}
      </main>
    </div>
  )
}
