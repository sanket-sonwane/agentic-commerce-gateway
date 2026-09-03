/**
 * Single source of truth for all user-facing terminology.
 *
 * Every UI surface imports from here instead of defining its own label maps.
 * Backend terminology is NEVER changed — this is a presentation-only layer.
 *
 * PRINCIPLE: Show meaning first, machinery second.
 */

// ---- Safety / policy rule labels (replaces duplicate RULE_LABELS in 3 files) ----
export const RULE_LABELS = {
  spend_limit: 'Your spending limit',
  stock_check: 'Item availability',
  category_allowed: 'Purchase category',
  delegation_expiry: 'Agent permission',
  quantity_limit: 'Quantity per item',
}

// ---- Chat status captions (replaces STATUS_COPY in events.js) ----
export const CHAT_STATUS = {
  completed:              { icon: '\u2705', text: 'Order placed' },
  cancelled:              { icon: '\uD83D\uDEAB', text: 'Order cancelled' },
  declined:               { icon: '',       text: 'Order could not be completed' },
  payment_pending:        { icon: '\u23F3', text: 'Payment in progress' },
  requires_payment:       { icon: '\uD83D\uDD10', text: 'Ready for your payment' },
  requires_confirmation:  { icon: '',       text: 'Waiting for your confirmation' },
}

// ---- Payment provider status (replaces PAYMENT_TEXT in ConsumerProvenanceDrawer) ----
export const PAYMENT_STATUS = {
  verified:     'Payment confirmed and verified',
  authorized:   'Payment authorized \u2014 awaiting capture',
  declined:     'Payment was declined',
  pending:      'Payment is being processed',
  order_created: 'Payment initiated \u2014 not yet charged',
  not_called:   'Payment was not attempted',
}

// ---- Proof labels (replaces internal artifact names) ----
export const PROOF_LABELS = {
  execution_mandate: 'Transaction authorization',
  policy_decision:   'Safety verification',
  decision_receipt:  'Payment binding',
  provenance:        'Full provenance',
  audit_chain:       'Audit trail',
}

export const PROOF_STATUS = {
  verified:    'Verified',
  failed:      'Failed',
  unavailable: 'Not available',
}

// ---- Agent session status labels ----
export const AGENT_STATUS = {
  completed:                      'Completed',
  awaiting_buyer_confirmation:    'Waiting for buyer',
  ready_for_completion:           'Ready for payment',
  awaiting_payment_authorization: 'Payment pending',
  declined:                       'Stopped',
  unable_to_fulfill:              'Could not fulfil',
}

// ---- Friendly status caption for chat (single function, replaces humanStatus in events.js) ----
export function humanStatus(status) {
  const copy = CHAT_STATUS[status]
  return copy ? `${copy.icon} ${copy.text}`.trim() : status?.replace(/_/g, ' ')
}

// ---- Integrity badge labels ----
export const INTEGRITY = {
  verified: 'Audit verified',
  tampered: 'Audit integrity issue',
}

// ---- Transaction status verbs (single map for Transactions list + current
//      transaction panel; replaces the private STATUS_VERB in OpsView). ----
export const TRXN_STATUS = {
  completed:              'Paid',
  requires_payment:       'Awaiting payment',
  payment_pending:        'Payment pending',
  requires_confirmation:  'Awaiting confirmation',
  declined:               'Stopped',
  cancelled:              'Cancelled',
  created:                'Preparing',
}

// ---- Consumer-purchase proof stage: one human verb per backend proof state.
//      Frontend shows EXACTLY what the backend projection reported. ----
export const PROOF_VERIFY = {
  verified:    'Verified',
  failed:      'Failed',
  unavailable: 'Not available',
}

// ---- Payment outcome buckets (Phase 21: never fabricate PAID/ATTEMPTED). ----
// Maps the backend consumer projection payment status to a display bucket.
export function paymentBucket(payment) {
  if (!payment) return 'NOT_ATTEMPTED'
  switch (payment.status) {
    case 'verified':
      return 'VERIFIED'
    case 'authorized':
      return 'AUTHORIZED'
    case 'declined':
      return 'DECLINED'
    case 'pending':
      return payment.payment_id ? 'AUTHORIZED' : 'PENDING'
    case 'order_created':
      return payment.payment_id ? 'AUTHORIZED' : (payment.razorpay_called ? 'ATTEMPTED' : 'NOT_ATTEMPTED')
    default:
      return payment.razorpay_called ? 'ATTEMPTED' : 'NOT_ATTEMPTED'
  }
}

// ---- Complaint / failure headline (authoritative-drive). Backend supplies
//      failure_reason / stopped_at; we only map stopped stage to a short label. ----
export const STAGE_STOPPED_LABEL = {
  gating:   'Blocked by a safety check',
  payment:  'Payment could not be completed',
  provider: 'The payment service was unavailable',
}
