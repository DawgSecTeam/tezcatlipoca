import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import './index.css'
import { initTheme } from './components/ui'
import Comps from './pages/Comps'
import CompLayout from './pages/CompLayout'
import Overview from './pages/Overview'
import Injects from './pages/Injects'
import Packet from './pages/Packet'
import Boxes from './pages/Boxes'
import BoxEdit from './pages/BoxEdit'
import PinPage from './pages/PinPage'

initTheme()

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Comps />} />
        <Route path="/c/:id" element={<CompLayout />}>
          <Route index element={<Overview />} />
          <Route path="injects" element={<Injects />} />
          <Route path="injects/:slug" element={<Injects />} />
          <Route path="packet" element={<Packet />} />
          <Route path="boxes" element={<Boxes />} />
          <Route path="boxes/:box" element={<BoxEdit />} />
          <Route path="boxes/:box/services" element={<PinPage kind="services" />} />
          <Route path="boxes/:box/misconfigs" element={<PinPage kind="misconfigs" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
