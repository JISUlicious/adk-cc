/** File upload → `uploads/<name>` in the session's workspace (#121).
 *
 * One client for both shells: desktop hits `/desktop/uploads/{name}`
 * (project-bound, `userId` IS the project id), web hits
 * `/api/uploads/{name}` (auth principal supplies tenant/user; in no-auth
 * dev the ids ride as query params). Raw body + explicit Content-Type —
 * the repo's upload convention (skills, datasets); no multipart.
 *
 * On a 409 (name taken) the caller can retry with `overwrite`, or use
 * `uploadWithAutoRename` which appends `-2`, `-3`… before the extension —
 * the composer's default, because a modal over a drag-drop is friction.
 */
import { ApiError, apiFetch } from "./client"
import { IS_DESKTOP } from "@/shared/lib/platform"

export interface UploadedFile {
  name: string
  rel_path: string
  bytes: number
}

export class UploadConflict extends Error {}

export async function uploadFile(args: {
  file: Blob
  name: string
  userId: string
  sessionId: string
  overwrite?: boolean
}): Promise<UploadedFile> {
  const q = new URLSearchParams(
    IS_DESKTOP
      ? { project_id: args.userId, session_id: args.sessionId }
      : { user_id: args.userId, session_id: args.sessionId },
  )
  if (args.overwrite) q.set("overwrite", "1")
  const base = IS_DESKTOP ? "/desktop/uploads" : "/api/uploads"
  try {
    const res = await apiFetch<{ upload: UploadedFile }>(
      `${base}/${encodeURIComponent(args.name)}?${q}`,
      {
        method: "PUT",
        body: args.file,
        headers: { "Content-Type": "application/octet-stream" },
      },
    )
    return res.upload
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      throw new UploadConflict(e.message)
    }
    throw e
  }
}

/** `report.csv` → `report-2.csv` → `report-3.csv`… until a free name. */
export async function uploadWithAutoRename(args: {
  file: Blob
  name: string
  userId: string
  sessionId: string
}): Promise<UploadedFile> {
  const dot = args.name.lastIndexOf(".")
  const stem = dot > 0 ? args.name.slice(0, dot) : args.name
  const ext = dot > 0 ? args.name.slice(dot) : ""
  let name = args.name
  for (let i = 2; i <= 20; i++) {
    try {
      return await uploadFile({ ...args, name })
    } catch (e) {
      if (!(e instanceof UploadConflict)) throw e
      name = `${stem}-${i}${ext}`
    }
  }
  // Twenty collisions is not a naming problem anymore.
  return await uploadFile({ ...args, name, overwrite: true })
}

export function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${n} B`
}

/** The plain-text line the model reads — no new part types, no blobs in
 *  session events; the agent uses its normal fs tools on the path. */
export function attachmentLine(u: UploadedFile): string {
  return `[attached file: ${u.rel_path} — ${formatBytes(u.bytes)}]`
}
