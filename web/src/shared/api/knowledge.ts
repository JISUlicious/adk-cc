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

export const fetchWikiGraph = () => apiFetch<Graph>("/api/knowledge/wiki/graph")
export const fetchWikiPage = (slug: string) =>
  apiFetch<WikiPage>(`/api/knowledge/wiki/page/${encodeURIComponent(slug)}`)
export const fetchMemoryGraph = () => apiFetch<Graph>("/api/knowledge/memory/graph")
export const fetchMemoryItem = (id: string) =>
  apiFetch<MemoryItemDetail>(`/api/knowledge/memory/item/${encodeURIComponent(id)}`)
