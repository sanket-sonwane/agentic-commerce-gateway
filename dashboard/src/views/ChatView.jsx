import { useEffect, useRef, useState } from 'react'
import { SendHorizontal, ArrowDown, Soup, Croissant, ShoppingBasket, Search, ReceiptText, Ban } from 'lucide-react'
import { streamSSE } from '../api.js'
import MessageBubble from '../components/MessageBubble.jsx'
import ActivityChip, { FoundChip } from '../components/ActivityChip.jsx'
import BillCard from '../components/BillCard.jsx'
import SubstituteCard from '../components/SubstituteCard.jsx'
import StatusPill from '../components/StatusPill.jsx'
import RazorpayPayCard from '../components/RazorpayPayCard.jsx'

const TOOL_ICONS = {
  discover_products: Search,
  create_checkout_session: ReceiptText,
  cancel_checkout_session: Ban,
}

const SUGGESTIONS = [
  { icon: Soup, text: 'Buy ingredients for chicken biryani for four people under ₹800' },
  { icon: Croissant, text: 'Get milk, bread and eggs for under ₹300' },
  { icon: ShoppingBasket, text: 'Order pasta dinner ingredients under ₹600' },
]

export default function ChatView() {
  const [events, setEvents] = useState([])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState(null)
  const [pendingBill, setPendingBill] = useState(null)
  const [pendingSub, setPendingSub] = useState(null)
  const [busy, setBusy] = useState(false)
  const [showFab, setShowFab] = useState(false)
  const scrollRef = useRef(null)
  const taRef = useRef(null)
  const stickBottom = useRef(true)

  useEffect(() => {
    if (stickBottom.current) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
    setShowFab(!stickBottom.current && events.length > 0)
  }, [events, busy])

  useEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 130)}px`
  }, [input])

  function applyEvent(e) {
    e.time = Date.now()
    setEvents(prev => [...prev, e])
    if (e.type === 'bill') setPendingBill(e)
    else if (e.type === 'substitute_proposal') setPendingSub(e)
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
    await runStream(`/conversations/${conversationId}/decision`, { approved })
  }

  async function choose(choice) {
    if (!pendingSub || busy) return
    const sub = pendingSub
    setPendingSub(null)
    const resolvedTag = choice === 'cancel' ? 'cancelled' : choice === 'other' ? 'other' : 'chosen'
    setEvents(prev => prev.map(ev => (ev === sub ? { ...ev, resolved: resolvedTag } : ev)))
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
    setEvents(prev => [
      ...prev,
      { type: 'status', checkout_session_id: s.checkout_session_id, status: s.status, payment: s.payment, time: Date.now() },
      { type: 'message', content: `✅ Order placed! You paid **₹${s.totals.total}**. Receipt: \`${s.checkout_session_id}\``, time: Date.now() },
    ])
  }

  function onScroll() {
    const el = scrollRef.current
    if (!el) return
    stickBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
    setShowFab(!stickBottom.current && events.length > 0)
  }

  const blockedReason = pendingBill
    ? 'Answer the bill above to continue'
    : pendingSub
      ? 'Pick an option on the substitution card above'
      : null

  return (
    <div className="chat-wrap">
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        {events.length === 0 ? (
          <div className="hero">
            <div className="hero-mark"><ShoppingBasket size={28} /></div>
            <h2>Groceries, ordered by AI.</h2>
            <p>Describe what you need — your agent shops, and you approve the bill.</p>
            <div className="sugg-grid">
              {SUGGESTIONS.map(s => (
                <button key={s.text} className="sugg-card" onClick={() => send(s.text)}>
                  <span className="si"><s.icon size={17} /></span>
                  <span>{s.text}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="sysline">Every order needs your approval before any payment</div>
        )}

        {events.map((ev, i) => (
          <EventView key={i} ev={ev} onDecide={decide} busy={busy} onPaid={paymentPaid} onChoose={choose} />
        ))}

        {busy && (
          <div className="typing-dots"><i /><i /><i /></div>
        )}
      </div>

      {showFab && (
        <button className="scroll-fab" onClick={() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })}>
          <ArrowDown size={16} />
        </button>
      )}

      <div className="input-dock-wrap">
        {blockedReason && !busy && <div className="blocked-hint">↑ {blockedReason}</div>}
        <div className="input-dock">
          <textarea
            ref={taRef}
            rows={1}
            value={input}
            placeholder={blockedReason ? blockedReason + '…' : 'What would you like to order?'}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send(input)
              }
            }}
            disabled={!!blockedReason}
          />
          <button className="send-btn" onClick={() => send(input)} disabled={busy || !input.trim()} title="Send">
            <SendHorizontal size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}

function EventView({ ev, onDecide, busy, onPaid, onChoose }) {
  switch (ev.type) {
    case 'user_message':
      return <MessageBubble role="user" content={ev.content} ts={ev.time} />
    case 'message':
      return <MessageBubble role="ai" content={ev.content} ts={ev.time} />
    case 'tool':
      return <ActivityChip icon={TOOL_ICONS[ev.tool]} label={ev.summary || ev.tool} defaultOpen={false} />
    case 'found':
      return <FoundChip ev={ev} />
    case 'status':
      return <StatusPill ev={ev} />
    case 'error':
      return <div className="alert-line anim-in">⚠ {ev.content}</div>
    case 'bill':
      return <BillCard ev={ev} onDecide={onDecide} busy={busy} />
    case 'substitute_proposal':
      return <SubstituteCard ev={ev} onChoose={onChoose} busy={busy} />
    case 'bill_redirect':
      return <RazorpayPayCard redirect={ev} onPaid={onPaid} />
    default:
      return null
  }
}
