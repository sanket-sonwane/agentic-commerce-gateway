import { useMemo } from 'react'
import { ShoppingBag } from 'lucide-react'
import { TRXN_STATUS } from '../utils/terminology.js'

/**
 * TRANSACTIONS — consumer-friendly history.
 *
 * NOTE (MISSING AUTHORITATIVE FIELD): there is no consumer-facing transaction
 * LIST endpoint in the backend (the only list, /admin/ops/status, requires the
 * operator and is merchant-scoped). This view therefore composes entries from
 * the sessions the current chat observed (passed down from App) and from
 * sessions persisted to localStorage. Each row is authoritative per-session:
 * it opens the consumer Money Story / purchase proof which reads the real
 * /consumer/transactions/{id}/provenance projection.
 */
const LS_KEY = 'acg.known_sessions'

export function loadPersisted() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') } catch { return {} }
}
function persist(map) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(map)) } catch { /* ignore */ }
}

export function mergeSessions(prev, next) {
  const out = { ...(prev || {}) }
  let changed = false
  for (const [id, meta] of Object.entries(next || {})) {
    const prevMeta = out[id]
    if (!prevMeta || (meta.status && prevMeta.status !== meta.status) || (meta.amount != null && prevMeta.amount == null)) {
      out[id] = { ...(prevMeta || {}), ...meta, updated_at: meta.updated_at || prevMeta?.updated_at || Date.now() }
      changed = true
    }
  }
  if (changed) persist(out)
  return out
}

// Called by the provenance drawer when a receipt turns out to be from before
// the last merchant restart (free-tier ephemeral DB): dim the row, keep the
// friendly explanation one click away.
export function markSessionGone(id) {
  try {
    const map = loadPersisted()
    if (map[id] && !map[id].pre_restart) {
      map[id] = { ...map[id], pre_restart: true }
      persist(map)
    }
  } catch { /* ignore */ }
}

export default function TransactionsView({ sessions, onOpen }) {
  const list = useMemo(() => {
    return Object.entries(sessions || {})
      .map(([id, m]) => ({ id, ...m }))
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  }, [sessions])

  return (
    <div className="txns-view">
      <div className="txns-head">
        <h2>Transactions</h2>
        <span className="muted" style={{ fontSize: 11 }}>Consumer view of your purchases</span>
      </div>

      {list.length === 0 ? (
        <div className="txns-empty">
          <ShoppingBag size={22} />
          <p>No purchases yet.</p>
          <span className="muted">Go to Shop and let your agent build an order.</span>
        </div>
      ) : (
        <div className="txns-list">
          {list.map(t => (
            <button
              key={t.id}
              className="txn-row"
              onClick={() => onOpen?.(t.id)}
              style={t.pre_restart ? { opacity: 0.55 } : undefined}
              title={t.pre_restart ? 'Receipt predates the last service restart — data resets on free hosting' : undefined}
            >
              <span className={`txn-row-status ${t.status || ''}`}>
                {TRXN_STATUS[t.status] || (t.status ? t.status.replace(/_/g, ' ') : '—')}
              </span>
              <span className="txn-row-desc">
                {t.description || 'Grocery order'}{t.pre_restart ? ' · pre-restart' : ''}
              </span>
              <span className="txn-row-amt">₹{t.amount != null ? t.amount : '—'}</span>
              {t.updated_at && <span className="txn-row-time">{new Date(t.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
