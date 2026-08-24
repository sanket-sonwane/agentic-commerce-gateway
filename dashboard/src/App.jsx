import { useEffect, useState } from 'react'
import { ShoppingBag, Sun, Moon, X, LayoutDashboard } from 'lucide-react'
import ChatView from './views/ChatView.jsx'
import OpsView from './views/OpsView.jsx'
import { checkHealth } from './api.js'

/**
 * Single-window shell: chat is home; Merchant Ops opens as a slide-over
 * panel (deep-linkable per session via onRequestOps).
 */
export default function App() {
  const [opsOpen, setOpsOpen] = useState(false)
  const [focusSession, setFocusSession] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('acg-theme') || 'dark')
  const [health, setHealth] = useState({ merchant: null, buyer: null })

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

  function openOps(sessionId = null) {
    setFocusSession(sessionId)
    setOpsOpen(true)
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><ShoppingBag size={19} strokeWidth={2.2} /></div>
          <div>
            <h1>Agentic Commerce</h1>
            <div className="sub"><span>ACP v0.1</span><span>·</span><span>Razorpay test mode</span></div>
          </div>
        </div>
        <div className="spacer" />
        <div className="health" title="Service health (refreshes every 12s)">
          <label><span className={`dot ${health.merchant === true ? 'ok' : health.merchant === false ? 'bad' : ''}`} /> merchant</label>
          <label><span className={`dot ${health.buyer === true ? 'ok' : health.buyer === false ? 'bad' : ''}`} /> AI agent</label>
        </div>
        <button className="icon-btn" title="Toggle theme" onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}>
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
        </button>
        <button
          className={`ops-toggle ${opsOpen ? 'on' : ''}`}
          onClick={() => (opsOpen ? setOpsOpen(false) : openOps())}
        >
          <LayoutDashboard size={14} /> Audit trail & ops
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
              <h2><LayoutDashboard size={15} /> Merchant Ops — audit trail</h2>
              <button className="icon-btn" title="Close" onClick={() => setOpsOpen(false)}>
                <X size={16} />
              </button>
            </div>
            <div className="ops-body">
              {/* P4 replaces this with the redesigned OpsPanel */}
              <OpsView focusSessionId={focusSession} />
            </div>
          </aside>
        </>
      )}
    </div>
  )
}
