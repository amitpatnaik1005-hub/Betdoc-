import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { startPwa } from './lib/pwa'

// Group 109: the Pocket Twin PWA. The service worker registers in production builds only (import.meta.env.PROD), and the
// install prompt is kept from the moment the browser offers it
startPwa()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
