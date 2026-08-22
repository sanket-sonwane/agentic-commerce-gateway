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

/**
 * "Authorize this order" card.
 * `redirect` carries: checkout_session_id, razorpay_order_id, key_id,
 * customer_id?, save_card?, amount (₹), amount_paise.
 */
export default function RazorpayPayCard({ redirect, onPaid }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [paid, setPaid] = useState(null)

  async function openModal() {
    setError(null)
    setBusy(true)
    try {
      await loadRazorpay()
      const options = {
        key: redirect.key_id,
        order_id: redirect.razorpay_order_id,
        amount: redirect.amount_paise,
        currency: 'INR',
        name: 'Agentic Commerce Gateway',
        description: `Grocery order ${redirect.checkout_session_id}`,
        prefill: { name: 'Demo Shopper', email: 'shopper@acg-demo.local', contact: '+919000000000' },
        notes: { checkout_session_id: redirect.checkout_session_id },
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
      if (redirect.customer_id) options.customer_id = redirect.customer_id
      const rzp = new window.Razorpay(options)
      rzp.open()
    } catch (e) {
      setError(e.message)
      setBusy(false)
    }
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
        <div className="pay-hint">💡 Tick “Save card” in the window — future orders get charged automatically.</div>
      )}
      {error && <div className="bill-note">⚠ {error}</div>}
    </div>
  )
}
