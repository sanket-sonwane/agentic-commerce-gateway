import { useState } from 'react'
import { LockKeyhole, Loader2, BadgeCheck } from 'lucide-react'
import { merchantFetch } from '../api.js'

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
          if (ok && body?.checkout_session?.status === 'completed') {
            setPaid({
              amount: body.checkout_session.totals.total,
              saved: body.checkout_session.payment.saved_card || null,
            })
            onPaid?.(body)
          } else {
            setError(body?.error?.message || 'Payment could not be verified.')
          }
        } catch {
          setError('Verification request failed — check Merchant Ops view.')
        } finally {
          setBusy(false)
        }
      },
      modal: {
        ondismiss: () => {
          setBusy(false)
          setError('Payment window closed before completion — you can retry.')
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
    rzp.on('payment.error', (resp) => {
      if (mandateMode && !retried && isMandateBlock(resp)) {
        setNotice(
          '⚠ Auto-pay registration unavailable — reopening in one-tap mode: this order completes now, but each future order will need a quick OTP.',
        )
        setTimeout(() => launch(false, true), 50)
        return
      }
      const err = resp?.error || {}
      setError(
        `${err.description || 'Razorpay rejected the payment setup.'} (reason: ${err.reason || err.code || 'unknown'})`,
      )
      setBusy(false)
    })
    rzp.open()
  }

  if (paid) {
    return (
      <div className="pay-card">
        <div className="pay-paid"><BadgeCheck size={18} /> Paid ₹{paid.amount}</div>
        {paid.saved && (
          <div className="pay-saved">
            💳 {paid.saved.network || 'Card'} ••••{paid.saved.last4} saved — future orders charge automatically.
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="pay-card">
      <div className="pay-hero">
        <span className="amt">₹{redirect.amount}</span>
        <span className="secure"><LockKeyhole size={12} /> RAZORPAY SECURE</span>
      </div>
      <div className="pay-sub">Test-mode charge · you approve every order</div>
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
