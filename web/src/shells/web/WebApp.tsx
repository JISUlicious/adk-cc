import { BrowserRouter, Routes, Route } from "react-router-dom"

import { AuthGate } from "./AuthGate"
import { ChatPage } from "@/shared/pages/ChatPage"
import { AdminPage } from "@/shared/pages/AdminPage"
import { KnowledgePage } from "@/shared/pages/KnowledgePage"
import { OrgPage } from "@/shared/pages/OrgPage"
import { AccountPage } from "@/shared/pages/AccountPage"
import { AcceptInvitePage } from "@/shared/pages/AcceptInvitePage"
import { ResetPasswordPage } from "@/shared/pages/ResetPasswordPage"

/**
 * Web shell: full multi-user app — login + every route, composing the shared
 * component library. ChatPage uses its default (web) rail + settings.
 */
export function WebApp() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public — you accept an invite / reset a password BEFORE you can
            sign in, so these routes live OUTSIDE the AuthGate. */}
        <Route path="/invite/:token" element={<AcceptInvitePage />} />
        <Route path="/reset-password/:token" element={<ResetPasswordPage />} />
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
                <Route path="/account" element={<AccountPage />} />
              </Routes>
            </AuthGate>
          }
        />
      </Routes>
    </BrowserRouter>
  )
}
