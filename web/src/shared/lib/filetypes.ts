/**
 * Small filename-based type checks for the file/artifact viewers, so a `.md`
 * renders as markdown and a `.html` renders in the sandboxed preview instead of
 * showing raw source.
 */

/** True for markdown files (`.md`, `.markdown`, `.mdx`). */
export function isMarkdown(name: string, mime?: string): boolean {
  if (mime && /^text\/(markdown|x-markdown)/i.test(mime)) return true
  return /\.(md|markdown|mdx)$/i.test(name)
}

/** True for HTML files (`.html`, `.htm`, `.xhtml`). */
export function isHtml(name: string, mime?: string): boolean {
  if (mime && mime.split(";")[0].trim().toLowerCase() === "text/html") return true
  return /\.x?html?$/i.test(name)
}

/** Map a filename/path to a highlight.js language name (see `lib/hljs.ts` for
 * the registered set), or "" to let the highlighter auto-detect. Extension-based;
 * a few whole-name specials (Dockerfile, Makefile) are handled first. */
const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  py: "python", pyi: "python",
  rs: "rust", go: "go", rb: "ruby",
  java: "java", c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp",
  json: "json", jsonc: "json",
  yml: "yaml", yaml: "yaml",
  toml: "ini", ini: "ini", cfg: "ini", conf: "ini",
  sh: "bash", bash: "bash", zsh: "bash",
  css: "css", scss: "css",
  html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml",
  md: "markdown", markdown: "markdown",
  sql: "sql",
  diff: "diff", patch: "diff",
}

export function langFromPath(name: string): string {
  const base = (name.split("/").pop() || name).toLowerCase()
  if (base === "dockerfile" || base.startsWith("dockerfile.")) return "dockerfile"
  const ext = base.includes(".") ? base.slice(base.lastIndexOf(".") + 1) : ""
  return EXT_LANG[ext] ?? ""
}
