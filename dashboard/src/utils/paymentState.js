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
