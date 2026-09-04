import { useEffect, useState } from 'react'
import {
  ShoppingBag, Sun, Moon, X, ShieldCheck, LayoutDashboard,
  ShoppingCart, ReceiptText, History, Lock,
} from 'lucide-react'
import ChatView from './views/ChatView.jsx'
import OpsView from './views/OpsView.jsx'
import CurrentTransactionPanel from './components/CurrentTransactionPanel.jsx'
import TransactionsView from './components/TransactionsView.jsx'
import ConsumerProvenanceDrawer from './components/ConsumerProvenanceDrawer.jsx'
import { checkHealth, merchantFetch } from './api.js'
import { INTEGRITY } from './utils/terminology.js'

export default function App() {
  const [opsOpen, setOpsOpen] = useState(false)
  const [consumerOpen, setConsumerOpen] = useState(false)
  const [consumerSession, setConsumerSession] = useState(null)
  const [focusSession, setFocusSession] = useState(null)
  const [view, setView] = useState('shop')
  const [theme, setTheme] = useState(() => localStorage.getItem('acg-theme') || 'dark')
  const [health, setHealth] = useState({ merchant: null, buyer: null })
  const [integrity, setIntegrity] = useState(null)
  const [orders, setOrders] = useState([])

  // Authoritative consumer Orders (paid purchases + fulfilment), read via the
  // server-side consumer proxy (GET /consumer/orders). This is the source of
  // truth for the Transactions view; nothing here is persisted to localStorage.
  const refreshOrders = async () => {
    try {
      const r = await merchantFetch('/consumer/orders')
      if (!r.ok || !r.body?.orders) return
      const next = r.body.orders
      setOrders(prev => {
        const key = o => `${o.checkout_session_id}::${o.fulfillment_status}::${o.order_status}::${o.amount}`
        const same = prev.length === next.length && prev.every((o, i) => key(o) === key(next[i]))
        return same ? prev : next
      })
    } catch { /* merchant down — leave as-is */ }
  }

  // Keep the authoritative order list fresh: on mount, on a light poll, and
  // whenever a chat/ops event observes a new session.
  useEffect(() => {
    refreshOrders()
    const t = setInterval(refreshOrders, 3000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Active transaction = the most recent authoritative Order (drives the
  // consumer proof panel, which reads its own authoritative endpoints).
  const activeSession = [...orders].sort(
    (a, b) => (new Date(b.created_at || 0) - new Date(a.created_at || 0)),
  )[0]?.checkout_session_id || null

  const onSessionObserved = () => { refreshOrders() }

  useEffect(() => {
    merchantFetch('/audit_logs/verify').then(r => setIntegrity(r.body)).catch(() => {})
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('acg-theme', theme)
  }, [theme])

  useEffect(() => {
    let alive = true
    const tick = async () => {
      const h = await checkHealth()
      if (alive) setHealth(h)
    }
    tick()
    const t = setInterval(tick, 12000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  const openOps = (sessionId = null) => {
    setFocusSession(sessionId)
    setOpsOpen(true)
  }

  useEffect(() => {
    const handler = e => openOps(e.detail || null)
    window.addEventListener('acg:open-ops', handler)
    return () => window.removeEventListener('acg:open-ops', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const handler = e => { setConsumerSession(e.detail || null); setConsumerOpen(true) }
    window.addEventListener('acg:open-consumer', handler)
    return () => window.removeEventListener('acg:open-consumer', handler)
  }, [])

  const openProof = (id) => { setConsumerSession(id); setConsumerOpen(true) }

  const navItems = [
    { key: 'shop', label: 'Shop', icon: ShoppingCart },
    { key: 'txn', label: 'Transactions', icon: History },
  ]

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><ShoppingBag size={16} strokeWidth={2.2} /></div>
          <div>
            <h1>ACG</h1>
            <div className="sub"><span>Agentic Commerce Gateway</span><span>&middot;</span><span>test mode</span></div>
          </div>
        </div>
        <div className="spacer" />
        {integrity && (
          <span className={`integrity-badge ${integrity.intact ? 'yes' : 'no'}`}>
            <ShieldCheck size={11} />
            {integrity.intact ? `${INTEGRITY.verified}` : INTEGRITY.tampered}
          </span>
        )}
        <div className="health" title="Service health (refreshes every 12s)">
          <label><span className={`dot ${health.merchant === true ? 'ok' : health.merchant === false ? 'bad' : ''}`} /> merchant</label>
          <label><span className={`dot ${health.buyer === true ? 'ok' : health.buyer === false ? 'bad' : ''}`} /> agent</label>
        </div>
        <button className="icon-btn" title="Toggle theme" onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}>
          {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <button
          className={`ops-toggle ${opsOpen ? 'on' : ''}`}
          onClick={() => (opsOpen ? setOpsOpen(false) : openOps())}
          title="Merchant Ops & audit trail"
        >
          <LayoutDashboard size={13} /> Ops
        </button>
      </header>

      <div className="app-body">
        <aside className="side-rail">
          <nav className="side-nav" aria-label="Primary">
            {navItems.map(n => (
              <button
                key={n.key}
                className={`side-nav-item ${view === n.key ? 'active' : ''}`}
                onClick={() => setView(n.key)}
              >
                <n.icon size={15} /> {n.label}
              </button>
            ))}
            <button className="side-nav-item" onClick={() => openOps()}>
              <LayoutDashboard size={15} /> Ops
            </button>
          </nav>

          <div className="side-trust">
            <div className="side-trust-title"><Lock size={11} /> You're in control</div>
            <ul>
              <li><span className="ok">✓</span> AI finds &amp; builds the cart</li>
              <li><span className="ok">✓</span> You approve before money moves</li>
              <li><span className="ok">✓</span> Every action is auditable</li>
            </ul>
          </div>
        </aside>

        <main className="center-stage">
          {view === 'shop' ? (
            <ChatView onRequestOps={openOps} onSessionObserved={onSessionObserved} />
          ) : (
            <TransactionsView orders={orders} onOpen={openProof} />
          )}
        </main>

        {activeSession && (
          <CurrentTransactionPanel
            sessionId={activeSession}
            onWhy={openProof}
            onOps={openOps}
          />
        )}
      </div>

      {opsOpen && (
        <>
          <div className="ops-scrim" onClick={() => setOpsOpen(false)} />
          <aside className="ops-panel" role="dialog" aria-label="Merchant operations and audit trail">
            <div className="ops-head">
              <h2><LayoutDashboard size={14} /> Merchant Ops</h2>
              <button className="icon-btn" title="Close" onClick={() => setOpsOpen(false)}>
                <X size={15} />
              </button>
            </div>
            <div className="ops-body">
              <OpsView focusSessionId={focusSession} onSessionObserved={onSessionObserved} />
            </div>
          </aside>
        </>
      )}

      {consumerOpen && consumerSession && (
        <ConsumerProvenanceDrawer
          sessionId={consumerSession}
          onClose={() => setConsumerOpen(false)}
        />
      )}
    </div>
  )
}
