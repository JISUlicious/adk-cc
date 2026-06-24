import { BrowserRouter, Routes, Route } from "react-router-dom"

import { AuthGate } from "@/components/AuthGate"
import { ChatPage } from "@/pages/ChatPage"
import { AdminPage } from "@/pages/AdminPage"
import { KnowledgePage } from "@/pages/KnowledgePage"
import { OrgPage } from "@/pages/OrgPage"
import { AcceptInvitePage } from "@/pages/AcceptInvitePage"

function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public — you accept an invite BEFORE you have an account, so this
            route lives OUTSIDE the AuthGate. */}
        <Route path="/invite/:token" element={<AcceptInvitePage />} />
        <Route
          path="*"
          element={
            <AuthGate>
              <Routes>
                <Route path="/" element={<ChatPage />} />
                <Route path="/admin" element={<AdminPage />} />
                <Route path="/admin/:tab" element={<AdminPage />} />
                <Route path="/knowledge" element={<KnowledgePage />} />
                <Route path="/org" element={<OrgPage />} />
              </Routes>
            </AuthGate>
          }
        />
      </Routes>
    </BrowserRouter>
  )
}

export default App
