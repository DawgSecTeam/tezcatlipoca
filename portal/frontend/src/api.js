// Thin fetch wrapper, same contract as webui/frontend/src/api.js: every call throws an Error
// carrying the server's `detail` message, plus the HTTP status (the portal's 401/403/423 mean
// different things to the UI).
async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const err = new Error(data?.detail ? String(data.detail) : `${res.status} ${res.statusText}`)
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  health: () => request('GET', '/healthz'),
  me: () => request('GET', '/api/me'),
  login: (username, password, display) => request('POST', '/api/login', { username, password, display }),
  logout: () => request('POST', '/api/logout', {}),
  console: (team, box) => request('POST', '/api/console', { team, box }),
  access: () => request('GET', '/api/admin/access'),
  setAccess: (open) => request('POST', '/api/admin/access', { open }),
  log: () => request('GET', '/api/admin/log'),
}

// What a box IS for rendering — the same three kinds webui's kindOf draws (firewall wins).
export const kindOf = (box) => (box?.firewall ? 'firewall' : box?.os === 'windows' ? 'windows' : 'linux')

export const consolePath = (team, box) => `/console/${encodeURIComponent(team)}/${encodeURIComponent(box)}`
