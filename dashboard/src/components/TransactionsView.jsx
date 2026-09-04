import { useMemo } from 'react'
import { ShoppingBag } from 'lucide-react'
import { TRXN_STATUS } from '../utils/terminology.js'
import { orderTitle } from '../utils/events.js'

/**
 * TRANSACTIONS — consumer purchase history.
 *
 * Authoritative source: the merchant's Order domain, read through the
 * server-side consumer proxy (GET /consumer/orders). A completed checkout
 * materializes a durable Order; those Orders (and their payment + fulfillment
 * state) are the truth shown here. Clicking a row opens the consumer Money
 * Story / purchase proof, which reads the authoritative provenance projection.
 *
 * No payment status, order status, amount, fulfillment or payment identifier
 * is read from localStorage — the list is fetched fresh from the backend.
 */

const FULFILLMENT_LABEL = {
  pending: 'Preparing',
  placed: 'Placed',
  packed: 'Packed',
  shipped: 'Shipped',
  delivered: 'Delivered',
}

export default function TransactionsView({ orders = [], onOpen }) {
  const list = useMemo(() => {
    return [...orders]
      .map(o => ({
        id: o.checkout_session_id,
        checkout_session_id: o.checkout_session_id,
        order_id: o.order_id,
        // Every Order is a paid, settled transaction; fulfillment carries the state.
        status: 'completed',
        fulfillment_status: o.fulfillment_status,
        fulfillment_method: o.fulfillment_method,
        amount: o.amount != null ? o.amount : o.authorized_amount_paise != null ? o.authorized_amount_paise / 100 : null,
        description: orderTitle(o.line_items || []),
        updated_at: o.created_at ? new Date(o.created_at).getTime() : 0,
      }))
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))
  }, [orders])

  return (
    <div className="txns-view">
      <div className="txns-head">
        <h2>Transactions</h2>
        <span className="muted" style={{ fontSize: 11 }}>Your confirmed purchases &amp; fulfilment</span>
      </div>

      {list.length === 0 ? (
        <div className="txns-empty">
          <ShoppingBag size={22} />
          <p>No purchases yet.</p>
          <span className="muted">Go to Shop and let your agent build and confirm an order.</span>
        </div>
      ) : (
        <div className="txns-list">
          {list.map(t => (
            <button key={t.id} className="txn-row" onClick={() => onOpen?.(t.id)}>
              <span className={`txn-row-status ${t.status || ''}`}>
                {TRXN_STATUS[t.status] || (t.status ? t.status.replace(/_/g, ' ') : '—')}
                {t.fulfillment_status ? (
                  <em className="txn-row-fulfillment">
                    {FULFILLMENT_LABEL[t.fulfillment_status] || t.fulfillment_status.replace(/_/g, ' ')}
                  </em>
                ) : null}
              </span>
              <span className="txn-row-desc">{t.description || 'Grocery order'}</span>
              <span className="txn-row-amt">₹{t.amount != null ? t.amount : '—'}</span>
              {t.updated_at ? <span className="txn-row-time">{new Date(t.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span> : null}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
