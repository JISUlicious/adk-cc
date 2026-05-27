import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { initTheme } from '@/lib/theme'

// Apply persisted theme before the React tree mounts so dark-preferring
// users don't see a light flash on first paint.
initTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
