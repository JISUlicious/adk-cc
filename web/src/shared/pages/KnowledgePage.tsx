import { useCallback, useEffect, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { ArrowLeft, ListChecks, Maximize2 } from "lucide-react"
import ForceGraph2D from "react-force-graph-2d"
import { Button } from "@/shared/components/ui/button"
import { WikiMarkdown } from "@/shared/lib/markdown"
import {
  fetchWikiGraph,
  fetchWikiInbox,
  fetchWikiPage,
  fetchMemoryGraph,
  fetchMemoryItem,
  type Graph,
  type GraphNode,
  type WikiPage,
  type MemoryItemDetail,
} from "@/shared/api/knowledge"
import { maybeAdmin } from "@/shared/api/auth"
import {
  adjudicateWikiClaim,
  deleteWikiPage,
  listWikiReview,
  putWikiPage,
  type WikiReviewItem,
} from "@/shared/api/admin"

/**
 * Knowledge visualizer (Task 1): a force-graph of the shared wiki and the
 * caller's own memory. Selecting a node loads its content in the side panel;
 * a [[wikilink]] in a wiki page selects + focuses that node and loads it.
 */
type Tab = "wiki" | "memory"

const NODE_COLOR: Record<string, string> = {
  domain: "#10b981",   // emerald
  inbox: "#3b82f6",    // blue
  semantic: "#8b5cf6", // violet
  episodic: "#9ca3af", // gray
}

export function KnowledgePage() {
  // Desktop passes the current project as ?user= so memory scopes to it; web
  // omits it (the authenticated principal decides server-side).
  const [params] = useSearchParams()
  const user = params.get("user") || undefined
  const [tab, setTab] = useState<Tab>("wiki")
  const [graph, setGraph] = useState<Graph>({ nodes: [], links: [] })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<WikiPage | MemoryItemDetail | null>(null)
  const fgRef = useRef<{
    centerAt: (x: number, y: number, ms: number) => void
    zoomToFit: (ms?: number, px?: number) => void
  } | null>(null)
  const recenter = useCallback(() => fgRef.current?.zoomToFit(400, 50), [])
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 800, h: 600 })
  // #129 admin curation: review queue + graph refresh after edits/deletes.
  const isAdmin = maybeAdmin()
  const [refreshTick, setRefreshTick] = useState(0)
  const [review, setReview] = useState<WikiReviewItem[] | null>(null)
  const [reviewOpen, setReviewOpen] = useState(false)

  // load the graph for the active tab
  useEffect(() => {
    setLoading(true)
    const load = tab === "wiki" ? fetchWikiGraph : fetchMemoryGraph
    load(user)
      .then((g) => setGraph(g))
      .catch((e) => {
        // A 404 means the SERVER has the knowledge view off (the page ships
        // in both shells regardless) — explain instead of a broken graph.
        const msg = String((e as Error).message ?? e)
        setError(
          /\b404\b/.test(msg)
            ? "The knowledge view is not enabled on this server. Set " +
              "ADK_CC_KNOWLEDGE_UI=1 (with ADK_CC_WIKI=1 and/or " +
              "ADK_CC_MEMORY=1) and restart — see docs/07-wiki-memory.md."
            : msg,
        )
      })
      .finally(() => setLoading(false))
  }, [tab, user, refreshTick])

  // clear the side panel when switching tabs (not on refresh after an edit)
  useEffect(() => setDetail(null), [tab, user])

  // admin: the librarian's pending review queue (silently absent when the
  // admin routes are not mounted on this deployment)
  useEffect(() => {
    if (tab !== "wiki" || !isAdmin) return
    listWikiReview()
      .then((r) => setReview(r.queue))
      .catch(() => setReview(null))
  }, [tab, isAdmin, refreshTick])

  // size the canvas to its container
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const openNode = useCallback(
    (node: GraphNode) => {
      if (tab === "wiki") {
        if (node.kind === "inbox") {
          // Show the actual note content(s), not a stub — "what is in my
          // inbox?" is the question this panel answers before the librarian
          // has run.
          fetchWikiInbox(node.id.replace(/^inbox:/, ""), user)
            .then((r) =>
              setDetail({
                status: "ok",
                title: `${node.label} — inbox (not yet merged)`,
                body: r.notes
                  .map((n) => n.text)
                  .join("\n\n---\n\n"),
              }))
            .catch((e) => setError(String(e)))
          return
        }
        fetchWikiPage(node.id, user).then(setDetail).catch((e) => setError(String(e)))
      } else {
        const id = node.id.replace(/^(sem|epi):/, "")
        fetchMemoryItem(id, user).then(setDetail).catch((e) => setError(String(e)))
      }
    },
    [tab, user],
  )

  // [[wikilink]] click → focus that node + load it
  const focusSlug = useCallback(
    (slug: string) => {
      const node = graph.nodes.find((n) => n.id === slug) as
        | (GraphNode & { x?: number; y?: number })
        | undefined
      if (node && fgRef.current && typeof node.x === "number" && typeof node.y === "number") {
        fgRef.current.centerAt(node.x, node.y, 600)
      }
      fetchWikiPage(slug, user).then(setDetail).catch((e) => setError(String(e)))
    },
    [graph, user],
  )

  const colorOf = (n: GraphNode) =>
    n.kind === "domain" && n.contested ? "#ef4444" : NODE_COLOR[n.kind] || "#9ca3af"

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-border/60 px-4 py-3">
        <Link to="/">
          <Button variant="ghost" size="icon" title="Back to chat">
            <ArrowLeft className="h-5 w-5" />
          </Button>
        </Link>
        <h1 className="text-lg font-semibold">Knowledge graph</h1>
        <div className="ml-4 flex gap-1">
          {(["wiki", "memory"] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`rounded-md px-3 py-1 text-sm capitalize ${
                tab === t ? "bg-accent font-medium" : "text-muted-foreground hover:bg-accent/50"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        {isAdmin && tab === "wiki" && review !== null && (
          <Button
            variant={reviewOpen ? "default" : "outline"}
            size="sm"
            className="ml-2"
            onClick={() => setReviewOpen((v) => !v)}
            title="Claims the librarian held for human adjudication"
          >
            <ListChecks className="mr-1 h-3.5 w-3.5" />
            Review{review.length ? ` (${review.length})` : ""}
          </Button>
        )}
        <span className="ml-auto text-xs text-muted-foreground">
          {graph.nodes.length} nodes · {graph.links.length} links
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        <div ref={wrapRef} className="relative min-w-0 flex-1 bg-muted/20">
          {/* Floating graph control, overlaid bottom-right of the canvas. */}
          {graph.nodes.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={recenter}
              title="Re-center / fit the graph to view"
              className="absolute top-3 right-3 z-10 shadow-sm bg-background/90 backdrop-blur"
            >
              <Maximize2 className="h-3.5 w-3.5" />
              Re-center
            </Button>
          )}
          {loading && (
            <p className="absolute left-3 top-3 text-sm text-muted-foreground">Loading…</p>
          )}
          {error && <p className="absolute left-3 top-3 text-sm text-destructive">{error}</p>}
          {!loading && graph.nodes.length === 0 && (
            <p className="absolute left-3 top-3 text-sm text-muted-foreground">
              No {tab} nodes yet.
            </p>
          )}
          <ForceGraph2D
            ref={fgRef as never}
            width={size.w}
            height={size.h}
            graphData={graph as never}
            nodeLabel="label"
            nodeColor={colorOf as never}
            nodeRelSize={6}
            linkColor={((l: { missing?: boolean }) =>
              l.missing ? "#f59e0b" : "rgba(120,120,120,0.4)") as never}
            linkDirectionalArrowLength={3}
            onNodeClick={openNode as never}
            onEngineStop={recenter as never}
          />
        </div>

        <aside className="w-[380px] shrink-0 overflow-y-auto border-l border-border/60 p-4">
          {reviewOpen && tab === "wiki" ? (
            <ReviewPane
              queue={review ?? []}
              onDone={() => setRefreshTick((t) => t + 1)}
            />
          ) : !detail ? (
            <p className="text-sm text-muted-foreground">
              Click a node to view its content.
            </p>
          ) : (
            <DetailPane
              detail={detail}
              tab={tab}
              onWikiLink={focusSlug}
              admin={isAdmin}
              onSaved={(slug) => {
                setRefreshTick((t) => t + 1)
                fetchWikiPage(slug, user).then(setDetail).catch(() => undefined)
              }}
              onDeleted={() => {
                setDetail(null)
                setRefreshTick((t) => t + 1)
              }}
            />
          )}
        </aside>
      </div>
    </div>
  )
}

function ReviewPane({
  queue,
  onDone,
}: {
  queue: WikiReviewItem[]
  onDone: () => void
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const act = (ch: string, action: "accept" | "reject") => {
    setBusy(ch)
    setErr(null)
    adjudicateWikiClaim(ch, action)
      .then(onDone)
      .catch((e) => setErr(String((e as Error).message ?? e)))
      .finally(() => setBusy(null))
  }
  return (
    <div className="space-y-3 text-sm">
      <h2 className="text-base font-semibold">Review queue</h2>
      <p className="text-xs text-muted-foreground">
        Claims the librarian held for a human decision — contested facts and
        changes to pages you edited by hand. Accepted claims land on the page
        at the next librarian run.
      </p>
      {err && <p className="text-xs text-destructive">{err}</p>}
      {queue.length === 0 && (
        <p className="text-xs text-muted-foreground">Nothing pending.</p>
      )}
      {queue.map((q) => (
        <div
          key={q.claim_hash}
          data-review-item
          className="space-y-1 rounded-md border border-border p-2"
        >
          <p className="text-xs font-medium">{q.slug}</p>
          <p className="whitespace-pre-wrap text-xs">{q.claim}</p>
          <p className="text-[11px] text-muted-foreground">
            {q.classification} · {q.reason} · by {q.user_id}
          </p>
          <div className="flex gap-2 pt-1">
            <Button
              size="sm"
              className="h-6 text-xs"
              disabled={busy === q.claim_hash}
              onClick={() => act(q.claim_hash, "accept")}
            >
              Accept
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-6 text-xs"
              disabled={busy === q.claim_hash}
              onClick={() => act(q.claim_hash, "reject")}
            >
              Reject
            </Button>
          </div>
        </div>
      ))}
    </div>
  )
}

function DetailPane({
  detail,
  tab,
  onWikiLink,
  admin = false,
  onSaved,
  onDeleted,
}: {
  detail: WikiPage | MemoryItemDetail
  tab: Tab
  onWikiLink: (slug: string) => void
  admin?: boolean
  onSaved?: (slug: string) => void
  onDeleted?: () => void
}) {
  // #129 admin curation state (wiki domain pages only)
  const [draft, setDraft] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [paneError, setPaneError] = useState<string | null>(null)
  useEffect(() => {
    setDraft(null)
    setConfirmDelete(false)
    setPaneError(null)
  }, [detail])
  if (tab === "memory") {
    const m = detail as MemoryItemDetail
    if (m.status !== "ok") return <p className="text-sm text-muted-foreground">Not found.</p>
    return (
      <div className="space-y-2 text-sm">
        <h2 className="text-base font-semibold">{m.topic}</h2>
        <p className="text-xs text-muted-foreground">
          {m.memory_type} · {m.item_status}
          {typeof m.confidence === "number" ? ` · confidence ${m.confidence}` : ""}
        </p>
        <p className="whitespace-pre-wrap">{m.text}</p>
        {m.supersedes && m.supersedes.length > 0 && (
          <div className="mt-3">
            <p className="text-xs font-medium text-muted-foreground">Superseded values</p>
            <ul className="list-disc pl-4 text-xs text-muted-foreground">
              {m.supersedes.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    )
  }
  const p = detail as WikiPage
  if (p.status !== "ok") return <p className="text-sm text-muted-foreground">Not found.</p>
  const editable = admin && !!p.slug
  return (
    <div className="space-y-2 text-sm">
      <div className="flex items-start justify-between gap-2">
        <h2 className="text-base font-semibold">
          {p.title}
          {p.contested && <span className="ml-2 text-xs text-amber-600">⚠ contested</span>}
          {!!p.frontmatter?.human_edited && (
            <span className="ml-2 text-xs text-emerald-600"
                  title="Hand-edited — the librarian holds conflicting notes for review">
              ✎ curated
            </span>
          )}
        </h2>
        {editable && draft === null && (
          <div className="flex shrink-0 gap-1">
            <Button size="sm" variant="outline" className="h-6 text-xs"
                    onClick={() => setDraft(p.body || "")}>
              Edit
            </Button>
            <Button size="sm" variant={confirmDelete ? "destructive" : "outline"}
                    className="h-6 text-xs"
                    disabled={busy}
                    onClick={() => {
                      if (!confirmDelete) { setConfirmDelete(true); return }
                      setBusy(true)
                      deleteWikiPage(p.slug!)
                        .then(() => onDeleted?.())
                        .catch((e) => setPaneError(String((e as Error).message ?? e)))
                        .finally(() => setBusy(false))
                    }}>
              {confirmDelete ? "Really delete" : "Delete"}
            </Button>
          </div>
        )}
        {editable && draft !== null && (
          <div className="flex shrink-0 gap-1">
            <Button size="sm" variant="outline" className="h-6 text-xs"
                    disabled={busy} onClick={() => setDraft(null)}>
              Cancel
            </Button>
            <Button size="sm" className="h-6 text-xs" disabled={busy}
                    onClick={() => {
                      setBusy(true)
                      putWikiPage(p.slug!, draft)
                        .then(() => onSaved?.(p.slug!))
                        .catch((e) => setPaneError(String((e as Error).message ?? e)))
                        .finally(() => setBusy(false))
                    }}>
              {busy ? "Saving…" : "Save"}
            </Button>
          </div>
        )}
      </div>
      {paneError && <p className="text-xs text-destructive">{paneError}</p>}
      {draft !== null && (
        <textarea
          data-wiki-page-edit
          className="h-72 w-full resize-y rounded border border-border bg-background p-2 font-mono text-xs"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          autoFocus
        />
      )}
      {(() => {
        const ty = p.frontmatter?.type as string | undefined
        const tags = p.frontmatter?.tags as string[] | undefined
        if (!ty && !(tags && tags.length)) return null
        return (
          <p className="text-xs text-muted-foreground">
            {ty || ""}
            {tags && tags.length ? (ty ? " · " : "") + tags.join(", ") : ""}
          </p>
        )
      })()}
      {draft === null && (
        <div className="leading-relaxed [&_p]:my-1.5">
          <WikiMarkdown onWikiLink={onWikiLink}>{p.body || ""}</WikiMarkdown>
        </div>
      )}
      {p.sources && p.sources.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">sources: {p.sources.join(", ")}</p>
      )}
    </div>
  )
}
