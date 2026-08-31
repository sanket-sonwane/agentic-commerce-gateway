import { useEffect, useState } from 'react'
import { ShoppingBag, Sun, Moon, X, LayoutDashboard, ShieldCheck } from 'lucide-react'
import ChatView from './views/ChatView.jsx'
import OpsView from './views/OpsView.jsx'
import ConsumerProvenanceDrawer from './components/ConsumerProvenanceDrawer.jsx'
import { checkHealth, merchantFetch } from './api.js'

export default function App() {
  const [opsOpen, setOpsOpen] = useState(false)
  const [consumerOpen, setConsumerOpen] = useState(false)
  const [consumerSession, setConsumerSession] = useState(null)
  const [focusSession, setFocusSession] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('acg-theme') || 'dark')
  const [health, setHealth] = useState({ merchant: null, buyer: null })
  const [integrity, setIntegrity] = useState(null)

  useEffect(() => {
    if (opsOpen && !integrity) {
      merchantFetch('/audit_logs/verify').then(r => setIntegrity(r.body)).catch(() => {})
    }
  }, [opsOpen, integrity])

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

  function openOps(sessionId = null) {
    setFocusSession(sessionId)
    setOpsOpen(true)
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><ShoppingBag size={16} strokeWidth={2.2} /></div>
          <div>
            <h1>ACG</h1>
            <div className="sub"><span>v0.1</span><span>&middot;</span><span>Razorpay test</span></div>
          </div>
        </div>
        <div className="spacer" />
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

      <div className="main">
        <ChatView onRequestOps={openOps} />
      </div>

      {opsOpen && (
        <>
          <div className="ops-scrim" onClick={() => setOpsOpen(false)} />
          <aside className="ops-panel" role="dialog" aria-label="Merchant operations and audit trail">
            <div className="ops-head">
              <h2><LayoutDashboard size={14} /> Merchant Ops</h2>
              {integrity && (
                <span className={`integrity-badge ${integrity.intact ? 'yes' : 'no'}`}>
                  <ShieldCheck size={11} />
                  {integrity.intact ? `chain verified ${integrity.entries_checked}` : `tampered @${integrity.first_broken_seq}`}
                </span>
              )}
              <button className="icon-btn" title="Close" onClick={() => setOpsOpen(false)}>
                <X size={15} />
              </button>
            </div>
            <div className="ops-body">
              <OpsView focusSessionId={focusSession} />
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
