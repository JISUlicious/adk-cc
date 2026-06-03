import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// Vite config — adk-cc chat UI.
//
// Dev: serve at :5173, proxy /run* and /apps/* and /list-apps to the
// running `adk api_server` on :8000 (set ADK_CC_DEV_API to override).
// Prod: built assets are served by FastAPI directly (StaticFiles mount
// in adk_cc/service/server.py); CORS not needed.
//
// envDir points at the REPO ROOT (one level up from web/) so the SAME
// `.env` that configures the adk-cc backend also supplies the frontend's
// VITE_* build-time vars — e.g. VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS.
// Without this, Vite reads .env from web/ only, so a flag set in the root
// .env is silently ignored (baked false) — exactly how the allow-scripts
// toggle got dropped. Vite only exposes VITE_-prefixed vars to client
// code, so the backend's ADK_CC_*/secret vars in the same file are NOT
// leaked into the bundle.
export default defineConfig({
  envDir: path.resolve(__dirname, ".."),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // ADK CLI exposes these top-level paths.
      "/run": _devApi(),
      "/run_sse": _devApi(),
      "/list-apps": _devApi(),
      "/apps": _devApi(),
      "/debug": _devApi(),
      // adk-cc admin endpoints under /admin if mounted.
      "/admin": _devApi(),
      // Our own additions under /api (plans, audit tail, etc.).
      "/api": _devApi(),
    },
  },
})

function _devApi() {
  return {
    target: process.env.ADK_CC_DEV_API ?? "http://127.0.0.1:8000",
    changeOrigin: true,
  }
}
