/**
 * SSE event → UI contract (the suppression/humanization layer).
 *
 * Chat renders ONLY what this module returns. Internal machinery
 * (tool calls, discovery payloads, raw session ids) is folded into a
 * single collapsed activity summary — visible on demand, never noise.
 */

export const SUPPRESSED_TYPES = new Set(['conversation_started', 'tool', 'found'])

const STATUS_COPY = {
  completed: { icon: '✅', text: 'Order placed' },
  cancelled: { icon: '🚫', text: 'Order cancelled' },
  declined: { icon: '', text: "Order couldn't be placed" },
  payment_pending: { icon: '⏳', text: 'Payment pending — nothing was charged' },
  requires_payment: { icon: '🔐', text: 'Waiting for your payment' },
  requires_confirmation: { icon: '', text: 'Waiting for your confirmation' },
}

export function humanStatus(status) {
  const copy = STATUS_COPY[status]
  return copy ? `${copy.icon} ${copy.text}`.trim() : status?.replace(/_/g, ' ')
}

/** Short human title for a bill/substitution event ("Milk ×2 + ₹40 delivery"). */
export function orderTitle(lineItems = []) {
  if (!lineItems.length) return 'your order'
  const first = lineItems[0]
  const rest = lineItems.length - 1
  const base = `${first.name}${first.quantity > 1 ? ` ×${first.quantity}` : ''}`
  return rest > 0 ? `${base} +${rest} more` : base
}

/**
 * Reduce raw SSE events into render items.
 *
 * Output item kinds:
 *   user        – user bubble            { text }
 *   ai          – assistant bubble       { text, tone? }
 *   order       – order-ready + chips    { ev }   (bill)
 *   swap        – substitution + chips   { ev }   (substitute_proposal)
 *   payment     – the single money card  { ev }   (bill_redirect)
 *   caption     – centered micro-status  { text, csid }
 *   activity    – collapsed machinery    { labels }
 *   error       – inline alert           { text }
 */
export function toViewItems(events, { busy }) {
  const items = []
  let activity = []

  const flushActivity = () => {
    if (!activity.length) return
    items.push({ kind: 'activity', id: `act-${items.length}`, labels: activity })
    activity = []
  }

  for (const ev of events) {
    switch (ev.type) {
      case 'user_message':
        flushActivity()
        items.push({ kind: 'user', id: `u-${ev.seq ?? ev.time}`, text: ev.content })
        break
      case 'message':
        flushActivity()
        items.push({ kind: 'ai', id: `m-${ev.time}`, text: ev.content })
        break
      case 'bill':
        flushActivity()
        items.push({ kind: 'order', id: `b-${ev.time}`, ev })
        break
      case 'substitute_proposal':
        flushActivity()
        items.push({ kind: 'swap', id: `s-${ev.time}`, ev })
        break
      case 'bill_redirect':
        flushActivity()
        items.push({ kind: 'payment', id: `p-${ev.time}`, ev })
        break
      case 'status':
        flushActivity()
        items.push({
          kind: 'caption',
          id: `st-${ev.time}`,
          text: humanStatus(ev.status),
          csid: ev.checkout_session_id,
          payment: ev.payment,
        })
        break
      case 'error':
        flushActivity()
        items.push({ kind: 'error', id: `e-${ev.time}`, text: ev.content })
        break
      case 'tool':
        if (ev.summary) activity.push(ev.summary)
        break
      case 'found': {
        const n = ev.candidates?.length ?? 0
        activity.push(n ? `found ${n} option${n > 1 ? 's' : ''}` : 'searched the catalog')
        break
      }
      default:
        break // conversation_started & friends stay invisible
    }
  }

  if (busy && events.length) {
    // live shimmer label = most recent machinery line, else generic
    items.push({ kind: 'working', id: 'working', label: activity[activity.length - 1] || null })
  }

  return items
}
