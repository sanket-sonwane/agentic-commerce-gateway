/**
 * SSE event \u2192 UI contract (the suppression/humanization layer).
 *
 * Chat renders ONLY what this module returns. Internal machinery
 * (tool calls, discovery payloads, raw session ids) is folded into a
 * single collapsed activity summary \u2014 visible on demand, never noise.
 *
 * Human status uses centralized terminology from terminology.js.
 */
import { humanStatus as _humanStatus } from './terminology.js'

export const SUPPRESSED_TYPES = new Set(['conversation_started', 'tool', 'found'])

export { _humanStatus as humanStatus }

/** Short human title for a bill/substitution event ("Milk \u00D72 + \u20B940 delivery"). */
export function orderTitle(lineItems = []) {
  if (!lineItems.length) return 'your order'
  const first = lineItems[0]
  const rest = lineItems.length - 1
  const base = `${first.name}${first.quantity > 1 ? ` \u00D7${first.quantity}` : ''}`
  return rest > 0 ? `${base} +${rest} more` : base
}

/**
 * Reduce raw SSE events into render items.
 *
 * Output item kinds:
 *   user        \u2013 user bubble            { text }
 *   ai          \u2013 assistant bubble       { text, tone? }
 *   order       \u2013 order-ready + chips    { ev }   (bill)
 *   swap        \u2013 substitution + chips   { ev }   (substitute_proposal)
 *   payment     \u2013 the single money card  { ev }   (bill_redirect)
 *   caption     \u2013 centered micro-status  { text, csid }
 *   activity    \u2013 collapsed machinery    { labels }
 *   error       \u2013 inline alert           { text }
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
          text: _humanStatus(ev.status),
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
