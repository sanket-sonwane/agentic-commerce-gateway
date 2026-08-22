import { useEffect, useState } from 'react'
import { ShoppingBag, MessageSquareText, LayoutDashboard, Sun, Moon, CheckCircle2, XCircle } from 'lucide-react'
import ChatView from './views/ChatView.jsx'
import OpsView from './views/OpsView.jsx'
import { checkHealth } from './api.js'

export default function App() {
  const [tab, setTab] = useState('chat')
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

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark"><ShoppingBag size={20} strokeWidth={2.2} /></div>
          <div>
            <h1>Agentic Commerce</h1>
            <div className="sub"><span>ACP Reference v0.1</span><span>·</span><span>Razorpay test mode</span></div>
          </div>
        </div>
        <div className="spacer" />
        <div className="health" title="Service health (refreshes every 12s)">
          <label><span className={`dot ${health.merchant === true ? 'ok' : health.merchant === false ? 'bad' : ''}`} /> merchant</label>
          <label><span className={`dot ${health.buyer === true ? 'ok' : health.buyer === false ? 'bad' : ''}`} /> AI agent</label>
        </div>
        <button className="icon-btn" title="Toggle theme" onClick={() => setTheme(t => (t === 'dark' ? 'light' : 'dark'))}>
          {theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}
        </button>
        <div className="segmented">
          <button className={`seg-btn ${tab === 'chat' ? 'active' : ''}`} onClick={() => setTab('chat')}>
            <MessageSquareText size={15} /> Shop
          </button>
          <button className={`seg-btn ${tab === 'ops' ? 'active' : ''}`} onClick={() => setTab('ops')}>
            <LayoutDashboard size={15} /> Merchant Ops
          </button>
        </div>
      </header>
      <div className="main">{tab === 'chat' ? <ChatView /> : <OpsView />}</div>
    </div>
  )
}
