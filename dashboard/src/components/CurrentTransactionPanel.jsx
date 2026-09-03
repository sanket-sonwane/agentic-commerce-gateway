import { useEffect, useState } from 'react'
import { ShieldCheck, ShieldX, LockKeyhole, BadgeCheck, ChevronRight, Clock, XCircle } from 'lucide-react'
import { merchantFetch } from '../api.js'
import { PROOF_VERIFY, paymentBucket, STAGE_STOPPED_LABEL } from '../utils/terminology.js'

/**
 * CURRENT TRANSACTION PANEL — the persistent right rail.
 * Shows ONE live purchase with the consumer-fist hierarchy:
 *   RESULT -> AMOUNT -> STATUS -> WHAT HAPPENED -> WHY -> NEXT ACTION -> PROOF
 *
 * Reads ONLY the authoritative consumer projection
 * (/consumer/transactions/{id}/provenance). It never invents payment truth.
 */
export default function CurrentTransactionPanel({ sessionId, onWhy, onOps }) {
  const [proj, setProj] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    async function load() {
      if (!sessionId) return
      const r = await merchantFetch(`/consumer/transactions/${sessionId}/provenance`)
      if (!alive) return
      if (r.ok && r.body) setProj(r.body)
      else setError(r.body?.error?.message || 'Could not load transaction')
    }
    load()
    const t = setInterval(load, 2500)
    return () => { alive = false; clearInterval(t) }
  }, [sessionId])

  if (!sessionId) return null
  if (error) {
    return (
      <aside className="txn-rail">
        <div className="txn-rail-title">Current transaction</div>
        <div className="muted" style={{ fontSize: 12 }}>{error}</div>
      </aside>
    )
  }
  if (!proj) {
    return (
      <aside className="txn-rail">
        <div className="txn-rail-title">Current transaction</div>
        <div className="muted" style={{ fontSize: 12 }}>Loading…</div>
      </aside>
    )
  }

  const trx = proj.transaction
  const pay = proj.payment || {}
  const bucket = paymentBucket(pay)
  const checks = proj.checks || []
  const passCount = checks.filter(c => c.status === 'pass').length
  const failChecks = checks.filter(c => c.status === 'fail')
  const stopped = proj.stopped_at
  const authVerified = proj.authorization?.verified
  const authSource = proj.authorization?.authorization_source

  const headline =
    bucket === 'VERIFIED' ? 'Payment completed' :
    bucket === 'DECLINED' ? 'Payment declined' :
    bucket === 'AUTHORIZED' ? 'Payment authorized' :
    bucket === 'PENDING' ? 'Payment in progress' :
    bucket === 'ATTEMPTED' ? 'Awaiting payment confirmation' :
    stopped ? 'Purchase stopped' :
    'Order ready'

  const amountHead = `₹${trx.amount}`
  const statusText =
    bucket === 'VERIFIED' ? 'Paid' :
    bucket === 'DECLINED' ? (pay.payment_id ? 'Declined' : 'Declined — nothing was charged') :
    bucket === 'AUTHORIZED' ? 'Payment authorized' :
    bucket === 'PENDING' ? 'Payment in progress' :
    bucket === 'ATTEMPTED' ? 'Awaiting confirmation' :
    stopped ? (pay.payment_id ? 'Payment not completed' : 'Nothing was charged') :
    'Ready for your approval'

  return (
    <aside className="txn-rail">
      <div className="txn-rail-title">Current transaction</div>

      <div className={`txn-card ${bucket === 'VERIFIED' ? 'ok' : bucket === 'DECLINED' ? 'bad' : ''}`}>
        <div className="txn-amount">{amountHead}</div>
        <div className={`txn-status ${bucket === 'VERIFIED' ? 'yes' : bucket === 'DECLINED' ? 'no' : ''}`}>
          {bucket === 'VERIFIED' ? <BadgeCheck size={13} /> : bucket === 'DECLINED' ? <XCircle size={13} /> : <Clock size={13} />}
          {statusText}
        </div>

        <div className="txn-section">What happened</div>
        <div className="txn-copy">
          {stopped
            ? `${STAGE_STOPPED_LABEL[stopped] || 'The purchase did not complete'}. ${proj.failure_reason || ''}`.trim()
            : bucket === 'VERIFIED'
              ? 'Your agent placed the order.'
              : bucket === 'AUTHORIZED' || bucket === 'PENDING' || bucket === 'ATTEMPTED'
                ? 'Your agent started the payment and is awaiting confirmation.'
                : 'Your agent built the order and is waiting for your approval.'}
        </div>

        {stopped && (
          <div className="txn-blocked">
            <XCircle size={12} /> {pay.payment_id ? 'Payment was not completed' : 'Nothing was charged'}
          </div>
        )}

        <div className="txn-section">Why this is allowed</div>
        <div className="txn-checks">
          {authVerified && (
            <span className="txn-check yes">
              <ShieldCheck size={11} />
              {authSource === 'user_signed' ? 'You approved this purchase' : 'Agent permission recognized'}
            </span>
          )}
          {passCount > 0 && <span className="txn-check yes"><ShieldCheck size={11} /> {passCount} safety check{passCount > 1 ? 's' : ''} passed</span>}
          {failChecks.map(c => (
            <span key={c.name} className="txn-check no"><ShieldX size={11} /> {c.name}</span>
          ))}
          {!checks.length && !authVerified && <span className="muted" style={{ fontSize: 11 }}>Awaiting verification…</span>}
        </div>

        {checks.length > 0 && (
          <details className="txn-detail">
            <summary>See checks</summary>
            <div className="txn-detail-list">
              {checks.map(c => (
                <div key={c.name} className={`txn-detail-row ${c.status}`}>
                  <span className="xn">{c.status === 'pass' ? '✓' : c.status === 'fail' ? '✕' : '·'}</span>
                  <span className="nn">{c.name}</span>
                  <span className="dd">{c.summary}</span>
                </div>
              ))}
            </div>
          </details>
        )}

        <div className="txn-section">Payment</div>
        <div className="txn-pay">
          {bucket === 'VERIFIED' && <span className="txn-check yes"><BadgeCheck size={11} /> Payment verified</span>}
          {bucket === 'AUTHORIZED' && <span className="txn-check wait"><Clock size={11} /> Payment authorized — awaiting capture</span>}
          {bucket === 'PENDING' && <span className="txn-check wait"><Clock size={11} /> Payment is being processed</span>}
          {bucket === 'DECLINED' && <span className="txn-check no"><XCircle size={11} /> {pay.payment_id ? 'Payment declined' : 'Payment declined — nothing charged'}</span>}
          {bucket === 'ATTEMPTED' && <span className="txn-check wait"><Clock size={11} /> Payment sent, awaiting confirmation</span>}
          {(bucket === 'NOT_ATTEMPTED' && !stopped) && <span className="txn-check no"><LockKeyhole size={11} /> Payment not attempted</span>}
        </div>

        <div className="txn-section">Proof</div>
        <div className="txn-proof">
          <ProofLine label="Authorization" state={authVerified ? 'verified' : 'unavailable'} />
          <ProofLine label="Safety checks" state={proj.proof?.policy_decision} />
          <ProofLine label="Payment binding" state={proj.proof?.decision_receipt} />
          <ProofLine label="Audit trail" state={proj.proof?.audit_chain} />
        </div>

        <div className="txn-actions">
          <button className="txn-btn" onClick={() => onWhy?.(trx.id)}>
            View proof <ChevronRight size={13} />
          </button>
          <button className="txn-btn ghost" onClick={() => onOps?.(trx.id)}>
            Full audit trail
          </button>
        </div>
      </div>

      {proj.checkout_session_id && (
        <div className="txn-note mono" title="Authoritative merchant session id">
          {proj.checkout_session_id.slice(0, 22)}…
        </div>
      )}
    </aside>
  )
}

function ProofLine({ label, state }) {
  const ok = state === 'verified'
  const bad = state === 'failed'
  return (
    <div className="txn-proof-line">
      <span className={ok ? 'yes' : bad ? 'no' : 'wait'}>
        {ok ? '✓' : bad ? '✕' : '·'}
      </span>
      <span className="nn">{label}</span>
      <span className="dd">{PROOF_VERIFY[state] || 'Not available'}</span>
    </div>
  )
}
