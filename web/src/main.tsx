import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { initTheme } from "@/shared/lib/theme"

// Apply persisted theme before the React tree mounts so dark-preferring
// users don't see a light flash on first paint.
initTheme()

// The ONLY platform branch. We read `import.meta.env.VITE_ADK_CC_DESKTOP`
// DIRECTLY here (not the IS_DESKTOP re-export) so Vite statically replaces it
// and rollup dead-code-eliminates the unused shell's dynamic import: the web
// build never emits the desktop shell, and the desktop build never emits the
// web-only routes (e.g. the heavy force-graph KnowledgePage).
async function boot() {
  const App = import.meta.env.VITE_ADK_CC_DESKTOP === "1"
    ? (await import("@/shells/desktop/DesktopApp")).DesktopApp
    : (await import("@/shells/web/WebApp")).WebApp
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void boot()
