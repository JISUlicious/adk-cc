import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"

import { BackendReady } from "./BackendReady"
import { ProjectRail } from "./ProjectRail"
import { DesktopSettings } from "./DesktopSettings"
import { FileTreeSidePanel } from "./FileTreeSidePanel"
import { ChatPage } from "@/shared/pages/ChatPage"
import { KnowledgePage } from "@/shared/pages/KnowledgePage"

/**
 * Desktop shell: single-user, no login — just the chat, composing the shared
 * ChatPage with the desktop rail + settings, plus the knowledge-graph view
 * (/knowledge, scoped to the current project via ?user=). Any other path
 * redirects to "/".
 */
export function DesktopApp() {
  return (
    <BrowserRouter>
      <BackendReady>
        <Routes>
          <Route
            path="/"
            element={
              <ChatPage
                Rail={ProjectRail}
                Settings={DesktopSettings}
                RightPanel={FileTreeSidePanel}
              />
            }
          />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BackendReady>
    </BrowserRouter>
  )
}
