import * as React from "react"
import { useMemo, useState } from "react"
import { ChevronDown, ChevronUp, ChevronsUpDown } from "lucide-react"
import { cn } from "@/shared/lib/utils"

/**
 * A markdown table rendered as a real table (W6.4).
 *
 * Analysis answers arrive as markdown tables — a 30-row group-by is a wall of
 * text you cannot reorder, whose header scrolls away exactly when you need it,
 * and whose numbers are left-aligned so magnitudes don't line up. This adds the
 * three things that make such a table readable: click-to-sort, a sticky header,
 * and right-aligned numerics.
 *
 * It re-renders the table itself rather than decorating react-markdown's
 * output, because sorting needs the ROWS as a list. Cell CONTENT is passed
 * through untouched (the original React children), so inline code, links and
 * emphasis inside a cell still render — extracting cells to plain strings
 * would have been simpler and would have quietly flattened them.
 *
 * Sorting is display-only and stable: the original order is always one click
 * away (asc → desc → none), because for many tables the agent's chosen order
 * IS the answer.
 */

type Dir = "asc" | "desc" | null

function textOf(node: React.ReactNode): string {
  if (node == null || typeof node === "boolean") return ""
  if (typeof node === "string" || typeof node === "number") return String(node)
  if (Array.isArray(node)) return node.map(textOf).join("")
  if (React.isValidElement(node)) {
    return textOf((node.props as { children?: React.ReactNode }).children)
  }
  return ""
}

/** Numeric value of a cell, or null. Tolerates currency, thousands separators,
 *  percentages and parenthesised negatives — the forms a real report uses. */
function numOf(s: string): number | null {
  const t = s.trim().replace(/[$€£¥₩,\s]/g, "")
  if (!t) return null
  const neg = /^\((.*)\)$/.exec(t)
  const body = neg ? neg[1] : t
  const m = /^-?\d*\.?\d+%?$/.exec(body)
  if (!m) return null
  const n = parseFloat(body.replace("%", ""))
  if (Number.isNaN(n)) return null
  return neg ? -n : n
}

function rowsFrom(section: React.ReactNode): React.ReactElement[] {
  const out: React.ReactElement[] = []
  React.Children.forEach(section, (child) => {
    if (!React.isValidElement(child)) return
    if (child.type === "tr") out.push(child)
    else out.push(...rowsFrom((child.props as { children?: React.ReactNode }).children))
  })
  return out
}

function cellsOf(tr: React.ReactElement): React.ReactNode[] {
  const cells: React.ReactNode[] = []
  React.Children.forEach((tr.props as { children?: React.ReactNode }).children, (c) => {
    if (React.isValidElement(c)) {
      cells.push((c.props as { children?: React.ReactNode }).children)
    }
  })
  return cells
}

/** Rows beyond this get a fixed-height scroll box, so one big table cannot
 *  push the rest of the conversation off screen. */
const TALL_ROWS = 14

export function MarkdownTable({ children }: { children?: React.ReactNode }) {
  const [sort, setSort] = useState<{ col: number; dir: Dir }>({ col: -1, dir: null })

  const { headers, rows } = useMemo(() => {
    let head: React.ReactElement[] = []
    let body: React.ReactElement[] = []
    React.Children.forEach(children, (child) => {
      if (!React.isValidElement(child)) return
      if (child.type === "thead") head = rowsFrom((child.props as { children?: React.ReactNode }).children)
      else if (child.type === "tbody") body = rowsFrom((child.props as { children?: React.ReactNode }).children)
    })
    // A table with no <thead> (rare in GFM, common in hand-written HTML) still
    // renders — the first row just isn't treated as headings.
    return { headers: head.length ? cellsOf(head[0]) : [], rows: body }
  }, [children])

  // A column is numeric when most of its non-empty cells parse as numbers;
  // one stray "n/a" should not left-align a column of revenue.
  const numericCols = useMemo(() => {
    const count = headers.length || (rows[0] ? cellsOf(rows[0]).length : 0)
    return Array.from({ length: count }, (_, i) => {
      let seen = 0
      let numeric = 0
      for (const r of rows) {
        const t = textOf(cellsOf(r)[i]).trim()
        if (!t) continue
        seen++
        if (numOf(t) !== null) numeric++
      }
      return seen > 0 && numeric / seen >= 0.8
    })
  }, [headers.length, rows])

  const sorted = useMemo(() => {
    if (sort.dir === null || sort.col < 0) return rows
    const factor = sort.dir === "asc" ? 1 : -1
    // decorate-sort-undecorate keeps it stable on ties
    return rows
      .map((r, i) => ({ r, i, t: textOf(cellsOf(r)[sort.col]).trim() }))
      .sort((a, b) => {
        const na = numOf(a.t)
        const nb = numOf(b.t)
        let d: number
        if (na !== null && nb !== null) d = na - nb
        else if (a.t === b.t) d = 0
        else if (!a.t) d = 1                     // blanks last, both directions
        else if (!b.t) d = -1
        else d = a.t.localeCompare(b.t, undefined, { numeric: true })
        return d !== 0 ? d * factor : a.i - b.i
      })
      .map((x) => x.r)
  }, [rows, sort])

  function toggle(col: number) {
    setSort((s) =>
      s.col !== col
        ? { col, dir: "asc" }
        : s.dir === "asc"
          ? { col, dir: "desc" }
          : { col: -1, dir: null },   // third click restores the agent's order
    )
  }

  const tall = rows.length > TALL_ROWS

  return (
    <div
      className={cn(
        // min-w-0 matters: without it the table sizes to its content and pushes
        // out of the message column instead of scrolling inside it.
        "my-2 w-full min-w-0 overflow-auto rounded-md border border-border bg-background",
        tall && "max-h-[26rem]",
      )}
    >
      <table className="w-full border-collapse text-xs">
        {headers.length > 0 && (
          <thead className="sticky top-0 z-10 bg-brand-tint">
            <tr className="border-b border-border">
              {headers.map((h, i) => {
                const active = sort.col === i && sort.dir !== null
                const Icon = !active ? ChevronsUpDown : sort.dir === "asc" ? ChevronUp : ChevronDown
                return (
                  <th
                    key={i}
                    className={cn(
                      "border-r border-border last:border-r-0 px-2.5 py-1.5 font-semibold text-foreground",
                      numericCols[i] ? "text-right" : "text-left",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => toggle(i)}
                      title="Sort by this column"
                      className={cn(
                        "inline-flex w-full items-center gap-1 hover:text-primary",
                        numericCols[i] && "justify-end",
                      )}
                    >
                      <span className="truncate">{h}</span>
                      <Icon
                        className={cn(
                          "h-3 w-3 shrink-0",
                          active ? "text-primary" : "text-muted-foreground/50",
                        )}
                      />
                    </button>
                  </th>
                )
              })}
            </tr>
          </thead>
        )}
        <tbody className="[&>tr:nth-child(odd)]:bg-muted/50">
          {sorted.map((r, ri) => (
            <tr key={ri} className="border-b border-border last:border-b-0">
              {cellsOf(r).map((c, ci) => (
                <td
                  key={ci}
                  className={cn(
                    "border-r border-border last:border-r-0 px-2.5 py-1.5 align-top",
                    numericCols[ci] ? "text-right tabular-nums" : "text-left",
                  )}
                >
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
