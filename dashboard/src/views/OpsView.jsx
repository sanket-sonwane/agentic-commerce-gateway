import { useCallback, useEffect, useState } from 'react'
import { ReceiptText, ShieldCheck, CreditCard, RotateCcw, Copy, Check } from 'lucide-react'
import { adminFetch, merchantFetch } from '../api.js'
import RazorpayPayCard from '../components/RazorpayPayCard.jsx'
import AuditTimeline from '../components/AuditTimeline.jsx'
import MerchantAgentSection, { AgentTraceDetail } from '../components/MerchantAgentTrace.jsx'

const RULE_LABELS = {
  spend_limit: 'Spend limit',
  stock_check: 'Stock',
  category_allowed: 'Category',
  delegation_expiry: 'Delegation',
  quantity_limit: 'Quantity',
}

const STATUS_VERB = {
  completed: 'Paid', requires_payment: 'Awaiting payment', payment_pending: 'Pending',
  declined: 'Declined', cancelled: 'Cancelled', created: 'Created',
  requires_confirmation: 'Needs confirmation',
}

export default function OpsView({ focusSessionId = null }) {
  const [sessions, setSessions] = useState([])
  const [selected, setSelected] = useState(focusSessionId)
  const [opsStatus, setOpsStatus] = useState(null)
  const [integrity, setIntegrity] = useState(null)
  const [tab, setTab] = useState('tx')
  const [agentSessionId, setAgentSessionId] = useState(null)

  const refreshSessions = useCallback(async () => {
    try {
      const { body } = await adminFetch('/admin/ops/status')
      setOpsStatus(body)
      setSessions(body?.recent_sessions || [])
    } catch { /* merchant down */ }
  }, [])

  useEffect(() => {
    if (focusSessionId) { setSelected(focusSessionId); setTab('tx') }
  }, [focusSessionId])

  useEffect(() => {
    refreshSessions()
    const t = setInterval(refreshSessions, 2500)
    return () => clearInterval(t)
  }, [refreshSessions])

  useEffect(() => {
    merchantFetch('/audit_logs/verify').then(r => setIntegrity(r.body)).catch(() => {})
  }, [])

  async function resetDemo() {
    if (!window.confirm('Reset demo state? All transactions and audit events will be cleared.')) return
    await adminFetch('/admin/demo/reset', { method: 'POST' })
    setSelected(null)
    setAgentSessionId(null)
    setIntegrity(null)
    refreshSessions()
  }

  return (
    <div className="ops-grid">
      <div>
        <div className="stepper-tabs">
          <button className={`tab-btn ${tab === 'tx' ? 'active' : ''}`} onClick={() => { setTab('tx'); setSelected(null) }}>
            <ReceiptText size={12} /> Transactions
          </button>
          <button className={`tab-btn ${tab === 'agent' ? 'active' : ''}`} onClick={() => { setTab('agent'); setAgentSessionId(null) }}>
            Agent traces
          </button>
        </div>

        {tab === 'tx' ? (
          <>
            <div className="ops-section-title" style={{ marginTop: 2 }}>Transactions</div>
            {opsStatus && (
              <div className="kv" style={{ marginBottom: 8 }}>
                driver <b>{opsStatus.payment_driver}</b> &middot; razorpay{' '}
                <span className={opsStatus.razorpay_credentials_present ? 'intact-yes' : 'intact-no'}>
                  {opsStatus.razorpay_credentials_present ? 'connected' : 'no credentials'}
                </span>
              </div>
            )}
            {sessions.map(s => (
              <div
                key={s.checkout_session_id}
                className={`session-item ${selected === s.checkout_session_id ? 'selected' : ''}`}
                onClick={() => { setSelected(s.checkout_session_id); setAgentSessionId(null) }}
              >
                <div className="top">
                  <span>{STATUS_VERB[s.status] || s.status}</span>
                  <span>₹{s.total ?? '—'}</span>
                </div>
                <div className="sub">
                  {new Date(s.updated_at).toLocaleTimeString()} · {s.buyer_agent_id.replace('buyer_agent_', '')}
                </div>
              </div>
            ))}
            {!sessions.length && <div className="muted">No transactions yet — place an order in chat.</div>}
          </>
        ) : (
          <MerchantAgentSection
            selected={agentSessionId}
            onSelect={(id) => { setAgentSessionId(id); setSelected(null) }}
          />
        )}

        <div className="ops-section-title">Danger zone</div>
        <button className="mini-btn warn full" onClick={resetDemo}>
          <RotateCcw size={12} /> Reset demo state
        </button>
      </div>

      {tab === 'agent' && agentSessionId ? (
        <AgentTraceDetail agentSessionId={agentSessionId} />
      ) : selected ? (
        <SessionDetail sessionId={selected} />
      ) : (
        <div className="empty-state">
          Select a transaction to inspect its full audit trail.
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------- session detail */

function SessionDetail({ sessionId }) {
  const [session, setSession] = useState(null)
  const [events, setEvents] = useState([])
  const [explanation, setExplanation] = useState(null)
  const [provenance, setProvenance] = useState(null)
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
      const p = await merchantFetch(`/provenance/${sessionId}`)
      if (alive && p.ok) setProvenance(p.body)
    }
    load()
    const t = setInterval(load, 1500)
    return () => { alive = false; clearInterval(t) }
  }, [sessionId])

  const gatingEvents = events.filter(e => e.action === 'gating_evaluated' || e.action === 'final_validation')
  const latestGating = gatingEvents.length ? gatingEvents[gatingEvents.length - 1] : null
  const razorpayCalled = events.some(e =>
    e.action === 'razorpay_order_created' || e.action === 'razorpay_token_charge')

  function copyId() {
    navigator.clipboard?.writeText(sessionId).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <>
      <div className="sess-header">
        <button className="icon-btn" title={sessionId} onClick={copyId}>
          {copied ? <Check size={13} className="intact-yes" /> : <Copy size={13} />}
        </button>
        <span className="mono" style={{ fontWeight: 700, fontSize: 12 }}>{sessionId}</span>
        {session && <span className={`badge ${session.status}`}>{session.status.replace(/_/g, ' ')}</span>}
        {session && <span className="flex-spacer" style={{ fontWeight: 800 }}>₹{session.totals?.total}</span>}
      </div>

      <div className="ops-section-title">Plain answer</div>
      <div className="explain-box">
        <div className="headline">{explanation?.explanation || '…'}</div>
        {!!explanation?.timeline_facts?.length && (
          <ul style={{ margin: '6px 0 0', paddingLeft: 17 }}>
            {explanation.timeline_facts.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        )}
      </div>

      <div className="ops-section-title"><ShieldCheck size={11} style={{ verticalAlign: -1 }} /> Policy checks</div>
      {latestGating ? (
        <div className="policy-grid">
          {(latestGating.payload?.checks || []).map(c => (
            <div key={c.rule} className={`policy-row ${c.result}`}>
              <span className="pi">{c.result === 'pass' ? '✓' : c.result === 'fail' ? '✕' : '?'}</span>
              <span className="pn">{RULE_LABELS[c.rule] || c.rule}</span>
              <span className="pd">{c.detail}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="kv muted">No policy evaluation yet.</div>
      )}

      <div className="ops-section-title"><CreditCard size={11} style={{ verticalAlign: -1 }} /> Payment</div>
      <div className="kv"><b>Razorpay invoked</b> <span className={razorpayCalled ? 'intact-yes' : 'intact-no'}>{razorpayCalled ? 'YES' : 'NO'}</span></div>
      {session?.payment?.saved_card && (
        <div className="kv"><b>saved card</b> {session.payment.saved_card.network} ••••{session.payment.saved_card.last4}</div>
      )}
      {session?.status === 'requires_payment' && session?.payment?.awaiting_authorization && session?.razorpay_order_id && (
        <div className="pay-card-wrapper">
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

      <ProvenancePanel provenance={provenance} />

      <div className="ops-section-title">Audit timeline</div>
      <AuditTimeline events={events} stepper />
    </>
  )
}

/* ------------------------------------------------- signed authorization & proof */

function ProvenancePanel({ provenance }) {
  const [open, setOpen] = useState(false)
  if (!provenance) return null
  const passCount = (provenance.checks || []).filter(c => c.ok).length
  const total = (provenance.checks || []).length
  return (
    <>
      <div className="ops-section-title">
        <ShieldCheck size={11} style={{ verticalAlign: -1 }} /> Signed Authorization &amp; Proof
        <span style={{ marginLeft: 'auto', fontSize: 10 }}>
          {provenance.provenance_available
            ? <span className={passCount === total && total > 0 ? 'intact-yes' : 'intact-no'}>
                {passCount}/{total} verified
              </span>
            : <span className="intact-no">NOT_AVAILABLE</span>}
        </span>
      </div>
      <div className="explain-box">
        {provenance.provenance_available ? (
          <>
            <div className="headline">{provenance.summary}</div>
            <ul style={{ margin: '6px 0 0', paddingLeft: 17 }}>
              {(provenance.checks || []).map((c, i) => (
                <li key={i}>
                  <span className={c.ok ? 'intact-yes' : 'intact-no'}>
                    {c.ok ? '✓' : '✕'} {c.name}
                  </span>{' '}
                  <span className="muted">— {c.detail}</span>
                </li>
              ))}
            </ul>
            <button className="mini-btn" style={{ marginTop: 8 }} onClick={() => setOpen(o => !o)}>
              {open ? 'Hide' : 'Show'} artifact hashes
            </button>
            {open && (
              <div className="kv" style={{ marginTop: 6 }}>
                {provenance.artifacts?.execution_mandate && (
                  <>
                    <div><b>execution mandate</b> {provenance.artifacts.execution_mandate.mandate_id}</div>
                    <div><b>authorized amount</b> ₹{(provenance.artifacts.execution_mandate.total_paise / 100).toFixed(2)}</div>
                  </>
                )}
                {provenance.artifacts?.decision_receipt && (
                  <div><b>decision receipt</b> {provenance.artifacts.decision_receipt.receipt_id}</div>
                )}
                <div><b>merchant key</b> <span className="mono">{provenance.artifacts?.merchant_key_id}</span></div>
              </div>
            )}
          </>
        ) : (
          <div className="muted">{provenance.summary}</div>
        )}
      </div>
    </>
  )
}
