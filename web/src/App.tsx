import { BrowserRouter, Routes, Route } from "react-router-dom"

import { AuthGate } from "@/components/AuthGate"
import { ChatPage } from "@/pages/ChatPage"
import { AdminPage } from "@/pages/AdminPage"

function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/admin" element={<AdminPage />} />
          <Route path="/admin/:tab" element={<AdminPage />} />
        </Routes>
      </AuthGate>
    </BrowserRouter>
  )
}

export default App
