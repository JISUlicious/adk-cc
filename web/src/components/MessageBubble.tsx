import { cn } from "@/lib/utils"
import { Markdown } from "@/lib/markdown"

/**
 * One chat bubble. The user side is right-aligned with the primary
 * surface; the agent side is left-aligned with the muted surface so
 * who-said-what is obvious at a glance.
 *
 * The AGENT side renders its text as GFM markdown (headings, lists,
 * tables, code, links) via the shared renderer — most model output is
 * markdown and showing it raw is hard to read. The USER side stays
 * literal `whitespace-pre-wrap` text: users type plain messages, and
 * rendering their input as markdown would be surprising (plus code-block
 * styling clashes with the primary-colored bubble).
 *
 * Partial bubbles get a subtle pulse so the streaming feel is visible
 * without being noisy.
 */
export function MessageBubble({
  author,
  text,
  isPartial,
}: {
  author: string
  text: string
  isPartial: boolean
}) {
  const isUser = author === "user"
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[80%] rounded-lg px-4 py-2 text-sm",
          isUser
            ? "bg-primary text-primary-foreground whitespace-pre-wrap"
            : "bg-muted text-foreground",
          isPartial && "animate-pulse",
        )}
      >
        {!isUser && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
            {author}
          </div>
        )}
        {isUser ? text : <Markdown>{text}</Markdown>}
      </div>
    </div>
  )
}
