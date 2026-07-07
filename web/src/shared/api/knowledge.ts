/** Knowledge-graph API (Task 1). Read-only wiki + own-memory graph. */
import { apiFetch } from "./client"

export interface GraphNode {
  id: string
  label: string
  kind: "domain" | "inbox" | "semantic" | "episodic"
  contested?: boolean
  sources?: number
  confidence?: number
  status?: string
  topic?: string
}

export interface GraphLink {
  source: string
  target: string
  missing?: boolean
  overlay?: boolean
}

export interface Graph {
  nodes: GraphNode[]
  links: GraphLink[]
}

export interface WikiPage {
  status: string
  slug?: string
  title?: string
  contested?: boolean
  frontmatter?: Record<string, unknown>
  body?: string
  sources?: string[]
}

export interface MemoryItemDetail {
  status: string
  id?: string
  topic?: string
  text?: string
  memory_type?: string
  item_status?: string
  confidence?: number
  sources?: string[]
  supersedes?: string[]
  created?: string
  updated?: string
}

// `user` scopes memory/inbox to a specific project — used by the desktop shell
// (no auth, single-user loopback) to view the CURRENT project's memory. Ignored
// server-side when a request is authenticated (web), where the principal wins.
const _u = (user?: string) => (user ? `?user=${encodeURIComponent(user)}` : "")

export const fetchWikiGraph = (user?: string) =>
  apiFetch<Graph>(`/api/knowledge/wiki/graph${_u(user)}`)
export const fetchWikiPage = (slug: string, user?: string) =>
  apiFetch<WikiPage>(`/api/knowledge/wiki/page/${encodeURIComponent(slug)}${_u(user)}`)
export const fetchMemoryGraph = (user?: string) =>
  apiFetch<Graph>(`/api/knowledge/memory/graph${_u(user)}`)
export const fetchMemoryItem = (id: string, user?: string) =>
  apiFetch<MemoryItemDetail>(`/api/knowledge/memory/item/${encodeURIComponent(id)}${_u(user)}`)
