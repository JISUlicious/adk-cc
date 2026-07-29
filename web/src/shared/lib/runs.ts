/**
 * Group a session's outputs by the RUN that produced them (W6.3).
 *
 * A turn that builds a dashboard, an EDA note and two charts currently shows
 * four unrelated chips scattered through its narration, and three turns later
 * there is no way to ask "what did that analysis produce?" short of scrolling
 * the transcript or hunting the file tree.
 *
 * A run is one invocation that produced at least one artifact. Both facts are
 * already in the events the UI loads — `invocation_id` and
 * `actions.artifactDelta` — so this needs no new endpoint, and it cannot drift
 * from what the chat shows because it reads the same source.
 *
 * The run's LABEL is the user message that opened the invocation. That is the
 * part that makes a list of outputs usable: "revenue by region" tells you what
 * `dashboard.html` is; the filename does not.
 */

export type RunOutput = {
  filename: string
  version: number
  /** Event that carried the delta — used as a stable React key. */
  eventId: string
}

export type Run = {
  invocationId: string
  /** The user's message that started this run, trimmed for display. */
  prompt: string
  /** Unix seconds of the first event in the run, when the server sent one. */
  at?: number
  outputs: RunOutput[]
}

type AnyEvent = {
  id?: string
  author?: string
  invocation_id?: string
  invocationId?: string
  timestamp?: number
  actions?: Record<string, unknown>
  content?: { parts?: { text?: string; thought?: boolean }[] }
}

function invocationOf(e: AnyEvent): string {
  return (e.invocation_id ?? e.invocationId ?? "") as string
}

function userText(e: AnyEvent): string {
  return (e.content?.parts ?? [])
    .filter((p) => p.text && !p.thought)
    .map((p) => p.text as string)
    .join(" ")
    .trim()
}

/** Outputs recorded on ONE event, if any. Accepts both wire spellings. */
export function outputsOf(e: AnyEvent): RunOutput[] {
  const actions = (e.actions ?? {}) as Record<string, unknown>
  const delta =
    (actions.artifactDelta as Record<string, unknown> | undefined) ??
    (actions.artifact_delta as Record<string, unknown> | undefined)
  if (!delta || typeof delta !== "object") return []
  const out: RunOutput[] = []
  for (const [filename, ver] of Object.entries(delta)) {
    if (typeof filename !== "string" || !filename) continue
    const version = typeof ver === "number" ? ver : Number(ver)
    if (!Number.isFinite(version)) continue
    out.push({ filename, version, eventId: (e.id as string) ?? "" })
  }
  return out
}

/**
 * Runs in the order they happened, oldest first. Runs that produced nothing are
 * omitted — a "run" the user cannot open is just a turn, and they have the
 * transcript for that.
 *
 * Re-saving the same filename inside one run keeps only the LAST version: a
 * chart rewritten three times is one output, not three, and the newest is the
 * one worth opening.
 */
export function collectRuns(events: AnyEvent[] | undefined | null): Run[] {
  const byInvocation = new Map<string, Run>()
  for (const e of events ?? []) {
    const inv = invocationOf(e)
    if (!inv) continue
    let run = byInvocation.get(inv)
    if (!run) {
      run = { invocationId: inv, prompt: "", outputs: [] }
      byInvocation.set(inv, run)
    }
    if (run.at === undefined && typeof e.timestamp === "number") run.at = e.timestamp
    if (!run.prompt && e.author === "user") run.prompt = userText(e)
    for (const o of outputsOf(e)) {
      const existing = run.outputs.findIndex((x) => x.filename === o.filename)
      if (existing >= 0) run.outputs[existing] = o
      else run.outputs.push(o)
    }
  }
  return [...byInvocation.values()].filter((r) => r.outputs.length > 0)
}

/** Newest first — what a panel wants. */
export function recentRuns(events: AnyEvent[] | undefined | null): Run[] {
  return collectRuns(events).reverse()
}

/** Short label for a run, falling back when the prompt is empty (a resumed or
 *  tool-initiated invocation has no user message of its own). */
export function runLabel(run: Run, max = 60): string {
  const p = run.prompt.replace(/\s+/g, " ").trim()
  if (!p) return `${run.outputs.length} output${run.outputs.length === 1 ? "" : "s"}`
  return p.length > max ? `${p.slice(0, max - 1)}…` : p
}
