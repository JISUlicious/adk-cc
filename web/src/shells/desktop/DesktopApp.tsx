import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"

import { BackendReady } from "./BackendReady"
import { ProjectRail } from "./ProjectRail"
import { DesktopSettings } from "./DesktopSettings"
import { FileTreeSidePanel } from "./FileTreeSidePanel"
import { ChatPage } from "@/shared/pages/ChatPage"

/**
 * Desktop shell: single-user, no login — just the chat, composing the shared
 * ChatPage with the desktop rail + settings. Any other path redirects to "/"
 * (the web-only routes simply aren't part of this shell).
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
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BackendReady>
    </BrowserRouter>
  )
}
