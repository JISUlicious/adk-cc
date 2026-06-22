import { BrowserRouter, Routes, Route } from "react-router-dom"

import { AuthGate } from "@/components/AuthGate"
import { ChatPage } from "@/pages/ChatPage"
import { AdminPage } from "@/pages/AdminPage"
import { KnowledgePage } from "@/pages/KnowledgePage"

function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/admin/:tab" element={<AdminPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
        </Routes>
      </AuthGate>
    </BrowserRouter>
  )
}

export default App
