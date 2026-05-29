/**
 * Client for ADK's artifact REST surface. Artifacts are files the agent
 * published via the `save_as_artifact` tool; the panel + inline chip let
 * the user download them.
 *
 * Routes (served by google.adk.cli.adk_web_server):
 *   GET /apps/{app}/users/{user}/sessions/{session}/artifacts            → string[] (filenames)
 *   GET .../artifacts/{name}/versions                                    → number[] (version ids)
 *   GET .../artifacts/{name}                                             → latest Part (JSON)
 *   GET .../artifacts/{name}/versions/{v}                                → that version's Part (JSON)
 *
 * The Part is JSON with the bytes base64-encoded under inline_data.data
 * (camelCase `inlineData` on the wire via Pydantic's to_camel alias —
 * we accept both). Downloads can't use a plain <a download> link because
 * the auth middleware gates these routes and the browser won't send the
 * Bearer header on a direct navigation; so we fetch with the header,
 * decode, and trigger the download from an in-memory blob.
 */

import { apiFetch } from "./client"
import { getToken } from "./auth"

function artifactsBase(
  appName: string,
  userId: string,
  sessionId: string,
): string {
  return (
    `/apps/${encodeURIComponent(appName)}` +
    `/users/${encodeURIComponent(userId)}` +
    `/sessions/${encodeURIComponent(sessionId)}/artifacts`
  )
}

/** List artifact filenames for the session. */
export async function listArtifacts(
  appName: string,
  userId: string,
  sessionId: string,
): Promise<string[]> {
  return apiFetch<string[]>(artifactsBase(appName, userId, sessionId))
}

/** List the version ids (ints, ascending) for one artifact. */
export async function listArtifactVersions(
  appName: string,
  userId: string,
  sessionId: string,
  filename: string,
): Promise<number[]> {
  return apiFetch<number[]>(
    `${artifactsBase(appName, userId, sessionId)}/${encodeURIComponent(filename)}/versions`,
  )
}

/**
 * Fetch one artifact and trigger a browser download. When `version` is
 * omitted, the latest version is downloaded. Throws on HTTP / decode
 * errors so the caller can surface them.
 */
export async function downloadArtifact(
  appName: string,
  userId: string,
  sessionId: string,
  filename: string,
  version?: number,
): Promise<void> {
  const base = `${artifactsBase(appName, userId, sessionId)}/${encodeURIComponent(filename)}`
  const url =
    version === undefined
      ? base
      : `${base}/versions/${encodeURIComponent(String(version))}`

  const headers: Record<string, string> = {}
  const tok = getToken()
  if (tok) headers["Authorization"] = `Bearer ${tok}`

  const resp = await fetch(url, { headers })
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}`)
  }

  const payload = (await resp.json()) as Record<string, unknown>
  const inline = (payload.inline_data ?? payload.inlineData) as
    | { data?: string; mime_type?: string; mimeType?: string }
    | undefined
  if (!inline?.data) {
    throw new Error("artifact has no inline_data")
  }
  const mime = inline.mime_type || inline.mimeType || "application/octet-stream"
  const bytes = base64ToBytes(inline.data)
  // TS narrowed Blob to ArrayBufferView<ArrayBuffer>; the Uint8Array is
  // typed ArrayBufferLike. Runtime accepts it — explicit BlobPart cast.
  const blob = new Blob([bytes as BlobPart], { type: mime })
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = objectUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(objectUrl)
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}
