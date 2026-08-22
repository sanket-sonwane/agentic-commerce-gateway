import { ReceiptText, Truck, Loader2, Check, X } from 'lucide-react'
import CategoryIcon from './CategoryIcon.jsx'

/** v2 order-summary card with approve gate. `ev` = bill event. */
export default function BillCard({ ev, onDecide, busy }) {
  const t = ev.totals || {}
  const items = ev.line_items || []
  const subtotal = items.reduce((s, li) => s + li.unit_price * li.quantity, 0)
  const locked = !!ev.resolved || busy

  return (
    <div className="bill-card">
      <div className="bill-head"><ReceiptText size={14} /> Order summary {ev.change ? '· revised terms' : ''}</div>

      {items.map(li => (
        <div className="bill-line" key={li.pack_id}>
          <span className="qty-badge">×{li.quantity}</span>
          <span className="prod-ico" style={{ width: 26, height: 26, borderRadius: 8, background: 'var(--panel-2)', display: 'grid', placeItems: 'center', color: 'var(--accent-text)', flex: 'none' }}>
            <CategoryIcon category={li.category} />
          </span>
          <span className="nm">{li.name}<small>{li.size}{li.unit}</small></span>
          <b>₹{li.unit_price * li.quantity}</b>
        </div>
      ))}

      <div className="bill-ladder">
        <div className="ladder-row"><span>Subtotal</span><span>₹{subtotal}</span></div>
        {t.shipping > 0
          ? (
            <div className="ladder-row"><span style={{ display: 'flex', gap: 6, alignItems: 'center' }}><Truck size={13} /> Delivery</span><span>₹{t.shipping}</span></div>
          )
          : (
            <div className="ladder-row"><span style={{ display: 'flex', gap: 6, alignItems: 'center' }}><Truck size={13} /> Delivery</span><span className="free-tag">FREE</span></div>
          )}
        {ev.change && (
          <div className="ladder-row">
            <span>Price change</span>
            <span>₹{ev.change.old_total} → ₹{ev.change.new_total}</span>
          </div>
        )}
        <div className="ladder-row total"><span>Total</span><span>₹{t.total}</span></div>
      </div>

      {ev.note && <div className="bill-note">ⓘ <span>{ev.note}</span></div>}

      {!ev.resolved ? (
        <div className="bill-actions">
          <button className={`btn primary ${busy ? '' : ''}`} disabled={locked} onClick={() => onDecide(true)}>
            {busy ? <Loader2 size={15} className="spin" /> : <Check size={15} />} Approve &amp; Pay ₹{t.total}
          </button>
          <button className="btn ghost-danger" disabled={locked} onClick={() => onDecide(false)}>
            <X size={15} /> Cancel
          </button>
        </div>
      ) : ev.resolved === 'approved' ? (
        <div className="resolved-line approved"><Check size={14} /> Approved by you — processing payment</div>
      ) : (
        <div className="resolved-line cancelled"><X size={14} /> Rejected by you — order cancelled</div>
      )}
    </div>
  )
}
