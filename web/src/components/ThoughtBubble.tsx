import { Sparkles } from "lucide-react"

/**
 * Renders a model "thought" part — Gemini's `thought: true` summary
 * content. Shown faded + smaller + indented behind a subtle left bar
 * so the reader can tell at a glance this is the agent's internal
 * reasoning, not part of the user-facing reply.
 *
 * No italic (kami invariant #10): we cue "secondary content" via
 * opacity + size + a thinking icon instead.
 *
 * Partials are dropped upstream (`dedupePartials` filters thought
 * deltas to keep the streaming bubble clean) — thoughts pop in
 * once consolidated, not character-by-character.
 */
export function ThoughtBubble({
  author,
  text,
}: {
  author: string
  text: string
}) {
  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] flex items-start gap-2 border-l-2 border-border pl-3 py-1 text-xs text-muted-foreground/70 whitespace-pre-wrap">
        <Sparkles className="h-3 w-3 mt-0.5 shrink-0 opacity-70" />
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-wider mb-0.5 opacity-60">
            {author} · thinking
          </div>
          {reflowThought(text)}
        </div>
      </div>
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
