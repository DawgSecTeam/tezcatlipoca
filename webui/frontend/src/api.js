// Thin fetch wrapper: every call throws an Error carrying the server's `detail` message.
async function request(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const d = data?.detail
    const msg = Array.isArray(d) ? d.map((e) => `${(e.loc || []).join('.')}: ${e.msg}`).join('\n') : d
    throw new Error(msg ? String(msg) : `${res.status} ${res.statusText}`)
  }
  return data
}

const enc = encodeURIComponent

export const api = {
  comps: () => request('GET', '/comps'),
  createComp: (body) => request('POST', '/comps', body),
  comp: (id) => request('GET', `/comps/${enc(id)}`),
  putCompfile: (id, items) => request('PUT', `/comps/${enc(id)}/compfile`, { items }),

  addBox: (id, box) => request('POST', `/comps/${enc(id)}/boxes`, box),
  updateBox: (id, name, box) => request('PUT', `/comps/${enc(id)}/boxes/${enc(name)}`, box),
  deleteBox: (id, name) => request('DELETE', `/comps/${enc(id)}/boxes/${enc(name)}`),
  putPins: (id, name, kind, pins) =>
    request('PUT', `/comps/${enc(id)}/boxes/${enc(name)}/${kind}`, pins),

  inject: (id, slug) => request('GET', `/comps/${enc(id)}/injects/${enc(slug)}`),
  putInject: (id, slug, meta, body) =>
    request('PUT', `/comps/${enc(id)}/injects/${enc(slug)}`, { meta, body }),
  deleteInject: (id, slug) => request('DELETE', `/comps/${enc(id)}/injects/${enc(slug)}`),
  packet: (id) => request('GET', `/comps/${enc(id)}/packet`),
  putPacket: (id, body) => request('PUT', `/comps/${enc(id)}/packet`, { body }),

  catalog: (params) => request('GET', `/catalog?${new URLSearchParams(params)}`),
  catalogEntry: (name) => request('GET', `/catalog/${enc(name)}`),
  templates: () => request('GET', '/templates'),
  nodes: () => request('GET', '/nodes'),

  deploy: (id, body) => request('POST', `/comps/${enc(id)}/deploy`, body),
  verify: (id) => request('POST', `/comps/${enc(id)}/verify`),
  jobs: (id) => request('GET', `/comps/${enc(id)}/jobs`),
  job: (jobId, offset) => request('GET', `/jobs/${jobId}?offset=${offset}`),
}

export const platformOf = (template) => (/win/i.test(template || '') ? 'windows' : 'linux')

// What a box IS for rendering: an in-path firewall (unmanaged + in_path — it owns the
// team gateway .1 and terraform wires it between the engine and the team switch), or a
// plain platform. The single classifier for topology, sidebar dots and badges — never
// sniff template or host names (that's how firewalls got drawn as Linux hosts).
export const kindOf = (box) =>
  box?.in_path ? 'firewall' : platformOf(box?.template)

// Box pins are either a bare catalog name or {name, vars, ...}. The UI always works on objects.
export const asPin = (p) => (typeof p === 'string' ? { name: p } : p)
