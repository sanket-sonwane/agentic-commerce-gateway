import { ShoppingBasket } from 'lucide-react'
import CategoryIcon from './CategoryIcon.jsx'

/** Server-authoritative cart snapshot rendered as part of the chat flow.
 *  Pure display — no buttons. Approval happens by replying in chat. */
export default function CartSummary({ ev }) {
  const t = ev.totals || {}
  const items = ev.line_items || []
  const subtotal = items.reduce((s, li) => s + li.unit_price * li.quantity, 0)
  const closed = !!ev.closed

  return (
    <div className="cart-bubble" data-csid={ev.checkout_session_id}>
      <div className="bill-head"><ShoppingBasket size={13} /> Cart {ev.version ? `· v${ev.version}` : ''}{closed ? ' · closed' : ''}</div>

      {items.map(li => (
        <div className="bill-line" key={li.pack_id}>
          <span className="qty-badge">×{li.quantity}</span>
          <span className="prod-ico">
            <CategoryIcon category={li.category} />
          </span>
          <span className="nm">{li.name}<small>{li.size}{li.unit}</small></span>
          <b>₹{li.unit_price * li.quantity}</b>
        </div>
      ))}

      <div className="bill-ladder">
        <div className="ladder-row"><span>Subtotal</span><span>₹{subtotal}</span></div>
        {t.shipping > 0
          ? <div className="ladder-row"><span>Delivery</span><span>₹{t.shipping}</span></div>
          : <div className="ladder-row"><span>Delivery</span><span className="free-tag">FREE</span></div>}
        {ev.change && (
          <div className="ladder-row">
            <span>Price change</span>
            <span>₹{ev.change.old_total} → ₹{ev.change.new_total}</span>
          </div>
        )}
        <div className="ladder-row total"><span>Total</span><span>₹{t.total}</span></div>
      </div>

      {ev.note && <div className="bill-note">ⓘ <span>{ev.note}</span></div>}
    </div>
  )
}
