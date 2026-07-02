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
