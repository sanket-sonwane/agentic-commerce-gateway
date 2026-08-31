/**
 * LEGACY ALIAS — superseded by the canonical money card (PaymentCard.jsx).
 *
 * This file only re-exports the single authoritative payment card. It exists so
 * existing import sites (OpsView) keep working unchanged; it must NOT be
 * extended independently, to avoid a second, diverging copy of money-flow logic
 * and payment-state truth. See PaymentCard.jsx for the implementation.
 */
export { default } from './PaymentCard.jsx'
