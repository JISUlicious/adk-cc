import { cn } from "@/lib/utils"

/**
 * One chat bubble. The user side is right-aligned with the primary
 * surface; the agent side is left-aligned with the muted surface so
 * who-said-what is obvious at a glance.
 *
 * Partial bubbles get a subtle pulsing border so the streaming feel
 * is visible without being noisy.
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
          "max-w-[80%] rounded-lg px-4 py-2 text-sm whitespace-pre-wrap",
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground",
          isPartial && "animate-pulse",
        )}
      >
        {!isUser && (
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
            {author}
          </div>
        )}
        {text}
      </div>
    </div>
  )
}
