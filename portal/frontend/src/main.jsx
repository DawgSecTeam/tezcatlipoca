import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import './index.css'
import { initTheme } from './components/ui'
import { ConfirmProvider, ToastProvider } from './components/kit'
import Home from './pages/Home'
import Console from './pages/Console'

initTheme()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
      <ConfirmProvider>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/console/:team/:box" element={<Console />} />
      </Routes>
      </ConfirmProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
