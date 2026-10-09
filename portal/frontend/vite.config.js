import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Same stack as webui/frontend. `npm run dev` proxies to a portal on 127.0.0.1:8443 — either
// one started locally (`uvicorn` over portal/app.py) or a real engine's through
// `ssh -L 8443:127.0.0.1:8443 <user>@<engine>`.
const portal = 'http://127.0.0.1:8443'
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { target: 'es2022' },
  server: {
    proxy: {
      '/api': portal,
      '/healthz': portal,
      '/console/ws': { target: portal, ws: true },
    },
  },
})
