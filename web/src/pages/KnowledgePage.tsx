import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import ForceGraph2D from "react-force-graph-2d"
import { Button } from "@/components/ui/button"
import {
  fetchWikiGraph,
  fetchWikiPage,
  fetchMemoryGraph,
  fetchMemoryItem,
  type Graph,
  type GraphNode,
  type WikiPage,
  type MemoryItemDetail,
} from "@/api/knowledge"

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
  const [tab, setTab] = useState<Tab>("wiki")
  const [graph, setGraph] = useState<Graph>({ nodes: [], links: [] })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<WikiPage | MemoryItemDetail | null>(null)
  const fgRef = useRef<{ centerAt: (x: number, y: number, ms: number) => void } | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 800, h: 600 })

  // load the graph for the active tab
  useEffect(() => {
    setLoading(true)
    setDetail(null)
    const load = tab === "wiki" ? fetchWikiGraph : fetchMemoryGraph
    load()
      .then((g) => setGraph(g))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [tab])

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
          setDetail({ status: "ok", title: node.label, body: "(inbox note — not yet merged into the shared wiki)" })
          return
        }
        fetchWikiPage(node.id).then(setDetail).catch((e) => setError(String(e)))
      } else {
        const id = node.id.replace(/^(sem|epi):/, "")
        fetchMemoryItem(id).then(setDetail).catch((e) => setError(String(e)))
      }
    },
    [tab],
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
      fetchWikiPage(slug).then(setDetail).catch((e) => setError(String(e)))
    },
    [graph],
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
        <span className="ml-auto text-xs text-muted-foreground">
          {graph.nodes.length} nodes · {graph.links.length} links
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        <div ref={wrapRef} className="relative min-w-0 flex-1 bg-muted/20">
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
          />
        </div>

        <aside className="w-[380px] shrink-0 overflow-y-auto border-l border-border/60 p-4">
          {!detail ? (
            <p className="text-sm text-muted-foreground">
              Click a node to view its content.
            </p>
          ) : (
            <DetailPane detail={detail} tab={tab} onWikiLink={focusSlug} />
          )}
        </aside>
      </div>
    </div>
  )
}

function DetailPane({
  detail,
  tab,
  onWikiLink,
}: {
  detail: WikiPage | MemoryItemDetail
  tab: Tab
  onWikiLink: (slug: string) => void
}) {
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
  return (
    <div className="space-y-2 text-sm">
      <h2 className="text-base font-semibold">
        {p.title}
        {p.contested && <span className="ml-2 text-xs text-amber-600">⚠ contested</span>}
      </h2>
      <WikiBody body={p.body || ""} onWikiLink={onWikiLink} />
      {p.sources && p.sources.length > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">sources: {p.sources.join(", ")}</p>
      )}
    </div>
  )
}

/** Render a wiki body, turning [[slug|alias]] into clickable links that focus
 * the target node. Not full markdown — wikilink-aware preformatted text. */
function WikiBody({ body, onWikiLink }: { body: string; onWikiLink: (slug: string) => void }) {
  const parts = useMemo(() => {
    const out: Array<{ text: string; slug?: string }> = []
    const re = /\[\[([^\]]+)\]\]/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(body)) !== null) {
      if (m.index > last) out.push({ text: body.slice(last, m.index) })
      const target = m[1].split("|")[0].trim()
      const slug = target.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")
      out.push({ text: m[1].split("|").pop()!.trim(), slug })
      last = re.lastIndex
    }
    if (last < body.length) out.push({ text: body.slice(last) })
    return out
  }, [body])

  return (
    <p className="whitespace-pre-wrap">
      {parts.map((seg, i) =>
        seg.slug ? (
          <button
            key={i}
            onClick={() => onWikiLink(seg.slug!)}
            className="text-primary underline underline-offset-2 hover:opacity-80"
          >
            {seg.text}
          </button>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </p>
  )
}
