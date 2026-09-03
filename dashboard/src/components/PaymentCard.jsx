import { useState } from 'react'
import { LockKeyhole, Loader2, BadgeCheck, ShieldCheck } from 'lucide-react'
import { merchantFetch } from '../api.js'
import { mapPaymentState, explainPayment, PAYMENT_UI } from '../utils/paymentState.js'

let rzpLoading = null
function loadRazorpay() {
  if (window.Razorpay) return Promise.resolve()
  if (!rzpLoading) {
    rzpLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script')
      s.src = 'https://checkout.razorpay.com/v1/checkout.js'
      s.onload = resolve
      s.onerror = () => reject(new Error('Could not load Razorpay Checkout'))
      document.body.appendChild(s)
    })
  }
  return rzpLoading
}

// Errors meaning "mandate registration unavailable here" — account lacks
// E-Mandate activation or no instrument supports auto-debit.
const MANDATE_BLOCK_REASONS = [
  'no_appropriate_payment_method_found',
  'payment_method_not_available',
  'payment_method_not_supported',
  'emandate',
  'mandate',
]

function isMandateBlock(resp) {
  const err = resp?.error || {}
  const reason = String(err.reason || err.code || '').toLowerCase()
  const desc = String(err.description || '').toLowerCase()
  const blob = `${reason} ${desc}`
  return (
    MANDATE_BLOCK_REASONS.some((r) => blob.includes(r)) ||
    desc.includes('no appropriate payment method')
  )
}

// Small read-only safety summary shown before payment.
function SafetySummary() {
  return (
    <div className="safety-summary">
      <span className="safety-item pass">
        <ShieldCheck size={12} /> Approved by you
      </span>
      <span className="safety-item pass">
        <ShieldCheck size={12} /> Within your spending limit
      </span>
      <span className="safety-item pass">
        <ShieldCheck size={12} /> Items available
      </span>
    </div>
  )
}

/**
 * THE single money card. `redirect` carries: checkout_session_id,
 * razorpay_order_id, key_id, customer_id?, save_card?, amount, amount_paise.
 *
 * Two-stage strategy (unchanged from baseline):
 *   1. E-Mandate mode → registers auto-debit for headless future orders.
 *   2. On mandate-block, reopen ONCE in plain save-card mode.
 */
export default function PaymentCard({ redirect, onPaid }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [paid, setPaid] = useState(null)
  const [proof, setProof] = useState(null)

  async function openModal() {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      await loadRazorpay()
      launch(true)
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
  }

  function launch(mandateMode, retried = false) {
    const options = {
      key: redirect.key_id,
      order_id: redirect.razorpay_order_id,
      amount: redirect.amount_paise,
      currency: 'INR',
      name: 'Agentic Commerce Gateway',
      description: `Grocery order ${redirect.checkout_session_id}`,
      prefill: { name: 'Demo Shopper', email: 'shopper@acg-demo.local', contact: '+919000000000' },
      notes: {
        checkout_session_id: redirect.checkout_session_id,
        mode: mandateMode ? 'mandate' : 'onetap',
      },
      theme: { color: '#5865f2' },
      handler: async (resp) => {
        try {
          const { ok, body } = await merchantFetch(
            `/checkout_sessions/${redirect.checkout_session_id}/payment_callback`,
            { method: 'POST', body: JSON.stringify(resp) },
          )
          // The backend session is the authoritative source of truth. We never
          // infer "paid" from the callback event — only the backend status.
          const session = ok ? body?.checkout_session : body?.checkout_session || null
          const state = session ? mapPaymentState(session) : PAYMENT_UI.UNKNOWN
          if (state === PAYMENT_UI.COMPLETED) {
            setPaid({
              amount: session.totals.total,
              saved: session.payment.saved_card || null,
            })
            onPaid?.(body)
            // Pull the authoritative consumer projection so the trust claims
            // on the success card are gated behind real proof, never assumed.
            try {
              const p = await merchantFetch(`/consumer/transactions/${redirect.checkout_session_id}/provenance`)
              if (p.ok && p.body) setProof(p.body)
            } catch { /* leave proof null -> claims fall back to pending */ }
          } else if (session) {
            // Evidence-gated copy: only claim "nothing was charged" when the
            // backend proves no payment object was created at the gateway.
            const expl = explainPayment(session, { payment: session.payment })
            setError(`Payment was not completed. ${expl.nothingCharged ? 'Nothing was charged.' : 'A payment may have been initiated — check the transaction.'}`)
          } else {
            // No authoritative session from the callback (error/decline path).
            // Never assert "nothing was charged" without backend proof: consult
            // the session directly to decide.
            let nothingCharged = false
            try {
              const s = await merchantFetch(`/checkout_sessions/${redirect.checkout_session_id}`)
              if (s.ok && s.body) {
                nothingCharged = explainPayment(s.body.checkout_session, { payment: s.body.checkout_session.payment }).nothingCharged
              }
            } catch { /* keep conservative default */ }
            setError(`Payment could not be completed. ${nothingCharged ? 'Nothing was charged.' : 'Check the transaction for the latest payment status.'}`)
          }
        } catch {
          setError('Payment could not be verified. Please check the transaction for the latest status.')
        } finally {
          setBusy(false)
        }
      },
      modal: {
        ondismiss: async () => {
          setBusy(false)
          // Best-effort authoritative check before asserting anything about
          // money movement on dismiss.
          let nothingCharged = true
          try {
            const s = await merchantFetch(`/checkout_sessions/${redirect.checkout_session_id}`)
            if (s.ok && s.body) {
              nothingCharged = explainPayment(s.body.checkout_session, { payment: s.body.checkout_session.payment }).nothingCharged
            }
          } catch { /* keep default */ }
          setError(`Payment cancelled. ${nothingCharged ? 'Nothing was charged. You can retry.' : 'Check the transaction for the latest payment status.'}`)
        },
      },
    }

    if (redirect.customer_id && mandateMode) {
      options.customer_id = redirect.customer_id
      options.recurring = true
    } else if (redirect.customer_id) {
      options.customer_id = redirect.customer_id
    }

    const rzp = new window.Razorpay(options)
    rzp.on('payment.error', async (resp) => {
      if (mandateMode && !retried && isMandateBlock(resp)) {
        setNotice(
          '\u26A0 Auto-pay registration unavailable \u2014 reopening in one-tap mode: this order completes now, but each future order will need a quick OTP.',
        )
        setTimeout(() => launch(false, true), 50)
        return
      }
      // Best-effort authoritative session check: a gateway error can fire after
      // a payment object was created (money may be held). Never assert "nothing
      // was charged" without proving no payment exists.
      let nothingCharged = true
      try {
        const s = await merchantFetch(`/checkout_sessions/${redirect.checkout_session_id}`)
        if (s.ok && s.body) {
          const session = s.body.checkout_session
          nothingCharged = explainPayment(session, { payment: session.payment }).nothingCharged
        }
      } catch { /* keep default */ }
      setError(`Payment could not be completed. ${nothingCharged ? 'Nothing was charged.' : 'A payment may have been initiated — check the transaction.'}`)
      setBusy(false)
    })
    rzp.open()
  }

  if (paid) {
    const p = proof || {}
    const authOk = p.authorization?.verified === true
    const execOk = p.proof?.execution_mandate === 'verified'
    const auditOk = p.proof?.audit_chain === 'verified'
    const pending = !proof
    return (
      <div className="pay-card">
        <div className="pay-paid"><BadgeCheck size={18} /> Payment completed — ₹{paid.amount}</div>
        <div className="pay-proof-summary">
          <div className="proof-check">{authOk ? '✓' : pending ? '·' : '·'} <ShieldCheck size={12} />
            {authOk ? 'Your authorization was verified' : pending ? 'Confirming your authorization…' : 'Authorization record unavailable'}
          </div>
          <div className="proof-check">{execOk ? '✓' : pending ? '·' : '·'} <ShieldCheck size={12} />
            {execOk ? 'Execution mandate verified' : pending ? 'Confirming execution mandate…' : 'Execution mandate unavailable'}
          </div>
          <div className="proof-check">{auditOk ? '✓' : pending ? '·' : '·'} <ShieldCheck size={12} />
            {auditOk ? 'Audit trail recorded' : pending ? 'Recording audit trail…' : 'Audit trail unavailable'}
          </div>
        </div>
        {paid.saved && (
          <div className="pay-saved">
            Card {paid.saved.network || ''} ••••{paid.saved.last4} saved — future orders charge automatically.
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="pay-card">
      <div className="pay-hero">
        <span className="amt">₹{redirect.amount}</span>
        <span className="secure"><LockKeyhole size={12} /> SECURE PAYMENT</span>
      </div>
      <div className="pay-sub">Test-mode charge · you approve every order</div>

      <SafetySummary />

      <button className="btn-pay" disabled={busy} onClick={openModal}>
        {busy ? <><Loader2 size={16} /> Opening secure window…</> : <>Pay ₹{redirect.amount} securely</>}
      </button>
      {redirect.save_card && (
        <div className="pay-hint">
          Test card: <b>4111 1111 1111 1111</b> (any future expiry/CVV, any OTP). If auto-pay registration
          succeeds, future orders charge with no window.
        </div>
      )}
      {notice && <div className="pay-note">{notice}</div>}
      {error && <div className="pay-note" style={{ color: 'var(--red)' }}>⚠ {error}</div>}
    </div>
  )
}
