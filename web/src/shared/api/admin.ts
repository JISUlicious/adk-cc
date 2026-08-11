/**
 * Typed wrappers over the admin-panel backend routes.
 *
 * MCP servers + skills + credentials are tenant-scoped to the CALLER'S OWN org
 * (the backend rejects any other tenant). Model endpoints are global (/admin/...).
 *
 * The real authorization gate is server-side (admin role → 403); these
 * helpers just shape the requests/responses.
 */

import { apiFetch } from "./client"
import { decodeJwtPayload, getToken } from "./auth"

/** The admin panel manages the CALLER'S OWN tenant (org_id == tenant_id), read
 * from the signed-in token. Falls back to "local" when the tenant is unknown
 * (dev no-auth / opaque token). The server independently enforces that the
 * caller may only touch their own tenant. */
export function callerTenant(): string {
  const p = decodeJwtPayload(getToken() ?? "")
  return (p?.tenant as string) || "local"
}

const T = (): string => `/tenants/${callerTenant()}`

// --- MCP servers ----------------------------------------------------------

export interface McpServer {
  server_name: string
  transport: string
  url: string
  credential_key?: string | null
  tool_filter?: string[] | null
  require_confirmation?: boolean
  save_resources_as_artifacts?: boolean
  use_mcp_resources?: boolean
}

export async function listMcpServers(): Promise<McpServer[]> {
  const r = await apiFetch<{ servers: McpServer[] }>(`${T()}/mcp-servers`)
  return r.servers
}

export async function putMcpServer(s: McpServer): Promise<void> {
  const { server_name, ...rest } = s
  await apiFetch(`${T()}/mcp-servers/${encodeURIComponent(server_name)}`, {
    method: "PUT",
    body: JSON.stringify(rest),
  })
}

export async function deleteMcpServer(name: string): Promise<void> {
  await apiFetch(`${T()}/mcp-servers/${encodeURIComponent(name)}`, { method: "DELETE" })
}

// --- Skills ---------------------------------------------------------------

export async function listSkills(): Promise<string[]> {
  const r = await apiFetch<{ skills: string[] }>(`${T()}/skills`)
  return r.skills
}

export async function uploadSkill(name: string, zip: Blob): Promise<void> {
  // Raw zip body (the route reads request.body() directly, not multipart).
  await apiFetch(`${T()}/skills/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/zip" },
    body: zip,
  })
}

export async function deleteSkill(name: string): Promise<void> {
  await apiFetch(`${T()}/skills/${encodeURIComponent(name)}`, { method: "DELETE" })
}

// --- Credentials (names only; values write-only) --------------------------

export async function listCredentialKeys(): Promise<string[]> {
  const r = await apiFetch<{ keys: string[] }>(`${T()}/credentials`)
  return r.keys
}

export async function putCredential(key: string, value: string): Promise<void> {
  await apiFetch(`${T()}/credentials/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: JSON.stringify({ value }),
  })
}

export async function deleteCredential(key: string): Promise<void> {
  await apiFetch(`${T()}/credentials/${encodeURIComponent(key)}`, { method: "DELETE" })
}

// --- Model endpoints (global) ---------------------------------------------

export interface ModelEndpoint {
  name: string
  model: string
  api_base: string
  // The ACTUAL api key ("" = keyless). Write-only: never returned by list —
  // omit the field on updates to keep the stored key.
  api_key?: string
  api_key_env?: string // legacy env-var indirection
  api_key_present?: boolean
  key_source?: "inline" | "env" | "keyless"
}

export async function listModelEndpoints(): Promise<{
  endpoints: ModelEndpoint[]
  active: string | null
}> {
  return apiFetch(`/admin/model-endpoints`)
}

export async function putModelEndpoint(e: ModelEndpoint): Promise<void> {
  const { name, ...rest } = e
  await apiFetch(`/admin/model-endpoints/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify(rest),
  })
}

export async function deleteModelEndpoint(name: string): Promise<void> {
  await apiFetch(`/admin/model-endpoints/${encodeURIComponent(name)}`, {
    method: "DELETE",
  })
}

export async function activateModelEndpoint(name: string): Promise<void> {
  await apiFetch(`/admin/model-endpoints/${encodeURIComponent(name)}/activate`, {
    method: "POST",
  })
}

// --- Wiki document management (#129) --------------------------------------
// Admin/owner curation of the shared domain wiki: edit/delete pages and
// adjudicate the librarian's review queue. Server-side: a PUT stamps the
// page human_edited, and the librarian holds any would-be supersession of
// such a page until a human accepts it here.

export interface WikiAdminPage {
  slug: string
  title: string
  contested: boolean
  human_edited: string | null
}

export interface WikiReviewItem {
  claim_hash: string
  status: string
  slug?: string
  user_id?: string
  claim?: string
  classification?: string
  reason?: string
}

export async function listWikiPages(): Promise<WikiAdminPage[]> {
  const r = await apiFetch<{ pages: WikiAdminPage[] }>(`${T()}/wiki-pages`)
  return r.pages
}

export async function putWikiPage(
  slug: string,
  body: string,
  frontmatter?: Record<string, unknown>,
): Promise<void> {
  await apiFetch(`${T()}/wiki-pages/${encodeURIComponent(slug)}`, {
    method: "PUT",
    body: JSON.stringify({ body, frontmatter }),
  })
}

export async function deleteWikiPage(slug: string): Promise<void> {
  await apiFetch(`${T()}/wiki-pages/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  })
}

export async function listWikiReview(): Promise<{
  queue: WikiReviewItem[]
  contested_pages: string[]
}> {
  return apiFetch(`${T()}/wiki-review`)
}

export async function adjudicateWikiClaim(
  claimHash: string,
  action: "accept" | "reject",
  note = "",
): Promise<void> {
  await apiFetch(`${T()}/wiki-review/${encodeURIComponent(claimHash)}`, {
    method: "POST",
    body: JSON.stringify({ action, note }),
  })
}
