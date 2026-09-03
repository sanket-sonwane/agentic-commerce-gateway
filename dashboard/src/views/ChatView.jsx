import { useEffect, useMemo, useRef, useState } from 'react'
import { SendHorizontal, ArrowDown, Soup, Croissant, ShoppingBasket, Eye } from 'lucide-react'
import { streamSSE, registerUserKey, merchantFetch } from '../api.js'
import { toViewItems, orderTitle } from '../utils/events.js'
import { mapPaymentState } from '../utils/paymentState.js'
import MessageBubble from '../components/MessageBubble.jsx'
import WorkingShimmer from '../components/WorkingShimmer.jsx'
import QuickReplyChips from '../components/QuickReplyChips.jsx'
import PaymentCard from '../components/PaymentCard.jsx'
import MoneyStoryDrawer from '../components/MoneyStoryDrawer.jsx'

const SUGGESTIONS = [
  { icon: Soup, text: 'Buy ingredients for chicken biryani for four people under \u20B9800' },
  { icon: Croissant, text: 'Get milk, bread and eggs for under \u20B9300' },
  { icon: ShoppingBasket, text: 'Order pasta dinner ingredients under \u20B9600' },
]

export default function ChatView({ onRequestOps, onSessionObserved }) {
  const [events, setEvents] = useState([])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState(null)
  const [pendingBill, setPendingBill] = useState(null)
  const [pendingSub, setPendingSub] = useState(null)
  const [busy, setBusy] = useState(false)
  const [showFab, setShowFab] = useState(false)
  const [explainSession, setExplainSession] = useState(null)
  const scrollRef = useRef(null)
  const taRef = useRef(null)
  const stickBottom = useRef(true)

  const viewItems = useMemo(() => toViewItems(events, { busy }), [events, busy])

  useEffect(() => {
    if (stickBottom.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    setShowFab(!stickBottom.current && events.length > 0)
  }, [viewItems, busy])

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 130)}px`
  }, [input])

  function applyEvent(e) {
    e.time = Date.now()
    setEvents(prev => [...prev, e])
    if (e.type === 'bill') {
      setPendingBill(e)
      onSessionObserved?.({
        checkout_session_id: e.checkout_session_id,
        amount: e.totals?.total,
        description: orderTitle(e.line_items || []),
        updated_at: Date.now(),
      })
    } else if (e.type === 'status') {
      onSessionObserved?.({
        checkout_session_id: e.checkout_session_id,
        status: e.status,
        payment: e.payment,
        updated_at: Date.now(),
      })
    } else if (e.type === 'substitute_proposal') {
      setPendingSub(e)
    }
  }

  async function send(text) {
    if (!text.trim() || busy) return
    setInput('')
    stickBottom.current = true
    setEvents(prev => [...prev, { type: 'user_message', content: text, time: Date.now() }])
    await runStream('/chat', { message: text, conversation_id: conversationId })
  }

async function decide(approved) {
    if (!pendingBill || busy) return
    const bill = pendingBill
    setPendingBill(null)
    setEvents(prev => prev.map(ev => (ev === bill ? { ...ev, resolved: approved ? 'approved' : 'cancelled' } : ev)))
    let body = { approved }
    if (approved) {
      // Workstream E: approve ⇒ the browser signs a user-intent assertion with
      // its own device key (private key never leaves localStorage). The public
      // key is registered once so the merchant can verify consent before money
      // movement; a rejected signature blocks the whole transaction.
      setBusy(true)
      try {
        const me = await import('../lib/userAssertion.js')
        const keypair = await me.getOrCreateKeypair()
        const pem = await me.spkiPemFromPublicKey(keypair.publicKey)
        const keyId = await me.keyIdFromPem(pem)
        const ua = await me.buildUserAssertion({
          checkoutSessionId: bill.checkout_session_id,
          totalPaise: Math.round((bill.totals?.total ?? 0) * 100),
          description: `${bill.line_items?.length ?? 0} items from your shopping agent`,
          merchantId: bill.merchant_id,
          delegateId: bill.delegate_id,
        })
        const reg = await registerUserKey({ user_id: ua.user_id, key_id: keyId, public_key_pem: pem })
        if (!reg.ok) throw new Error(reg.body?.detail || `Key registration failed (${reg.status})`)
        body.user_assertion = ua
        setBusy(false)
      } catch (err) {
        // Signing failed → nothing was sent: the order is NOT cancelled and the
        // bill stays pending server-side. Restore it so the user can re-approve.
        setBusy(false)
        setPendingBill(bill)
        setEvents(prev => prev.map(ev => (ev === bill ? { ...ev, resolved: undefined } : ev)))
        setEvents(prev => [...prev, {
          type: 'message',
          role: 'system',
          content: `⚠️ I couldn't sign your approval on this device (${err.message}). Nothing was sent — please try again.`,
          time: Date.now(),
        }])
        return
      }
    }
    await runStream(`/conversations/${conversationId}/decision`, body)
  }

  async function choose(choice) {
    if (!pendingSub || busy) return
    const sub = pendingSub
    setPendingSub(null)
    setEvents(prev =>
      prev.map(ev => (ev === sub ? { ...ev, resolved: choice === 'cancel' ? 'cancelled' : choice === 'other' ? 'other' : 'chosen' } : ev)),
    )
    await runStream(`/conversations/${conversationId}/choose`, { choice })
  }

  async function runStream(path, body) {
    setBusy(true)
    try {
      await streamSSE(path, body, e => {
        if (e.type === 'conversation_started') {
          setConversationId(e.conversation_id)
          return
        }
        applyEvent(e)
      })
    } finally {
      setBusy(false)
    }
  }

  function paymentPaid(body) {
    const s = body.checkout_session
    const state = mapPaymentState(s)
    onSessionObserved?.({
      checkout_session_id: s.checkout_session_id,
      status: s.status,
      amount: s.totals?.total,
      description: orderTitle(s.line_items || []),
      updated_at: Date.now(),
    })
    setEvents(prev => [
      ...prev,
      { type: 'status', checkout_session_id: s.checkout_session_id, status: s.status, payment: s.payment, time: Date.now() },
      ...(state === 'COMPLETED'
        ? [{ type: 'message', content: `✅ Order placed! You paid **₹${s.totals.total}**.`, time: Date.now() }]
        : []),
    ])
  }

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
    setShowFab(!stickBottom.current && events.length > 0)
  }

  const blockedReason = pendingBill
    ? 'Answer the order above to continue'
    : pendingSub
      ? 'Pick an option above to continue'
      : null

  return (
    <div className="chat-wrap">
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="chat-inner">
          {events.length === 0 ? (
            <>
              <div className="hero">
                <div className="hero-mark"><ShoppingBasket size={26} /></div>
                <h2>Shop with your AI agent.</h2>
                <p>Your agent finds what you need. You approve before money moves.</p>
                <div className="sugg-grid">
                  {SUGGESTIONS.map(s => (
                    <button key={s.text} className="sugg-card" onClick={() => send(s.text)}>
                      <span className="si"><s.icon size={16} /></span>
                      <span>{s.text}</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="trust-line">Every order needs your approval · Money only moves after you pay</div>
            </>
          ) : null}

          {viewItems.map(item => (
            <ViewItem
              key={item.id}
              item={item}
              busy={busy}
              onDecide={decide}
              onChoose={choose}
              onRequestOps={onRequestOps}
              onWhy={setExplainSession}
              onPaid={paymentPaid}
            />
          ))}

          {explainSession && (
            <MoneyStoryDrawer sessionId={explainSession} onClose={() => setExplainSession(null)} />
          )}
        </div>
      </div>

      {showFab && (
        <button className="scroll-fab" onClick={() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })}>
          <ArrowDown size={15} />
        </button>
      )}

      <div className="input-dock-wrap">
        {blockedReason && !busy && <div className="blocked-hint">↑ {blockedReason}</div>}
        <div className="input-dock">
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder={blockedReason ? blockedReason + '\u2026' : 'What would you like to order?'}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send(input)
              }
            }}
            disabled={!!blockedReason}
            aria-label="Message your shopping assistant"
          />
          <button className="send-btn" onClick={() => send(input)} disabled={busy || !input.trim()} title="Send">
            <SendHorizontal size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------ view-item renderer */

function ViewItem({ item, busy, onDecide, onChoose, onRequestOps, onWhy, onPaid }) {
  switch (item.kind) {
    case 'user':
      return <MessageBubble role="user" content={item.text} />
    case 'ai':
      return <MessageBubble role="ai" content={item.text} />
    case 'working':
      return <WorkingShimmer label={item.label} />
    case 'activity':
      return (
        <details className="activity">
          <summary>agent activity ({item.labels.length} step{item.labels.length > 1 ? 's' : ''})</summary>
          <ul>{item.labels.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </details>
      )
    case 'order':
      return <OrderReady item={item} busy={busy} onDecide={onDecide} onRequestOps={onRequestOps} onWhy={onWhy} />
    case 'swap':
      return <SwapOffer item={item} busy={busy} onChoose={onChoose} />
    case 'payment':
      return <PaymentSlot ev={item.ev} onPaid={onPaid} />
    case 'caption':
      return <Caption item={item} onRequestOps={onRequestOps} onWhy={onWhy} />
    case 'error':
      return <div className="alert-line anim-in">⚠ {item.text}</div>
    default:
      return null
  }
}

/* ---------------------------------------------- order-ready (conversational bill) */

function OrderReady({ item, busy, onDecide, onRequestOps, onWhy }) {
  const { ev, resolved } = item
  const items = ev.line_items || []
  const t = ev.totals || {}
  const [safety, setSafety] = useState(null)

  useEffect(() => {
    let alive = true
    if (!ev.checkout_session_id) return
    merchantFetch(`/consumer/transactions/${ev.checkout_session_id}/provenance`)
      .then(r => {
        if (!alive || !r.ok || !r.body) return
        const byName = {}
        for (const c of r.body.checks || []) byName[c.name] = c.status
        setSafety({
          spendPass: byName['Spend limit'] === 'pass',
          stockPass: byName['Stock'] === 'pass',
          authVerified: !!r.body.authorization?.verified,
        })
      })
      .catch(() => {})
    return () => { alive = false }
  }, [ev.checkout_session_id])

  const gated = ok => (ok ? 'pass' : safety ? 'fail' : 'pending')

  return (
    <div className="msg-row ai">
      <div className="avatar"><Basket /></div>
      <div className="msg-body">
        <div className="bubble ai-tail order-bubble">
          <p style={{ margin: '0 0 4px' }}>Your order is ready:</p>
          <ul className="items">
            {items.map(li => (
              <li key={li.pack_id}>
                <span><b>{li.name}</b>{li.quantity > 1 ? ` ×${li.quantity}` : ''}</span>
                <span>₹{li.unit_price * li.quantity}</span>
              </li>
            ))}
          </ul>
          {!!t.shipping && (
            <div className="items"><li style={{ display: 'flex', justifyContent: 'space-between', width: '100%' }}><span>Delivery</span><span>₹{t.shipping}</span></li></div>
          )}
          <div className="total-line"><span>Total</span><span>₹{t.total}</span></div>
          <div className="safety-summary">
            <span className={`safety-item ${gated(safety?.spendPass)}`}>Within your spending limit</span>
            <span className={`safety-item ${gated(safety?.stockPass)}`}>Items in stock</span>
            <span className={`safety-item ${gated(safety?.authVerified)}`}>Your agent is authorized</span>
          </div>
          <div className="why-row">
            <button className="why-link" onClick={() => onWhy?.(ev.checkout_session_id)}>
              <Eye size={12} /> Why this total?
            </button>
            <button className="why-link" onClick={() => onRequestOps?.(ev.checkout_session_id)}>
              <Eye size={12} /> Full trail
            </button>
          </div>
          <QuickReplyChips
            choices={[
              { value: 'approved', label: `\u2713 Place order \u00B7 \u20B9${t.total}`, tone: 'primary', onClick: () => onDecide(true), pickedLabel: '\u2713 Ordered' },
              { value: 'cancelled', label: 'Cancel', tone: 'danger', onClick: () => onDecide(false), pickedLabel: 'Cancelled' },
            ]}
            disabled={busy}
            resolved={resolved}
          />
        </div>
        <div className="msg-meta">{orderTitle(items)} · needs your approval</div>
      </div>
    </div>
  )
}

/* ---------------------------------------------- substitution offer */

function SwapOffer({ item, busy, onChoose }) {
  const { ev, resolved } = item
  const prop = ev.proposal?.proposal || {}
  const orig = ev.proposal?.original || {}
  const save = (orig.price || 0) - (prop.price || 0)
  return (
    <div className="msg-row ai">
      <div className="avatar"><Swap /></div>
      <div className="msg-body">
        <div className="bubble ai-tail order-bubble swap-bubble">
          <p style={{ margin: '0 0 2px' }}>
            {(ev.original_name || prop.name)} isn't available right now{ev.proposal?.reason_code === 'requested_pack_unavailable' ? '' : ''}.
          </p>
          <div className="swap-line">
            <span className="swap-old">{orig.pack_id?.replace('pack_', '')}</span>
            <span className="swap-arrow">→</span>
            <b>{prop.name} {prop.size}{prop.unit} ×{prop.quantity}</b>
            <span>₹{prop.line_total ?? prop.price}</span>
            {save > 0 && <span className="save-badge">save ₹{save}</span>}
          </div>
          <QuickReplyChips
            choices={[
              { value: 'chosen', label: '\u2713 Take it', tone: 'primary', onClick: () => onChoose(0), pickedLabel: '\u2713 Taken' },
              { value: 'other', label: 'Other options', onClick: () => onChoose('other'), pickedLabel: 'Skipped' },
              { value: 'cancelled', label: 'Cancel', tone: 'danger', onClick: () => onChoose('cancel'), pickedLabel: 'Cancelled' },
            ]}
            disabled={busy}
            resolved={resolved}
          />
        </div>
      </div>
    </div>
  )
}

/* ---------------------------------------------- humanized caption */

function Caption({ item, onRequestOps, onWhy }) {
  const { text, csid, payment } = item
  const explainable = /couldn|pending|cancelled/i.test(text)
  const paid = /paid|completed/i.test(text)
  const failed = /couldn|stopped|cancelled|nothing/i.test(text)
  return (
    <div className={`caption ${paid ? 'gold-line' : ''} ${failed ? 'red-line' : ''}`}>
      {text}
      {csid && paid && (
        <>
          {' \u00B7 '}
          <button className="why-link" onClick={() => onWhy?.(csid)}>view proof</button>
          {' \u00B7 '}
          <button className="why-link" onClick={() => onRequestOps?.(csid)}>full trail</button>
        </>
      )}
      {csid && explainable && (
        <>
          {' \u00B7 '}
          <button className="why-link" onClick={() => onWhy?.(csid)}>what happened?</button>
        </>
      )}
      {payment?.saved_card && <> · 💳 {payment.saved_card.network} ••••{payment.saved_card.last4}</>}
    </div>
  )
}

function Swap() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M16 3l5 5-5 5M21 8H9M8 21l-5-5 5-5M3 16h12" />
    </svg>
  )
}
function Basket() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4zM3 6h18M16 10a4 4 0 01-8 0" />
    </svg>
  )
}

/* ---------------------------------------------- payment slot (the money card) */

function PaymentSlot({ ev, onPaid }) {
  return <div className="pay-card-wrapper"><PaymentCard redirect={ev} onPaid={onPaid} /></div>
}

