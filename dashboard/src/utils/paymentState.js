/**
 * SINGLE deterministic mapping from authoritative backend payment state to UI
 * state. The backend (CheckoutSessionOut.status, plus the derived
 * payment.awaiting_authorization flag) is the ONLY source of truth for money
 * state. The UI must never infer "completed", "declined" or amounts on its own.
 *
 * Input:   the authoritative session object from /checkout_sessions/{id} or the
 *          /payment_callback response (shape { status, payment: { ... } }).
 * Output:  a UI payment-state constant + a human label.
 */

export const PAYMENT_UI = {
  COMPLETED: 'COMPLETED',
  DECLINED: 'DECLINED',
  PAYMENT_PENDING: 'PAYMENT_PENDING',
  AUTHORIZATION_REQUIRED: 'AUTHORIZATION_REQUIRED',
  PAYMENT_REQUIRED: 'PAYMENT_REQUIRED',
  CANCELLED: 'CANCELLED',
  UNKNOWN: 'UNKNOWN',
}

export const PAYMENT_LABEL = {
  [PAYMENT_UI.COMPLETED]: 'Paid',
  [PAYMENT_UI.DECLINED]: 'Declined',
  [PAYMENT_UI.PAYMENT_PENDING]: 'Payment pending',
  [PAYMENT_UI.AUTHORIZATION_REQUIRED]: 'Authorization required',
  [PAYMENT_UI.PAYMENT_REQUIRED]: 'Payment required',
  [PAYMENT_UI.CANCELLED]: 'Cancelled',
  [PAYMENT_UI.UNKNOWN]: 'Payment status unknown',
}

/**
 * Map the backend checkout session to one UI payment state.
 *
 *   completed            -> COMPLETED
 *   declined             -> DECLINED
 *   payment_pending      -> PAYMENT_PENDING
 *   requires_payment     -> AUTHORIZATION_REQUIRED  (when awaiting_authorization)
 *                       -> PAYMENT_REQUIRED          (otherwise)
 *   cancelled            -> CANCELLED
 *   anything else        -> UNKNOWN
 *
 * awaiting_authorization is the only backend-derived concept used here, and it
 * is read from the authoritative payment payload, never invented by the UI.
 */
export function mapPaymentState(sessionOrStatus, payment = {}) {
  let status = sessionOrStatus
  if (sessionOrStatus !== null && typeof sessionOrStatus === 'object') {
    status = sessionOrStatus.status
    payment = sessionOrStatus.payment || payment
  }
  switch (status) {
    case 'completed':
      return PAYMENT_UI.COMPLETED
    case 'declined':
      return PAYMENT_UI.DECLINED
    case 'payment_pending':
      return PAYMENT_UI.PAYMENT_PENDING
    case 'requires_payment':
      return payment?.awaiting_authorization
        ? PAYMENT_UI.AUTHORIZATION_REQUIRED
        : PAYMENT_UI.PAYMENT_REQUIRED
    case 'cancelled':
      return PAYMENT_UI.CANCELLED
    default:
      return PAYMENT_UI.UNKNOWN
  }
}

export function paymentStateLabel(sessionOrStatus, payment = {}) {
  return PAYMENT_LABEL[mapPaymentState(sessionOrStatus, payment)]
}

/**
 * CANONICAL payment explanation — the SINGLE function every UI surface calls.
 *
 * Never says "nothing was charged" unless the backend proves it via:
 *   - payment.status === 'not_called'  (no Razorpay interaction)
 *   - payment.status === 'order_created' && !payment_id  (order exists but no payment)
 *   - session cancelled with no payment evidence
 *
 * The projection (from /consumer/transactions/{id}/provenance) is optional
 * enrichment. When absent, the helper falls back to session-level fields.
 *
 * @param {object} session — from /checkout_sessions/{id} or callback response
 * @param {object} [projection] — from /consumer/transactions/{id}/provenance
 * @returns {object} { state, label, nothingCharged, statusText, paymentStatus, detail }
 */
export function explainPayment(session, projection = {}) {
  const state = mapPaymentState(session, session?.payment || {})
  const pay = projection.payment || session?.payment || {}
  const paymentId = pay.payment_id
  const projStatus = pay.status

  // --- evidence-gated "nothing was charged" ---
  // Only when backend proves no money was attempted at the gateway.
  const nothingCharged = (
    projStatus === 'not_called' ||
    (projStatus === 'order_created' && !paymentId) ||
    (projStatus === 'declined' && !paymentId) ||
    (state === PAYMENT_UI.CANCELLED && !paymentId)
  )

  // --- statusText: short badge label ---
  let statusText
  switch (state) {
    case PAYMENT_UI.COMPLETED:
      statusText = 'Paid'
      break
    case PAYMENT_UI.DECLINED:
      statusText = nothingCharged ? 'Declined — nothing was charged' : 'Payment declined'
      break
    case PAYMENT_UI.PAYMENT_PENDING:
      statusText = paymentId ? 'Payment in progress' : 'Payment initiated'
      break
    case PAYMENT_UI.AUTHORIZATION_REQUIRED:
      statusText = 'Ready for your payment'
      break
    case PAYMENT_UI.PAYMENT_REQUIRED:
      statusText = nothingCharged ? 'Payment required' : 'Payment pending'
      break
    case PAYMENT_UI.CANCELLED:
      statusText = nothingCharged ? 'Cancelled — nothing was charged' : 'Cancelled'
      break
    default:
      statusText = 'Payment status unknown'
  }

  // --- paymentStatus: copy for the payment section ---
  let paymentStatus
  switch (projStatus) {
    case 'verified':
      paymentStatus = 'Payment confirmed and verified'
      break
    case 'authorized':
      paymentStatus = 'Payment authorized — awaiting capture'
      break
    case 'declined':
      paymentStatus = paymentId ? 'Payment was declined' : 'Payment was declined — nothing was charged'
      break
    case 'pending':
      paymentStatus = paymentId ? 'Payment in progress' : 'Payment is being processed'
      break
    case 'order_created':
      paymentStatus = paymentId ? 'Payment initiated' : 'Payment not yet attempted'
      break
    case 'not_called':
      paymentStatus = 'Payment was not attempted'
      break
    default:
      paymentStatus = 'Payment status unknown'
  }

  // --- detail: longer explanation for "what happened" sections ---
  let detail
  if (state === PAYMENT_UI.COMPLETED) {
    detail = 'Your payment was confirmed and the order is placed.'
  } else if (state === PAYMENT_UI.DECLINED) {
    detail = nothingCharged
      ? 'The payment was declined. Nothing was charged.'
      : 'The payment was declined.'
  } else if (state === PAYMENT_UI.PAYMENT_PENDING) {
    detail = paymentId
      ? 'Your payment is being processed at the gateway.'
      : 'The payment order has been created and is awaiting authorization.'
  } else if (state === PAYMENT_UI.AUTHORIZATION_REQUIRED) {
    detail = 'Your agent built the order and is waiting for you to pay.'
  } else if (state === PAYMENT_UI.PAYMENT_REQUIRED) {
    detail = nothingCharged
      ? 'The order is ready. No payment has been attempted.'
      : 'The order is ready and waiting for payment.'
  } else if (state === PAYMENT_UI.CANCELLED) {
    detail = nothingCharged ? 'The order was cancelled. Nothing was charged.' : 'The order was cancelled.'
  } else {
    detail = 'Payment status is being determined.'
  }

  return { state, label: PAYMENT_LABEL[state], nothingCharged, statusText, paymentStatus, detail }
}
