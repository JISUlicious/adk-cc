/**
 * Build-time platform flag. The web and desktop builds share the same
 * component library under `@/shared`; the two app shells under `@/shells`
 * compose it. `main.tsx` is the ONLY place that branches on this — everything
 * else receives platform differences via props/composition, not globals.
 *
 * Set by the desktop Vite build (`VITE_ADK_CC_DESKTOP=1`, see `build:desktop`).
 */
export const IS_DESKTOP = import.meta.env.VITE_ADK_CC_DESKTOP === "1"
