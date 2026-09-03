import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { merchantFetch } from '../api.js'
import { ACTION_META, metaFor, deriveStages } from '../utils/actionMeta.js'
import { RULE_LABELS } from '../utils/terminology.js'

/**
 * MONEY STORY — why money moved / didn't move.
 * Composed client-side from existing endpoints:
 *   /audit_logs?checkout_session_id=  ·  /checkout_sessions/{id}  ·  /explain/{id}
 */

function latestChecks(events = []) {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]
    if ((e.action === 'gating_evaluated' || e.action === 'final_validation') && e.payload?.checks?.length) {
      return e.payload.checks
    }
  }
  return null
}

export default function MoneyStoryDrawer({ sessionId, onClose }) {
  const [explanation, setExplanation] = useState(null)
  const [session, setSession] = useState(null)
  const [events, setEvents] = useState([])

  useEffect(() => {
    let alive = true
    async function load() {
      const [x, s, a] = await Promise.all([
        merchantFetch(`/explain/${sessionId}`),
        merchantFetch(`/checkout_sessions/${sessionId}`),
        merchantFetch(`/audit_logs?checkout_session_id=${sessionId}`),
      ])
      if (!alive) return
      if (x.ok) setExplanation(x.body)
      if (s.ok) setSession(s.body.checkout_session)
      if (a.ok) setEvents(a.body.events || [])
    }
    load()
    const onKey = e => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => { alive = false; window.removeEventListener('keydown', onKey) }
  }, [sessionId, onClose])

  const checks = latestChecks(events) || session?.gating_result?.checks || null
  const agentSteps = events.filter(e => e.actor === 'merchant_agent' && e.action !== 'agent_reasoning_only')
  const proofEvents = events.filter(e => e.actor === 'razorpay')
  const orderEvent = events.find(e => e.action === 'razorpay_order_created')
  const captured = events.some(e => e.action === 'payment_success')
  const stages = deriveStages(events)

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="explain-drawer" role="dialog" aria-label="Money story">
        <div className="drawer-head">
          <h3>Money story</h3>
          <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>{sessionId}</span>
          <button className="icon-btn" onClick={onClose} title="Close"><X size={15} /></button>
        </div>
        <div className="drawer-body">
          {/* verdict stepper */}
          <div className="story-stepper">
            {stages.map(s => (
              <div key={s.key} className={`story-stage ${s.done ? 'done' : ''} ${s.outcome ? `outcome ${s.label.toLowerCase()}` : ''}`}>
                <span className="stage-dot" />
                <span className="stage-label">{s.label}</span>
              </div>
            ))}
          </div>

          {/* plain verdict */}
          <div className="drawer-answer">{explanation?.explanation || 'Reading the audit trail…'}</div>

          {/* amount ladder */}
          {session && (
            <>
              <SectionTitle>Every rupee</SectionTitle>
              <div className="ladder">
                {(session.line_items || []).map(li => (
                  <div className="ladder-row" key={li.pack_id}>
                    <span>{li.name}{li.quantity > 1 ? ` ×${li.quantity}` : ''}</span>
                    <b>₹{li.unit_price * li.quantity}</b>
                  </div>
                ))}
                <div className="ladder-row dim">
                  <span>Delivery</span>
                  <b>{session.totals?.shipping ? `₹${session.totals.shipping}` : 'Free'}</b>
                </div>
                <div className="ladder-note">
                  {session.totals?.shipping
                    ? 'Flat delivery fee — free on orders over ₹999'
                    : 'Delivery waived — order over ₹999'}
                </div>
                <div className="ladder-row total">
                  <span>Total</span>
                  <b>₹{session.totals?.total}</b>
                </div>
                <div className="ladder-note">
                  {captured
                    ? 'Charged after all safety checks passed.'
                    : orderEvent
                      ? session?.payment?.payment_id
                        ? `Order ${shortId(orderEvent.payload?.order_id)} opened at the gateway — payment authorized, awaiting capture.`
                        : `Order ${shortId(orderEvent.payload?.order_id)} opened at the gateway — waiting for your authorization. Nothing charged yet.`
                      : 'No gateway order was created — nothing charged.'}
                </div>
              </div>
            </>
          )}

          {/* policy checks */}
          <SectionTitle>Why this was allowed</SectionTitle>
          {checks?.length ? (
            <div className="policy-grid">
              {checks.map(c => (
                <div key={c.rule} className={`policy-row ${c.result}`}>
                  <span className="pi">{c.result === 'pass' ? '\u2713' : c.result === 'fail' ? '\u2715' : '?'}</span>
                  <span className="pn">{RULE_LABELS[c.rule] || c.rule.replace(/_/g, ' ')}</span>
                  <span className="pd">{c.detail}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="kv" style={{ color: 'var(--muted)' }}>
              No safety checks recorded for this state.
            </div>
          )}

          {/* agent story */}
          {!!agentSteps.length && (
            <>
              <SectionTitle>How your agent handled this</SectionTitle>
              <ul className="story-list">
                {agentSteps.slice(0, 9).map(e => {
                  const meta = metaFor(e.action)
                  return <li key={e.seq}>{meta.title}</li>
                })}
                {agentSteps.length > 9 && (
                  <li className="more">+{agentSteps.length - 9} more steps in the full trail</li>
                )}
              </ul>
            </>
          )}

          {/* proof */}
          {!!proofEvents.length && (
            <>
              <SectionTitle>Payment verification</SectionTitle>
              <div className="proof-list">
                {proofEvents.map(e => {
                  const meta = metaFor(e.action)
                  return (
                    <div key={e.seq} className={`policy-row ${e.decision === 'approved' ? 'pass' : ''}`}>
                      <span className="pi" style={{ color: 'var(--mint)' }}>₹</span>
                      <span className="pn">{meta.title}</span>
                      <span className="pd">{e.payload?.order_id || e.reasoning_summary || ''}</span>
                    </div>
                  )
                })}
              </div>
            </>
          )}

          <button
            className="mini-btn full"
            style={{ marginTop: 14 }}
            onClick={() => { onClose(); window.dispatchEvent(new CustomEvent('acg:open-ops', { detail: sessionId })) }}
          >
            Open full audit trail
          </button>
          <button
            className="mini-btn full"
            style={{ marginTop: 8 }}
            onClick={() => { onClose(); window.dispatchEvent(new CustomEvent('acg:open-consumer', { detail: sessionId })) }}
          >
            Authorization &amp; proof (plain English)
          </button>
        </div>
      </aside>
    </>
  )
}

function SectionTitle({ children }) {
  return <div className="ops-section-title">{children}</div>
}

function shortId(id) {
  if (!id) return ''
  return id.length > 18 ? `${id.slice(0, 18)}…` : id
}
