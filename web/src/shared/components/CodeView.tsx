import { useMemo } from "react"
import hljs from "@/shared/lib/hljs"

/**
 * Syntax-highlighted code block, shared by the file viewers and markdown fenced
 * blocks. `lang` is a highlight.js language name (e.g. from `langFromPath` or a
 * ```lang fence); "" / unknown → auto-detect over the registered subset.
 *
 * `format` (opt-in) pretty-prints supported languages on view — currently JSON
 * (a minified file renders indented). The file viewers set it; markdown fenced
 * blocks do NOT, so code stays exactly as the author wrote it. Formatting is
 * display-only; the file on disk is untouched, and any parse failure falls back
 * to the raw text so partial/invalid content is never mangled.
 *
 * highlight.js escapes the source and only adds `<span class="hljs-*">` wrappers,
 * so the `dangerouslySetInnerHTML` output is safe. Token colors come from the
 * theme-aware `.hljs-*` palette in index.css — no shipped stylesheet.
 */
export function CodeView({
  code,
  lang,
  className,
  format = false,
}: {
  code: string
  lang?: string
  className?: string
  format?: boolean
}) {
  const html = useMemo(() => {
    let src = code.replace(/\n$/, "") // drop the single trailing newline
    if (format) src = formatForView(src, lang)
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(src, { language: lang, ignoreIllegals: true }).value
      }
      // Unknown language → guess over the registered subset. Viewer content is
      // backend-truncated, so this stays cheap.
      return hljs.highlightAuto(src).value
    } catch {
      return escapeHtml(src)
    }
  }, [code, lang, format])

  return (
    <pre className={className}>
      <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  )
}

/** Pretty-print supported languages for display. JSON → 2-space indent (key
 * order preserved). Any parse failure (partial / JSONL / trailing commas /
 * comments) returns the input unchanged, so nothing is ever mangled. */
function formatForView(code: string, lang?: string): string {
  if (lang === "json") {
    try {
      return JSON.stringify(JSON.parse(code), null, 2)
    } catch {
      return code
    }
  }
  return code
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>]/g, (c) =>
    c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;",
  )
}
