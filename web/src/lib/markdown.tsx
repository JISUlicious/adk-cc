import * as React from "react"
import { useState } from "react"
import { Check, Copy } from "lucide-react"
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
    // Fenced blocks carry a `language-*` className and are wrapped in
    // <pre> — let CodeBlock (the `pre` renderer) own their chrome, so
    // here we just pass the inner <code> through untouched. Inline code
    // (no language class) gets the muted pill.
    if (className?.startsWith("language-")) {
      return <code className={className} {...props} />
    }
    return (
      <code
        className="rounded bg-muted px-1 py-0.5 text-[0.85em] font-mono"
        {...props}
      />
    )
  },
  pre: (props: React.HTMLAttributes<HTMLPreElement>) => <CodeBlock {...props} />,
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a className="text-primary hover:underline" target="_blank" rel="noreferrer" {...props} />
  ),
  blockquote: (props: React.HTMLAttributes<HTMLQuoteElement>) => (
    <blockquote
      className="border-l-2 border-border pl-3 my-2 text-muted-foreground italic"
      {...props}
    />
  ),
  // Tables: a rounded, fully-ruled frame with a tinted header row and
  // zebra-striped body so dense data is easy to scan. The wrapper scrolls
  // horizontally on narrow widths instead of overflowing the bubble.
  table: (props: React.TableHTMLAttributes<HTMLTableElement>) => (
    // bg-background gives the table its OWN surface (parchment/ivory),
    // distinct from the agent bubble's bg-muted — critical because
    // --border == --muted in this theme, so border lines are invisible
    // ON the bubble but contrast clearly on this lighter surface.
    <div className="my-2 overflow-x-auto rounded-md border border-border bg-background">
      <table className="w-full border-collapse text-xs" {...props} />
    </div>
  ),
  thead: (props: React.HTMLAttributes<HTMLTableSectionElement>) => (
    <thead className="bg-brand-tint" {...props} />
  ),
  tbody: (props: React.HTMLAttributes<HTMLTableSectionElement>) => (
    // Zebra striping via odd-row tint (muted reads against bg-background).
    <tbody className="[&>tr:nth-child(odd)]:bg-muted/50" {...props} />
  ),
  tr: (props: React.HTMLAttributes<HTMLTableRowElement>) => (
    <tr className="border-b border-border last:border-b-0" {...props} />
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th
      className="border-r border-border last:border-r-0 px-2.5 py-1.5 text-left font-semibold text-foreground"
      {...props}
    />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td className="border-r border-border last:border-r-0 px-2.5 py-1.5 align-top" {...props} />
  ),
  hr: () => <hr className="my-3 border-border" />,
  strong: (props: React.HTMLAttributes<HTMLElement>) => (
    <strong className="font-semibold" {...props} />
  ),
}

/**
 * Fenced code block (the `pre` renderer). Wraps the body in a bordered
 * surface with a header strip showing the language label + a copy button.
 * No syntax highlighting (kept dependency-free) — monochrome monospace,
 * but clearly delineated as code. The language + raw text are read off
 * the child <code> that react-markdown nests inside the <pre>.
 */
function CodeBlock(props: React.HTMLAttributes<HTMLPreElement>) {
  const { children } = props
  const { lang, text } = extractCode(children)
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked (insecure context) — ignore */
    }
  }

  return (
    <div className="my-2 overflow-hidden rounded-md border border-border">
      <div className="flex items-center justify-between bg-brand-tint px-2.5 py-1 text-[10px] text-muted-foreground">
        <span className="font-mono uppercase tracking-wider">
          {lang || "code"}
        </span>
        <button
          type="button"
          onClick={copy}
          className="flex items-center gap-1 hover:text-foreground"
          title="Copy code"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" /> copied
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" /> copy
            </>
          )}
        </button>
      </div>
      <pre className="overflow-x-auto bg-background p-3 text-xs font-mono leading-relaxed">
        {children}
      </pre>
    </div>
  )
}

/** Pull the language tag + raw text out of a <pre>'s child <code>. */
function extractCode(children: React.ReactNode): { lang: string; text: string } {
  const child = React.Children.toArray(children)[0]
  if (!React.isValidElement(child)) {
    return { lang: "", text: typeof children === "string" ? children : "" }
  }
  const props = child.props as { className?: string; children?: React.ReactNode }
  const m = /language-([\w+-]+)/.exec(props.className ?? "")
  const lang = m ? m[1] : ""
  const text = nodeText(props.children)
  return { lang, text }
}

/** Flatten a React node tree to its concatenated text (for copy). */
function nodeText(node: React.ReactNode): string {
  if (node == null || node === false) return ""
  if (typeof node === "string" || typeof node === "number") return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join("")
  if (React.isValidElement(node)) {
    return nodeText((node.props as { children?: React.ReactNode }).children)
  }
  return ""
}

/** GFM markdown rendered with the shared component map. */
export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
      {children}
    </ReactMarkdown>
  )
}
