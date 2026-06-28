/** Context-window limits for the fullness gauge (compaction-indicator P2).
 * Mirrors the server's resolved ContextGuard ladder. Empty object when the
 * guard is disabled (no ADK_CC_MAX_CONTEXT_TOKENS). */
import { apiFetch } from "./client"

export interface ContextLimits {
  max_tokens?: number
  reserve?: number
  effective?: number
  warn?: number
  reject?: number
  compaction_threshold?: number | null
}

export async function fetchContextLimits(): Promise<ContextLimits> {
  return apiFetch<ContextLimits>("/api/context/limits")
}
