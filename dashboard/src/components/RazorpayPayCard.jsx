import { useState } from 'react'
import { LockKeyhole, Loader2 } from 'lucide-react'
import { merchantFetch } from '../api.js'

let rzpLoading = null
export function loadRazorpay() {
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

// Errors that mean "mandate registration is unavailable here" — the account
// lacks E-Mandate activation or no instrument supports auto-debit. Checkout
// reports these before any card entry, as an empty method list.
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
 * "Authorize this order" card.
 * `redirect` carries: checkout_session_id, razorpay_order_id, key_id,
 * customer_id?, save_card?, amount (₹), amount_paise.
 *
 * Two-stage strategy:
 *   1. Mandate mode (customer_id + recurring:true) — registers an E-Mandate so
 *      future orders charge headlessly.
 *   2. If Razorpay reports mandate registration is unavailable (account-level
 *      feature disabled, unsupported card), automatically reopen ONCE in plain
 *      save-card mode so the purchase is never blocked; the merchant can then
 *      enable E-Mandate for true headless payments later.
 */
export default function RazorpayPayCard({ redirect, onPaid }) {
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
        // Razorpay echoes these onto the payment entity — lets the backend
        // audit prove which checkout stage actually ran.
        mode: mandateMode ? 'mandate' : 'onetap',
      },
      theme: { color: '#6366f1' },
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
      // E-Mandate registration per Razorpay Recurring Payments docs:
      // {customer_id, order_id, recurring: true}. The ORDER itself carries the
      // token block (max_amount/expire_at/frequency) created server-side.
      // Cards only — use test card 4111 1111 1111 1111 (prepaid BINs never
      // support mandates).
      options.customer_id = redirect.customer_id
      options.recurring = true
    } else if (redirect.customer_id) {
      options.customer_id = redirect.customer_id
    }

    const rzp = new window.Razorpay(options)
    rzp.on('payment.error', (resp) => {
      if (mandateMode && !retried && isMandateBlock(resp)) {
        setNotice(
          '⚠ Auto-pay registration unavailable — the merchant account likely has E-Mandate disabled (or the card does not support mandates). Reopening in one-tap mode: this order completes now, but each future order will need a quick OTP.',
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
      <div className="bill-card pay-card">
        <div className="bill-head"><LockKeyhole size={14} /> Payment authorized</div>
        <div className="ladder-row total" style={{ paddingTop: 4 }}><span>Paid</span><span>₹{paid.amount}</span></div>
        {paid.saved && (
          <div style={{ display: 'flex', gap: 7, alignItems: 'center', fontSize: 12.5, color: 'var(--green)', marginTop: 8 }}>
            💳 {paid.saved.network || 'Card'} ••••{paid.saved.last4} saved — future orders will be charged automatically.
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="bill-card pay-card">
      <div className="bill-head"><LockKeyhole size={14} /> Authorize payment</div>
      <div className="bill-line"><span>Razorpay test-mode charge</span><b>₹{redirect.amount}</b></div>
      <div className="bill-actions">
        <button className="btn primary" disabled={busy} onClick={openModal}>
          {busy ? <><Loader2 size={15} /> Opening secure window…</> : <>Pay ₹{redirect.amount} securely</>}
        </button>
      </div>
      {redirect.save_card && !paid && (
        <div className="pay-hint">💡 Use test card <b>4111 1111 1111 1111</b> (any future expiry/CVV, OTP any 4 digits). If auto-pay registration succeeds, every future order charges automatically with no window.</div>
      )}
      {notice && <div className="bill-note" style={{ color: '#b45309' }}>{notice}</div>}
      {error && <div className="bill-note">⚠ {error}</div>}
    </div>
  )
}
