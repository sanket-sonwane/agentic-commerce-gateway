/**
 * Dictionary mapping EVERY audit action to human language + payload chips.
 * Used by AuditTimeline v2 and the Money Story drawer.
 *
 * kind: 'money' | 'agent' | 'system' | 'buyer'
 */

const rupees = n => `₹${n}`

export const ACTION_META = {
  // ---------- buyer / protocol ----------
  discovery_requested: { title: 'Asked the catalog', kind: 'buyer' },
  discovery_result_generated: { title: 'Catalog answered', kind: 'agent' },
  checkout_create_requested: { title: 'Order requested', kind: 'buyer' },
  checkout_update_requested: { title: 'Basket change requested', kind: 'buyer' },
  confirmation_requested: { title: 'Confirmation asked', kind: 'buyer' },
  complete_requested: { title: 'Payment requested', kind: 'money', kindLabel: 'buyer' },

  // ---------- deterministic control plane ----------
  gating_evaluated: {
    title: 'Policy engine',
    kind: 'money',
    chips: p => (p?.checks || []).map(c => ({ label: `${c.rule}: ${c.result}`, tone: c.result })),
  },
  final_validation: {
    title: 'Final validation (pre-money)',
    kind: 'money',
    chips: p => (p?.checks || []).map(c => ({ label: `${c.rule}: ${c.result}`, tone: c.result })),
  },
  reservation_created: { title: 'Stock reserved', kind: 'system', chips: p => (p?.expires_at ? [{ label: `holds until ${String(p.expires_at).slice(11, 19)}` }] : []) },
  checkout_created: { title: 'Checkout opened', kind: 'system', chips: p => (p ? [] : []) },
  checkout_updated: { title: 'Basket updated', kind: 'system' },
  checkout_declined: { title: 'Order declined', kind: 'money' },
  checkout_completed: { title: 'Order completed', kind: 'money' },
  checkout_cancelled: { title: 'Order cancelled — stock released', kind: 'system' },
  confirmation_accepted: { title: 'Buyer accepted terms', kind: 'buyer' },
  confirmation_rejected: { title: 'Buyer rejected terms', kind: 'buyer' },
  inventory_adjusted: { title: 'Operator changed stock', kind: 'system', chips: p => (p && 'old' in p ? [{ label: `${p.old} → ${p.new}` }] : []) },
  demo_reset: { title: 'Demo reset', kind: 'system' },

  // ---------- razorpay ----------
  razorpay_order_creation: { title: 'Creating gateway order', kind: 'money' },
  razorpay_order_created: {
    title: 'Razorpay order created',
    kind: 'money',
    chips: p => {
      const out = []
      if (p?.order_id) out.push({ label: p.order_id, mono: true })
      if (p?.mandate_order) out.push({ label: 'mandate order' })
      return out
    },
  },
  razorpay_token_charge: { title: 'Charging saved card', kind: 'money' },
  payment_success: { title: 'Payment captured', kind: 'money' },
  payment_failure: { title: 'Gateway declined', kind: 'money' },
  payment_pending: { title: 'Payment unresolved', kind: 'money' },
  awaiting_authorization: { title: 'Awaiting card authorization', kind: 'money' },
  payment_token_saved: { title: 'Card token saved', kind: 'money' },
  token_needs_reauth: { title: 'Token needs re-authorization', kind: 'money' },

  // ---------- merchant agent ----------
  agent_search_catalog: { title: 'Searched catalog', kind: 'agent' },
  agent_get_product: { title: 'Fetched product', kind: 'agent' },
  agent_check_inventory: { title: 'Checked live stock', kind: 'agent' },
  agent_get_current_price: { title: 'Checked price', kind: 'agent' },
  agent_find_alternative: { title: 'Looked for alternatives', kind: 'agent' },
  agent_run_gating: { title: 'Previewed policy', kind: 'agent' },
  agent_create_checkout: { title: 'Created checkout', kind: 'agent' },
  agent_update_checkout: { title: 'Updated checkout', kind: 'agent' },
  agent_get_checkout: { title: 'Read checkout state', kind: 'agent' },
  agent_request_confirmation: { title: 'Proposed alternative to buyer', kind: 'agent' },
  agent_apply_accepted_alternative: { title: 'Applied accepted alternative', kind: 'agent' },
  agent_ready_for_completion: { title: 'Handed basket to buyer', kind: 'agent' },
  agent_request_completion: { title: 'Requested gated completion', kind: 'money' },
  agent_outcome: { title: 'Agent outcome', kind: 'agent', chips: p => (p?.reason_code ? [{ label: p.reason_code, mono: true }] : []) },
  agent_reasoning_only: { title: 'Thinking…', kind: 'agent' },
  agent_decline_hold: { title: 'Decline held for confirmation', kind: 'system' },
  agent_mode_hold: { title: 'Mode mismatch corrected', kind: 'system' },
  agent_unpaid_completed_hold: { title: "Unpaid 'completed' claim blocked", kind: 'system' },
  buyer_proposal_decision: {
    title: 'Buyer decided on proposal',
    kind: 'buyer',
    chips: p => (p ? [{ label: p.accepted ? 'accepted' : 'rejected', tone: p.accepted ? 'pass' : 'fail' }] : []),
  },
  buyer_terms_decision: {
    title: 'Buyer decided on new terms',
    kind: 'buyer',
    chips: p => (p ? [{ label: p.accepted ? 'accepted' : 'rejected', tone: p.accepted ? 'pass' : 'fail' }] : []),
  },
}

export function metaFor(action) {
  return ACTION_META[action] || { title: action.replace(/_/g, ' '), kind: 'system' }
}

export function actorColor(actor) {
  if (actor === 'razorpay') return 'var(--mint)'
  if (actor === 'merchant_agent') return 'var(--violet)'
  if (actor === 'operator') return 'var(--amber)'
  return '#6aa3ff' // buyer azure
}

/** Derive lifecycle stages from an event list for the verdict stepper. */
export function deriveStages(events = []) {
  const has = a => events.some(e => e.action === a)
  const stages = [{ key: 'requested', label: 'Requested', done: has('checkout_create_requested') || has('checkout_created') }]
  stages.push({ key: 'validated', label: 'Validated', done: has('gating_evaluated') || has('final_validation') })
  const gateApproved = events.some(
    e => (e.action === 'gating_evaluated' || e.action === 'final_validation') && e.decision === 'approved',
  )
  stages.push({ key: 'gated', label: 'Gated', done: gateApproved })
  const authed =
    has('razorpay_order_created') ||
    has('awaiting_authorization') ||
    has('razorpay_token_charge') ||
    has('payment_success')
  stages.push({ key: 'authorized', label: 'Authorized', done: authed })
  const paid = has('payment_success')
  const failed = has('payment_failure') || has('checkout_declined')
  stages.push({ key: 'outcome', label: paid ? 'Paid' : failed ? 'Declined' : 'Open', done: paid || failed, outcome: true })
  return stages
}
