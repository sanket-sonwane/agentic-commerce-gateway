import { useCallback, useEffect, useState } from 'react'
import { ReceiptText, ShieldCheck, CreditCard, RotateCcw, Copy, Check, LogOut, LockKeyhole } from 'lucide-react'
import { adminFetch, adminLogin, adminLogout, merchantFetch } from '../api.js'
import PaymentCard from '../components/PaymentCard.jsx'
import AuditTimeline from '../components/AuditTimeline.jsx'
import MerchantAgentSection, { AgentTraceDetail } from '../components/MerchantAgentTrace.jsx'
import { RULE_LABELS } from '../utils/terminology.js'
import { orderTitle } from '../utils/events.js'

const STATUS_VERB = {
  completed: 'Paid', requires_payment: 'Awaiting payment', payment_pending: 'Pending',
  declined: 'Declined', cancelled: 'Cancelled', created: 'Created',
  requires_confirmation: 'Needs confirmation',
}

export default function OpsView({ focusSessionId = null, onSessionObserved }) {
  const [sessions, setSessions] = useState([])
  const [selected, setSelected] = useState(focusSessionId)
  const [opsStatus, setOpsStatus] = useState(null)
  const [integrity, setIntegrity] = useState(null)
  const [tab, setTab] = useState('tx')
  const [agentSessionId, setAgentSessionId] = useState(null)
  // null = checking, true = operator authenticated, false = requires sign-in
  const [authed, setAuthed] = useState(null)
  const [password, setPassword] = useState('')
  const [loginError, setLoginError] = useState(null)
  const [loginBusy, setLoginBusy] = useState(false)

  const refreshSessions = useCallback(async () => {
    try {
      const res = await adminFetch('/admin/ops/status')
      if (res.status === 401) {
        setAuthed(false)
        return
      }
      setAuthed(true)
      setOpsStatus(res.body)
      setSessions(res.body?.recent_sessions || [])
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

  async function onSubmitLogin(e) {
    e.preventDefault()
    setLoginBusy(true)
    setLoginError(null)
    const { ok, body } = await adminLogin(password)
    setLoginBusy(false)
    if (ok) {
      setPassword('')
      setAuthed(true)
      refreshSessions()
    } else {
      setLoginError(body?.detail || 'Sign-in rejected')
      setAuthed(false)
    }
  }

  async function onLogout() {
    await adminLogout()
    setAuthed(false)
    setOpsStatus(null)
    setSessions([])
    setSelected(null)
  }

  async function resetDemo() {
    if (!window.confirm('Reset demo state? All transactions will be cleared and catalog stock restored. The audit trail is preserved — this reset is recorded in the audit log.')) return
    const res = await adminFetch('/admin/demo/reset', { method: 'POST' })
    if (res.status === 401) { setAuthed(false); return }
    setSelected(null)
    setAgentSessionId(null)
    setIntegrity(null)
    refreshSessions()
  }

  if (authed === false) {
    return (
      <div className="empty-state" style={{ maxWidth: 360, margin: '60px auto' }}>
        <div className="ops-section-title"><LockKeyhole size={13} style={{ verticalAlign: -1 }} /> Operator sign-in</div>
        <p className="muted" style={{ marginBottom: 10 }}>
          This console is privileged (stock control, demo reset). Sign in with the operator
          passphrase to continue. It is verified server-side; your browser does not store it.
        </p>
        <form onSubmit={onSubmitLogin} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Operator passphrase"
            autoFocus
            style={{ padding: '8px 10px', borderRadius: 8, border: '1px solid var(--line)' }}
          />
          {loginError && <div className="pay-note" style={{ color: 'var(--red)' }}>⚠ {loginError}</div>}
          <button className="btn-pay" disabled={loginBusy || !password} style={{ width: '100%' }}>
            {loginBusy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    )
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
        <button className="mini-btn full" style={{ marginTop: 8 }} onClick={onLogout}>
          <LogOut size={12} /> Sign out operator
        </button>
      </div>

      {tab === 'agent' && agentSessionId ? (
        <AgentTraceDetail agentSessionId={agentSessionId} />
      ) : selected ? (
        <SessionDetail sessionId={selected} onSessionObserved={onSessionObserved} />
      ) : (
        <div className="empty-state ops-empty">
          Select a transaction to inspect its full audit trail.
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------- session detail */

function SessionDetail({ sessionId, onSessionObserved }) {
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
        <div className="headline">{explanation?.explanation || '\u2026'}</div>
      </div>

      <div className="ops-section-title"><ShieldCheck size={11} style={{ verticalAlign: -1 }} /> Safety checks</div>
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
        <div className="kv muted">No safety checks recorded yet.</div>
      )}

      <div className="ops-section-title"><CreditCard size={11} style={{ verticalAlign: -1 }} /> Payment</div>
      <div className="kv"><b>Payment sent</b> <span className={razorpayCalled ? 'intact-yes' : 'intact-no'}>{razorpayCalled ? 'Yes' : 'No'}</span></div>
      {session?.payment?.saved_card && (
        <div className="kv"><b>saved card</b> {session.payment.saved_card.network} ••••{session.payment.saved_card.last4}</div>
      )}
      {session?.status === 'requires_payment' && session?.payment?.awaiting_authorization && session?.razorpay_order_id && (
        <div className="pay-card-wrapper">
          <PaymentCard
            redirect={{
              checkout_session_id: session.checkout_session_id,
              razorpay_order_id: session.razorpay_order_id,
              key_id: session.payment.key_id,
              customer_id: session.payment.customer_id,
              save_card: !!session.payment.save_card,
              amount: session.totals?.total ?? 0,
              amount_paise: (session.totals?.total ?? 0) * 100,
            }}
            onPaid={(body) => {
              const s = body.checkout_session
              onSessionObserved?.({
                checkout_session_id: s.checkout_session_id,
                status: s.status,
                payment: s.payment,
                amount: s.totals?.total,
                description: orderTitle(s.line_items || []),
                updated_at: Date.now(),
              })
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
        <ShieldCheck size={11} style={{ verticalAlign: -1 }} /> Authorization &amp; Proof
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
