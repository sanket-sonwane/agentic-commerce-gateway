// API helpers. Local dev: Vite proxies /api/merchant -> :8000 and
// /api/buyer -> :8001. Deployed: absolute URLs come from build-time env.
const MERCHANT_BASE = import.meta.env.VITE_MERCHANT_BASE_URL || '/api/merchant'
const BUYER_BASE = import.meta.env.VITE_BUYER_BASE_URL || '/api/buyer'
// Cross-origin (deployed) calls need credentials to carry the admin session
// cookie; same-origin (/api/* via the Vite proxy) keeps the default behavior.
const CREDENTIALS = import.meta.env.VITE_MERCHANT_BASE_URL ? 'include' : 'same-origin'

export async function merchantFetch(path, options = {}) {
  const res = await fetch(`${MERCHANT_BASE}${path}`, {
    credentials: CREDENTIALS,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const body = await res.json().catch(() => ({}))
  return { ok: res.ok, status: res.status, body }
}

export function adminFetch(path, options = {}) {
  // No privileged value is embedded here. The browser authenticates via the
  // server-signed HttpOnly `acg_admin_session` cookie issued by POST /admin/login;
  // the privileged secret remains server-side only.
  return merchantFetch(path, options)
}

export function adminLogin(password) {
  return merchantFetch('/admin/login', { method: 'POST', body: JSON.stringify({ password }) })
}

export function adminLogout() {
  return merchantFetch('/admin/logout', { method: 'POST' })
}

export function registerUserKey(payload) {
  return merchantFetch('/user_keys', { method: 'POST', body: JSON.stringify(payload) })
}

export async function checkHealth() {
  const probe = async (url) => {
    try {
      const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(4000) })
      return res.ok
    } catch {
      return false
    }
  }
  const [merchant, buyer] = await Promise.all([probe(MERCHANT_BASE), probe(BUYER_BASE)])
  return { merchant, buyer }
}

// POST that streams SSE lines; onEvent called per parsed event.
export async function streamSSE(path, body, onEvent) {
  const res = await fetch(`${BUYER_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try { detail = (await res.json()).detail || detail } catch { /* noop */ }
    onEvent({ type: 'error', content: detail })
    return
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop()
    for (const part of parts) {
      for (const line of part.split('\n')) {
        if (!line.startsWith('data: ')) continue
        const payload = line.slice(6)
        if (!payload || payload === '{}') continue
        try { onEvent(JSON.parse(payload)) } catch { /* ignore malformed */ }
      }
    }
  }
}
