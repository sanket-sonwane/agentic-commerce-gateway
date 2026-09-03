import { useEffect, useState } from 'react'
import { X, ShieldCheck, Lock } from 'lucide-react'
import { merchantFetch } from '../api.js'
import { PAYMENT_STATUS, PROOF_LABELS, PROOF_STATUS } from '../utils/terminology.js'

/**
 * CONSUMER PROVENANCE — a plain-language view of how one purchase was
 * authorized, gated, and (where applicable) paid for.
 *
 * Renders EXACTLY what the backend projection returns (single call to
 * /consumer/transactions/{id}/provenance). It does NO business logic, NO
 * cryptography, and NO policy decisions — it is presentation only.
 */

export default function ConsumerProvenanceDrawer({ sessionId, onClose }) {
  const [state, setState] = useState(null)
  const [error, setError] = useState(null)
  const [showTech, setShowTech] = useState(false)

  useEffect(() => {
    let alive = true
    merchantFetch(`/consumer/transactions/${sessionId}/provenance`).then(r => {
      if (!alive) return
      if (r.ok && r.body) setState(r.body)
      else setError(r.body?.error?.message || 'Could not load authorization details')
    })
    const onKey = e => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => { alive = false; window.removeEventListener('keydown', onKey) }
  }, [sessionId, onClose])

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="explain-drawer" role="dialog" aria-label="Authorization and proof">
        <div className="drawer-head">
          <h3>Authorization &amp; proof</h3>
          <span className="mono" style={{ fontSize: 10, color: 'var(--faint)' }}>{sessionId}</span>
          <button className="icon-btn" onClick={onClose} title="Close"><X size={15} /></button>
        </div>
        <div className="drawer-body">
          {error && <div className="drawer-answer" style={{ color: 'var(--red)' }}>{error}</div>}
          {!state && !error && <div className="drawer-answer">Reading the authorization trail…</div>}

          {state && (
            <>
              {/* header */}
              <div className="kv" style={{ marginBottom: 4 }}>
                <b>merchant</b> {state.transaction.merchant}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '0 0 12px' }}>
                <span style={{ fontFamily: 'var(--font-head)', fontSize: 22, fontWeight: 800 }}>₹{state.transaction.amount}</span>
                <span className={`badge ${state.transaction.status}`}>{state.transaction.status.replace(/_/g, ' ')}</span>
              </div>

              {/* stopped at banner */}
              {state.stopped_at && (
                <div className="drawer-answer" style={{ borderColor: 'rgba(239,68,68,0.3)' }}>
                  <span style={{ fontWeight: 800 }}>Stopped at {state.stopped_at.replace(/_/g, ' ')}.</span>{' '}
                  {state.failure_reason ? `${state.failure_reason}.` : 'The purchase did not complete.'}
                </div>
              )}

              {/* journey stepper */}
              <SectionTitle>How this reached you</SectionTitle>
              <div className="story-stepper" style={{ flexWrap: 'wrap' }}>
                {state.journey.map(s => (
                  <div key={s.stage} className={`story-stage ${s.status === 'completed' ? 'done' : ''} ${s.status === 'failed' ? 'outcome declined' : ''} ${s.status === 'reached' ? 'reached' : ''}`}>
                    <span className="stage-dot" />
                    <span className="stage-label">{s.title}</span>
                  </div>
                ))}
              </div>

              {/* who / what / limit */}
              <SectionTitle>Who was allowed to spend</SectionTitle>
              <div className="kv"><b>authorized by</b> {state.authorization.authorized_by}</div>
              <div className="kv"><b>agent</b> {state.authorization.agent}</div>
              <div className="kv"><b>what</b> {state.authorization.scope || 'Purchases for this account'}</div>
              {state.authorization.max_amount_paise != null && (
                <div className="kv"><b>limit</b> up to ₹{Math.round(state.authorization.max_amount_paise / 100)} per order</div>
              )}
              {!!state.authorization.merchant_scope?.length && (
                <div className="kv"><b>categories</b> {state.authorization.merchant_scope.join(', ')}</div>
              )}

              {/* safety checks */}
              <SectionTitle>Safety checks</SectionTitle>
              {state.checks.length ? (
                <div className="policy-grid">
                  {state.checks.map(c => (
                    <div key={c.name} className={`policy-row ${c.status === 'pass' ? 'pass' : c.status === 'fail' ? 'fail' : ''}`}>
                      <span className="pi">{c.status === 'pass' ? '✓' : c.status === 'fail' ? '✕' : '?'}</span>
                      <span className="pn">{c.name}</span>
                      <span className="pd">{c.summary}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="kv" style={{ color: 'var(--muted)' }}>No policy checks were recorded for this state.</div>
              )}

              {/* razorpay */}
              <SectionTitle>Payment</SectionTitle>
              <div className="kv"><b>provider</b> {state.payment.provider}</div>
              <div className="policy-row" style={{ marginTop: 4 }}>
                <span className="pi" style={{ color: 'var(--mint)' }}>₹</span>
                <span className="pn">
                  {state.payment.razorpay_called
                    ? PAYMENT_STATUS[state.payment.status] || 'Gateway interaction recorded'
                    : 'Payment was not attempted'}
                </span>
                <span className="pd">
                  {state.payment.order_id ? `order ${state.payment.order_id}` : ''}
                  {state.payment.payment_id ? ` · payment ${state.payment.payment_id}` : ''}
                </span>
              </div>

              {/* proof */}
              <SectionTitle>Proof</SectionTitle>
              <div className="proof-list">
                {[
                  ['execution_mandate', state.proof.execution_mandate],
                  ['policy_decision', state.proof.policy_decision],
                  ['decision_receipt', state.proof.decision_receipt],
                  ['provenance', state.proof.provenance],
                  ['audit_chain', state.proof.audit_chain],
                ].map(([field, st]) => (
                  <div key={field} className={`policy-row ${st === 'verified' ? 'pass' : st === 'failed' ? 'fail' : ''}`}>
                    <span className="pi"><ShieldCheck size={12} /></span>
                    <span className="pn">{PROOF_LABELS[field] || field}</span>
                    <span className="pd">{PROOF_STATUS[st] || st}</span>
                  </div>
                ))}
              </div>
              <div className="kv" style={{ color: 'var(--faint)', marginTop: 6 }}>
                <Lock size={10} /> Independently verifiable by the standalone offline verifier.
              </div>

              {/* technical evidence expander */}
              <button
                className="mini-btn full"
                style={{ marginTop: 14 }}
                onClick={() => setShowTech(v => !v)}
              >
                {showTech ? 'Hide' : 'View'} technical evidence
              </button>
              {showTech && (
                <div className="anim-in" style={{ marginTop: 10 }}>
                  <div className="kv"><b>mandate</b> {state.proof.mandate_id || 'n/a'}</div>
                  <div className="kv"><b>key id</b> {state.proof.key_id || 'n/a'}</div>
                  <div className="kv"><b>verifier</b> {state.proof.verifier_status}</div>
                  <div className="kv" style={{ marginTop: 6, color: 'var(--muted)' }}>
                    {state.authorization.authority_note || 'Authorization recognized from your agent delegation.'}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  )
}

function SectionTitle({ children }) {
  return <div className="ops-section-title">{children}</div>
}
