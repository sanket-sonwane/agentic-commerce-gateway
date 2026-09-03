import { describe, it, expect } from 'vitest'
import { explainPayment, mapPaymentState, PAYMENT_UI } from './paymentState.js'
import { paymentBucket } from './terminology.js'

/**
 * Phase 10 — "One transaction must tell one payment story."
 *
 * Every surface renders from explainPayment(). These pin the exact mapping so
 * a single authoritative payload can never yield contradictory copy (e.g. one
 * surface claiming "nothing was charged" while another shows an authorized
 * payment). "Nothing was charged" may ONLY appear when the backend proves no
 * payment object exists at the gateway.
 */

function session(status, payment = {}) {
  return { status, payment }
}

describe('mapPaymentState', () => {
  it('maps completed -> COMPLETED', () => {
    expect(mapPaymentState('completed')).toBe(PAYMENT_UI.COMPLETED)
  })
  it('maps declined -> DECLINED', () => {
    expect(mapPaymentState('declined')).toBe(PAYMENT_UI.DECLINED)
  })
  it('maps payment_pending -> PAYMENT_PENDING', () => {
    expect(mapPaymentState('payment_pending')).toBe(PAYMENT_UI.PAYMENT_PENDING)
  })
  it('requires_payment + awaiting_authorization -> AUTHORIZATION_REQUIRED', () => {
    expect(mapPaymentState(session('requires_payment', { awaiting_authorization: true })))
      .toBe(PAYMENT_UI.AUTHORIZATION_REQUIRED)
  })
  it('requires_payment without awaiting_authorization -> PAYMENT_REQUIRED', () => {
    expect(mapPaymentState(session('requires_payment', {}))).toBe(PAYMENT_UI.PAYMENT_REQUIRED)
  })
  it('maps cancelled -> CANCELLED', () => {
    expect(mapPaymentState('cancelled')).toBe(PAYMENT_UI.CANCELLED)
  })
})

describe('explainPayment — nothing was charged is evidence-gated', () => {
  it('completed/verified never says nothing was charged', () => {
    const e = explainPayment(session('completed', { payment_id: 'pay_1' }), { payment: { status: 'verified', payment_id: 'pay_1' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.statusText).toBe('Paid')
    expect(e.paymentStatus).toBe('Payment confirmed and verified')
  })

  it('payment_pending with a payment_id (authorized) never says nothing was charged', () => {
    const e = explainPayment(session('payment_pending', { payment_id: 'pay_1' }), { payment: { status: 'authorized', payment_id: 'pay_1' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.statusText).toBe('Payment in progress')
    expect(e.paymentStatus).toBe('Payment authorized — awaiting capture')
  })

  it('payment_pending with pending + payment_id never says nothing was charged', () => {
    const e = explainPayment(session('payment_pending', { payment_id: 'pay_1' }), { payment: { status: 'pending', payment_id: 'pay_1' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.paymentStatus).toBe('Payment in progress')
  })

  it('payment_pending without payment_id stays neutral', () => {
    // No payment object yet -> we must not claim a charge happened, but we also
    // must not claim "nothing was charged" definitively. helper reports false.
    const e = explainPayment(session('payment_pending', {}), { payment: { status: 'pending' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.statusText).toBe('Payment initiated')
  })

  it('requires_payment + order_created + no payment_id -> payment required, nothing charged', () => {
    const e = explainPayment(session('requires_payment', {}), { payment: { status: 'order_created' } })
    expect(e.nothingCharged).toBe(true)
    expect(e.statusText).toBe('Payment required')
  })

  it('AUTHORIZATION_REQUIRED + order_created + no payment_id -> ready, nothing charged', () => {
    const e = explainPayment(session('requires_payment', { awaiting_authorization: true }), { payment: { status: 'order_created' } })
    expect(e.state).toBe(PAYMENT_UI.AUTHORIZATION_REQUIRED)
    expect(e.nothingCharged).toBe(true)
    expect(e.statusText).toBe('Ready for your payment')
  })

  it('requires_payment with order_created + payment_id -> initiated, not nothing-charged', () => {
    const e = explainPayment(session('requires_payment', { payment_id: 'pay_1' }), { payment: { status: 'order_created', payment_id: 'pay_1' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.paymentStatus).toBe('Payment initiated')
  })

  it('declined with no payment_id -> nothing was charged', () => {
    const e = explainPayment(session('declined', {}), { payment: { status: 'declined' } })
    expect(e.nothingCharged).toBe(true)
    expect(e.statusText).toBe('Declined — nothing was charged')
  })

  it('declined with a payment_id -> never says nothing was charged', () => {
    const e = explainPayment(session('declined', { payment_id: 'pay_1' }), { payment: { status: 'declined', payment_id: 'pay_1' } })
    expect(e.nothingCharged).toBe(false)
    expect(e.paymentStatus).toBe('Payment was declined')
  })

  it('cancelled with no payment_id -> nothing was charged', () => {
    const e = explainPayment(session('cancelled', {}), { payment: { status: 'not_called' } })
    expect(e.nothingCharged).toBe(true)
    expect(e.statusText).toBe('Cancelled — nothing was charged')
  })

  it('not_called always nothing was charged', () => {
    const e = explainPayment(session('requires_payment', {}), { payment: { status: 'not_called' } })
    expect(e.nothingCharged).toBe(true)
  })
})

describe('paymentBucket — authorized/pending+id map to AUTHORIZED', () => {
  it('authorized status -> AUTHORIZED', () => {
    expect(paymentBucket({ status: 'authorized', razorpay_called: true })).toBe('AUTHORIZED')
  })
  it('pending with payment_id -> AUTHORIZED', () => {
    expect(paymentBucket({ status: 'pending', payment_id: 'pay_1' })).toBe('AUTHORIZED')
  })
  it('pending without payment_id -> PENDING', () => {
    expect(paymentBucket({ status: 'pending' })).toBe('PENDING')
  })
  it('verified -> VERIFIED', () => {
    expect(paymentBucket({ status: 'verified' })).toBe('VERIFIED')
  })
  it('declined -> DECLINED', () => {
    expect(paymentBucket({ status: 'declined' })).toBe('DECLINED')
  })
  it('order_created + no payment_id + called -> ATTEMPTED', () => {
    expect(paymentBucket({ status: 'order_created', razorpay_called: true })).toBe('ATTEMPTED')
  })
  it('order_created + no payment_id + not called -> NOT_ATTEMPTED', () => {
    expect(paymentBucket({ status: 'order_created', razorpay_called: false })).toBe('NOT_ATTEMPTED')
  })
  it('order_created + payment_id -> AUTHORIZED', () => {
    expect(paymentBucket({ status: 'order_created', payment_id: 'pay_1' })).toBe('AUTHORIZED')
  })
  it('not_called -> NOT_ATTEMPTED', () => {
    expect(paymentBucket({ status: 'not_called' })).toBe('NOT_ATTEMPTED')
  })
})
