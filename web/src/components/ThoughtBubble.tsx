import { useState } from "react"
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react"

/**
 * Renders a model "thought" part — Gemini's `thought: true` summary
 * content. Collapsed by default; expand to read the full reasoning.
 *
 * When collapsed: chevron + sparkles + first line preview (truncated
 * if long), single row. When expanded: full multi-paragraph thought
 * below an "<author> · thinking" overline.
 *
 * Reflow normalizes the lone-newline fragmentation some providers
 * emit (one part per token + `\n` between deltas). Single newlines
 * disappear; `\n\n` paragraph breaks survive.
 *
 * No italic (kami invariant #10): we cue "secondary content" via
 * opacity + size + a thinking icon instead.
 */
export function ThoughtBubble({
  author,
  text,
}: {
  author: string
  text: string
}) {
  const [open, setOpen] = useState(false)
  const reflowed = reflowThought(text)
  const preview = firstLinePreview(reflowed)

  return (
    <div className="flex justify-start">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="max-w-[80%] flex items-start gap-2 border-l-2 border-border pl-3 pr-2 py-1 text-xs text-muted-foreground/70 hover:text-muted-foreground hover:border-muted-foreground/50 transition-colors text-left w-full"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 mt-0.5 shrink-0 opacity-70" />
        ) : (
          <ChevronRight className="h-3 w-3 mt-0.5 shrink-0 opacity-70" />
        )}
        <Sparkles className="h-3 w-3 mt-0.5 shrink-0 opacity-70" />
        <div className="min-w-0 flex-1">
          {open ? (
            <>
              <div className="text-[10px] uppercase tracking-wider mb-0.5 opacity-60">
                {author} · thinking
              </div>
              <div className="whitespace-pre-wrap">{reflowed}</div>
            </>
          ) : (
            <div className="truncate">
              <span className="text-[10px] uppercase tracking-wider opacity-60 mr-2">
                {author} · thinking
              </span>
              {preview}
            </div>
          )}
        </div>
      </button>
    </div>
  )
}

/** Some providers split a thought into one part per token and stick a
 * lone `\n` between consecutive deltas, producing mid-word fragments
 * like "Tet\nris\n works". This reflow drops lone newlines (preserving
 * real paragraph breaks where the model emitted `\n\n`+), then
 * collapses any double-spaces the cleanup leaves behind. */
function reflowThought(text: string): string {
  return text
    .replace(/(?<!\n)\n(?!\n)/g, "")
    .replace(/[ \t]{2,}/g, " ")
}

/** First line shown in the collapsed state. Uses the first paragraph
 * (split on the `\n\n` boundary that reflowThought preserves);
 * truncates at a word boundary near 100 chars so the row stays on
 * one line at typical widths. */
function firstLinePreview(text: string): string {
  const firstPara = (text.split("\n\n")[0] ?? "").trim()
  if (firstPara.length <= 100) return firstPara
  const slice = firstPara.slice(0, 100)
  const lastSpace = slice.lastIndexOf(" ")
  const cut = lastSpace > 60 ? slice.slice(0, lastSpace) : slice
  return cut + "…"
}
