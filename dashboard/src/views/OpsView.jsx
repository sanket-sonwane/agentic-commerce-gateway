import { useCallback, useEffect, useState } from 'react'
import {
  Search, ReceiptText, PackageCheck, ShieldCheck, ShieldX, PencilLine, CircleCheckBig,
  CreditCard, BadgeCheck, XCircle, Clock, CheckCheck, Ban, UserCog, RotateCcw,
  Hourglass, Save, Copy, Check, CircleHelp, FileSignature, PlayCircle, Dot,
} from 'lucide-react'
import { adminFetch, merchantFetch } from '../api.js'
import RazorpayPayCard from '../components/RazorpayPayCard.jsx'
import MerchantAgentSection, { AgentTraceDetail } from '../components/MerchantAgentTrace.jsx'

const RULE_LABELS = {
  spend_limit: 'Spend limit',
  stock_check: 'Stock',
  category_allowed: 'Category',
  delegation_expiry: 'Delegation',
  quantity_limit: 'Quantity',
}

function actionVisual(ev) {
  const a = ev.action
  if (a.startsWith('discovery')) return { Icon: Search }
  if (a === 'checkout_create_requested' || a === 'checkout_created') return { Icon: ReceiptText }
  if (a.startsWith('reservation') || a === 'inventory_adjusted') return { Icon: PackageCheck }
  if (a === 'gating_evaluated' || a === 'final_validation')
    return { Icon: ev.decision === 'declined' ? ShieldX : ShieldCheck }
  if (a === 'checkout_updated') return { Icon: PencilLine }
  if (a.startsWith('confirmation')) return { Icon: FileSignature }
  if (a === 'complete_requested') return { Icon: PlayCircle }
  if (a === 'razorpay_order_creation' || a === 'razorpay_order_created' || a === 'razorpay_token_charge') return { Icon: CreditCard }
  if (a === 'payment_success') return { Icon: BadgeCheck }
  if (a === 'payment_failure' || a === 'checkout_declined') return { Icon: XCircle }
  if (a === 'payment_pending' || a === 'awaiting_authorization') return { Icon: Hourglass }
  if (a === 'payment_token_saved') return { Icon: Save }
  if (a === 'checkout_completed') return { Icon: CheckCheck }
  if (a === 'checkout_cancelled') return { Icon: Ban }
  if (a === 'demo_reset') return { Icon: RotateCcw }
  if (a === 'inventory_adjusted') return { Icon: UserCog }
  if (ev.decision === 'hold' || ev.decision === 'pending') return { Icon: CircleHelp }
  return { Icon: Dot }
}

export default function OpsView({ focusSessionId = null }) {
  const [sessions, setSessions] = useState([])
  const [selected, setSelected] = useState(focusSessionId)
  const [opsStatus, setOpsStatus] = useState(null)
  const [tab, setTab] = useState('tx')
  const [agentSessionId, setAgentSessionId] = useState(null)

  useEffect(() => {
    if (focusSessionId) {
      setSelected(focusSessionId)
      setTab('tx')
    }
  }, [focusSessionId])

  const refreshSessions = useCallback(async () => {
    try {
      const { body } = await adminFetch('/admin/ops/status')
      setOpsStatus(body)
      setSessions(body?.recent_sessions || [])
    } catch { /* merchant down */ }
  }, [])

  useEffect(() => {
    refreshSessions()
    const t = setInterval(refreshSessions, 2000)
    return () => clearInterval(t)
  }, [refreshSessions])

  return (
    <div className="ops-layout">
      <div className="ops-col">
        <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
          <button className={`mini-btn ${tab === 'tx' ? '' : ''}`} style={{ opacity: tab === 'tx' ? 1 : 0.5 }} onClick={() => { setTab('tx'); setSelected(null) }}>
            Transactions
          </button>
          <button className={`mini-btn ${tab === 'agent' ? '' : ''}`} style={{ opacity: tab === 'agent' ? 1 : 0.5 }} onClick={() => { setTab('agent'); setAgentSessionId(null) }}>
            Agent
          </button>
        </div>

        {tab === 'tx' ? (
          <>
            <h3><ReceiptText size={13} /> Transactions</h3>
            {opsStatus && (
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 12 }}>
                driver <b style={{ color: 'var(--text)' }}>{opsStatus.payment_driver}</b> · razorpay{' '}
                <b className={opsStatus.razorpay_credentials_present ? 'intact-yes' : 'intact-no'}>
                  {String(opsStatus.razorpay_credentials_present)}
                </b>
              </div>
            )}
            {sessions.map(s => (
              <div
                key={s.checkout_session_id}
                className={`session-item ${selected === s.checkout_session_id ? 'selected' : ''}`}
                onClick={() => { setSelected(s.checkout_session_id); setAgentSessionId(null) }}
              >
                <div className="top">
                  <span className="sid">{s.checkout_session_id}</span>
                  <span className="amt">₹{s.total}</span>
                </div>
                <span className={`badge ${s.status}`}>{s.status.replace(/_/g, ' ')}</span>
              </div>
            ))}
            {!sessions.length && <div className="sysline">No transactions yet — place an order in the Shop tab.</div>}
          </>
        ) : (
          <MerchantAgentSection
            selected={agentSessionId}
            onSelect={(id) => { setAgentSessionId(id); setSelected(null) }}
          />
        )}

        <h3><RotateCcw size={13} /> Danger zone</h3>
        <button
          className="mini-btn warn full"
          onClick={async () => {
            await adminFetch('/admin/demo/reset', { method: 'POST' })
            setSelected(null)
            setAgentSessionId(null)
            refreshSessions()
          }}
        >
          <RotateCcw size={13} /> Reset demo state
        </button>
      </div>

      {tab === 'agent' && agentSessionId ? (
        <AgentTraceDetail agentSessionId={agentSessionId} />
      ) : selected ? (
        <SessionDetail sessionId={selected} />
      ) : (
        <div
          className="ops-col"
          style={{ gridColumn: 'span 2', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--muted)' }}
        >
          Select a transaction to inspect its audit trail.
        </div>
      )}
    </div>
  )
}

function SessionDetail({ sessionId }) {
  const [session, setSession] = useState(null)
  const [events, setEvents] = useState([])
  const [explanation, setExplanation] = useState(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let alive = true
    async function load() {
      const s = await merchantFetch(`/checkout_sessions/${sessionId}`)
      if (alive && s.ok) setSession(s.body.checkout_session)
      const a = await merchantFetch(`/audit_logs?checkout_session_id=${sessionId}`)
      if (alive) setEvents(a.body.events || [])
      const x = await merchantFetch(`/explain/${sessionId}`)
      if (alive) setExplanation(x.body)
    }
    load()
    const t = setInterval(load, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [sessionId])

  const gatingEvents = events.filter(e => e.action === 'gating_evaluated' || e.action === 'final_validation')
  const latestGating = gatingEvents.length ? gatingEvents[gatingEvents.length - 1] : null
  const razorpayEvents = events.filter(e =>
    ['razorpay_order_creation', 'razorpay_order_created', 'razorpay_token_charge', 'payment_success', 'payment_failure', 'payment_pending'].includes(e.action),
  )
  const razorpayCalled = razorpayEvents.some(e => e.action === 'razorpay_order_created')

  function copyId() {
    navigator.clipboard?.writeText(sessionId).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <>
      <div className="ops-col">
        <h3>Event timeline</h3>
        <div className="stepper">
          {events.slice().reverse().map(ev => {
            const { Icon } = actionVisual(ev)
            return (
              <div key={ev.seq} className={`step ${ev.decision || ''}`}>
                <span className="step-dot"><Icon size={14} /></span>
                <div className="step-body">
                  <div className="step-title">
                    <span>{ev.action.replace(/_/g, ' ')}</span>
                    <span className="step-time">{new Date(ev.timestamp + 'Z').toLocaleTimeString()}</span>
                  </div>
                  {ev.reasoning_summary && <div className="step-reason">{ev.reasoning_summary}</div>}
                </div>
              </div>
            )
          })}
          {!events.length && <div className="sysline">No events yet.</div>}
        </div>
      </div>

      <div className="ops-col">
        <div className="sess-header">
          <button className="icon-btn" title={sessionId} onClick={copyId} style={{ width: 32, height: 32 }}>
            {copied ? <Check size={14} style={{ color: 'var(--green)' }} /> : <Copy size={14} />}
          </button>
          <span className="mono" style={{ fontWeight: 700, fontSize: 13 }}>{sessionId}</span>
          {session && <span className={`badge ${session.status}`}>{session.status.replace(/_/g, ' ')}</span>}
          {session && <span style={{ marginLeft: 'auto', fontWeight: 800 }}>₹{session.totals?.total}</span>}
        </div>

        <h3>Transaction</h3>
        {session && (
          <div>
            <div className="kv"><b>buyer</b> {session.buyer_agent_id}</div>
            <div className="kv"><b>delegation</b> {session.delegation_id} · v{session.version}</div>
            <div className="kv"><b>reservation</b> {session.reservation?.status}{session.reservation?.expires_at ? ` until ${new Date(session.reservation.expires_at).toLocaleTimeString()}` : ''}</div>
            {(session.line_items || []).map(li => (
              <div className="kv" key={li.pack_id}>• {li.name} ×{li.quantity} @ ₹{li.unit_price}</div>
            ))}
          </div>
        )}

        <h3><ShieldCheck size={13} /> Policy checks</h3>
        {latestGating ? (
          <div className="policy-grid">
            {(latestGating.payload?.checks || []).map(c => (
              <div key={c.rule} className={`policy-row ${c.result}`}>
                <span className="pi">{c.result === 'pass' ? <ShieldCheck size={15} /> : c.result === 'fail' ? <ShieldX size={15} /> : <CircleHelp size={15} />}</span>
                <span className="pn">{RULE_LABELS[c.rule] || c.rule}</span>
                <span className="pd">{c.detail}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="sysline">No policy evaluation yet.</div>
        )}

        <h3><CreditCard size={13} /> Payment</h3>
        <div className="kv"><b>Razorpay invoked</b> <span className={razorpayCalled ? 'intact-yes' : 'intact-no'}>{razorpayCalled ? 'YES' : 'NO'}</span></div>
        {session?.payment?.saved_card && (
          <div className="kv"><b>saved card</b> {session.payment.saved_card.network} ••••{session.payment.saved_card.last4}</div>
        )}
        {session?.status === 'requires_payment' && session?.payment?.awaiting_authorization && session?.razorpay_order_id && (
          <div style={{ margin: '10px 0' }}>
            <RazorpayPayCard
              redirect={{
                checkout_session_id: session.checkout_session_id,
                razorpay_order_id: session.razorpay_order_id,
                key_id: session.payment.key_id,
                customer_id: session.payment.customer_id,
                save_card: !!session.payment.save_card,
                amount: session.totals?.total ?? 0,
                amount_paise: (session.totals?.total ?? 0) * 100,
              }}
            />
          </div>
        )}
      </div>

      <div className="ops-col" style={{ borderRight: 'none' }}>
        <h3><CircleHelp size={13} /> Explanation</h3>
        <div className="explain-box">
          <div className="headline">{explanation?.explanation || '…'}</div>
          <ul>
            {(explanation?.timeline_facts || []).map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
        <IntegrityPanel />
      </div>
    </>
  )
}

function IntegrityPanel() {
  const [result, setResult] = useState(null)
  return (
    <>
      <h3><ShieldCheck size={13} /> Audit integrity</h3>
      <button className="mini-btn" onClick={async () => { const r = await merchantFetch('/audit_logs/verify'); setResult(r.body) }}>
        Verify hash chain
      </button>
      {result && (
        <div className="kv" style={{ marginTop: 8 }}>
          {result.intact
            ? <span className="intact-yes">✓ VERIFIED — chain intact across {result.entries_checked} entries</span>
            : <span className="intact-no">✕ TAMPERED at seq #{result.first_broken_seq}</span>}
        </div>
      )}
    </>
  )
}
