import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { initTheme } from "@/shared/lib/theme"
import { IS_DESKTOP } from "@/shared/lib/platform"

// Apply persisted theme before the React tree mounts so dark-preferring
// users don't see a light flash on first paint.
initTheme()

// The ONLY platform branch. `IS_DESKTOP` is a build-time constant, so Vite
// dead-code-eliminates the unused shell — the web build never bundles the
// desktop shell, and the desktop build never bundles the web-only routes
// (e.g. the heavy force-graph KnowledgePage).
async function boot() {
  const App = IS_DESKTOP
    ? (await import("@/shells/desktop/DesktopApp")).DesktopApp
    : (await import("@/shells/web/WebApp")).WebApp
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void boot()
