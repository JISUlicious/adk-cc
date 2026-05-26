import { AuthGate } from "@/components/AuthGate"
import { ChatPage } from "@/pages/ChatPage"

function App() {
  return (
    <AuthGate>
      <ChatPage />
    </AuthGate>
  )
}

export default App
