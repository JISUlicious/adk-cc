import type * as React from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

/**
 * Shared GFM markdown renderer + Tailwind component map.
 *
 * Custom renderers (rather than @tailwindcss/typography) keep the bundle
 * lean and let chat bubbles / plan cards share one consistent look:
 * headings, lists, code, tables, links, blockquotes. Used by
 * MessageBubble and PlanCard.
 *
 * Note: react-markdown does NOT render raw HTML embedded in the markdown
 * (no rehype-raw here), so `<script>`/`<img onerror>` in model output are
 * shown as text, not executed — safe by default.
 */
export const MARKDOWN_COMPONENTS = {
  h1: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h1 className="text-base font-semibold mt-3 mb-2 first:mt-0" {...props} />
  ),
  h2: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h2 className="text-sm font-semibold mt-3 mb-1.5 first:mt-0" {...props} />
  ),
  h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
    <h3 className="text-sm font-medium mt-2 mb-1 first:mt-0" {...props} />
  ),
  p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
    <p className="my-2 first:mt-0 last:mb-0" {...props} />
  ),
  ul: (props: React.HTMLAttributes<HTMLUListElement>) => (
    <ul className="list-disc pl-5 my-2 space-y-1" {...props} />
  ),
  ol: (props: React.OlHTMLAttributes<HTMLOListElement>) => (
    <ol className="list-decimal pl-5 my-2 space-y-1" {...props} />
  ),
  li: (props: React.LiHTMLAttributes<HTMLLIElement>) => (
    <li className="leading-relaxed" {...props} />
  ),
  code: (props: React.HTMLAttributes<HTMLElement>) => {
    const { className } = props
    // Block code blocks have language-* className; inline has nothing.
    if (className?.startsWith("language-")) {
      return <code className="text-xs" {...props} />
    }
    return (
      <code
        className="rounded bg-muted px-1 py-0.5 text-[0.85em] font-mono"
        {...props}
      />
    )
  },
  pre: (props: React.HTMLAttributes<HTMLPreElement>) => (
    <pre
      className="rounded bg-muted p-2 my-2 overflow-x-auto text-xs font-mono"
      {...props}
    />
  ),
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a className="text-primary hover:underline" target="_blank" rel="noreferrer" {...props} />
  ),
  blockquote: (props: React.HTMLAttributes<HTMLQuoteElement>) => (
    <blockquote
      className="border-l-2 border-border pl-3 my-2 text-muted-foreground italic"
      {...props}
    />
  ),
  table: (props: React.TableHTMLAttributes<HTMLTableElement>) => (
    <table className="my-2 border-collapse text-xs" {...props} />
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th className="border border-border px-2 py-1 text-left font-medium" {...props} />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td className="border border-border px-2 py-1" {...props} />
  ),
  hr: () => <hr className="my-3 border-border" />,
  strong: (props: React.HTMLAttributes<HTMLElement>) => (
    <strong className="font-semibold" {...props} />
  ),
}

/** GFM markdown rendered with the shared component map. */
export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
      {children}
    </ReactMarkdown>
  )
}
