/**
 * The product's name for the UI, mirroring agents/adk_cc/branding.py.
 *
 * Build-time via VITE_BRAND_NAME so a white-label build sets one env var.
 * `BRAND` is what a human reads; `BRAND_SLUG` is the identifier form for
 * anywhere a leading dot is illegal (storage keys, CSS classes, ids).
 */
export const BRAND: string =
  (import.meta.env?.VITE_BRAND_NAME as string | undefined)?.trim() || ".jus"

export const BRAND_SLUG: string = BRAND.replace(/^\.+/, "").toLowerCase() || "jus"
