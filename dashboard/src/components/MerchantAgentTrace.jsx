import { useEffect, useMemo, useState } from 'react'
import { Bot, ShieldCheck, ShieldX, CircleHelp, CreditCard, BadgeCheck, UserCheck } from 'lucide-react'
import { merchantFetch } from '../api.js'

// Groups audit events by merchant agent session id.
export function useAgentSessions() {
  const [sessions, setSessions] = useState([])
  useEffect(() => {
    let alive = true
    async function load() {
      try {
        const { body } = await merchantFetch('/audit_logs?limit=400')
        if (!alive) return
        const map = new Map()
        for (const e of body.events || []) {
          const sid = e.payload?.agent_session_id
          if (!sid) continue
          if (!map.has(sid)) map.set(sid, { agent_session_id: sid, first: e, last: e, count: 0 })
          const s = map.get(sid)
          s.count += 1
          s.last = e
        }
        setSessions([...map.values()].sort((a, b) => b.last.seq - a.last.seq))
      } catch { /* merchant down */ }
    }
    load()
    const t = setInterval(load, 2500)
    return () => { alive = false; clearInterval(t) }
  }, [])
  return sessions
}

const STATUS_COLORS = {
  completed: 'completed',
  awaiting_buyer_confirmation: 'requires_confirmation',
  ready_for_completion: 'requires_payment',
  declined: 'declined',
  unable_to_fulfill: 'declined',
  awaiting_payment_authorization: 'payment_pending',
}

function previewText(ev) {
  const raw = ev.payload?.result?.preview
  if (!raw) return ''
  try {
    const parsed = JSON.parse(raw)
    return typeof parsed === 'string' ? parsed : JSON.stringify(parsed)
  } catch {
    return String(raw)
  }
}

// The §39-style decision trace for one agent session.
export function AgentTraceDetail({ agentSessionId }) {
  const [session, setSession] = useState(null)
  const [events, setEvents] = useState([])

  useEffect(() => {
    let alive = true
    async function load() {
      const s = await merchantFetch(`/agent_sessions/${agentSessionId}`)
      if (alive && s.ok) setSession(s.body)
      const t = await merchantFetch(`/agent_sessions/${agentSessionId}/trace`)
      if (alive && t.ok) setEvents(t.body.events || [])
    }
    load()
    const timer = setInterval(load, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [agentSessionId])

  const policyEvent = useMemo(
    () => [...events].reverse().find(e => ['gating_evaluated', 'final_validation'].includes(e.action) && e.payload?.checks),
    [events],
  )
  const paid = events.some(e => e.action === 'payment_success')
  const razorpayTouched = events.some(e => e.action.startsWith('razorpay'))
  const buyerDecision = events.find(e => e.action === 'buyer_proposal_decision')
  const agentActions = events.filter(e => e.action.startsWith('agent_') && e.action !== 'agent_outcome' && e.action !== 'agent_reasoning_only')

  if (!session) return <div className="sysline">Loading agent session…</div>
  const outcome = session.outcome || {}
  const proposal = session.proposal

  return (
    <>
      <div className="ops-col">
        <h3><Bot size={13} /> Merchant Agent</h3>
        <div className="explain-box" style={{ marginBottom: 12 }}>
          <div className="headline">“{session.buyer_request}”</div>
          <div className="kv"><b>session</b> {session.agent_session_id}</div>
          <div className="kv"><b>status</b> {String(session.status).replace(/_/g, ' ')}</div>
          <div className="kv"><b>budget used</b> {session.iterations} iterations · {session.tool_calls} tool calls</div>
          {outcome.message && <div className="kv"><b>outcome</b> {outcome.message}</div>}
          {outcome.reason_code && <div className="kv mono">{outcome.reason_code}</div>}
        </div>

        {proposal && (
          <div className="explain-box">
            <h3><UserCheck size={13} /> Proposal awaiting buyer</h3>
            <div className="kv"><b>reason</b> {proposal.reason_code}</div>
            {proposal.original?.pack_id && (
              <div className="kv"><b>original</b> {proposal.original.pack_id} ×{proposal.original.quantity}</div>
            )}
            {proposal.proposal?.name && (
              <div className="kv"><b>proposed</b> {proposal.proposal.name} ({proposal.proposal.size}{proposal.proposal.unit}) ×{proposal.proposal.quantity} — ₹{proposal.proposal.line_total ?? proposal.proposal.total}</div>
            )}
            {proposal.message && <div className="kv" style={{ color: 'var(--muted)' }}>{proposal.message}</div>}
          </div>
        )}
      </div>

      <div className="ops-col">
        <h3>Decision trace</h3>
        <div className="stepper">
          {agentActions.map(ev => {
            const isDecline = ev.decision === 'declined'
            return (
              <div key={ev.seq} className={`step ${ev.decision || ''}`}>
                <span className="step-dot">{isDecline ? <ShieldX size={14} /> : <CircleHelp size={14} />}</span>
                <div className="step-body">
                  <div className="step-title">
                    <span>{(ev.payload?.tool || ev.action).replace(/_/g, ' ')}</span>
                    <span className="step-time">it.{ev.payload?.iteration}</span>
                  </div>
                  {ev.reasoning_summary && <div className="step-reason">{ev.reasoning_summary}</div>}
                  {ev.payload?.result?.preview && (
                    <div className="step-reason mono" style={{ opacity: 0.75 }}>
                      {previewText(ev).slice(0, 220)}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
          {buyerDecision && (
            <div className={`step ${buyerDecision.decision}`}>
              <span className="step-dot"><UserCheck size={14} /></span>
              <div className="step-body">
                <div className="step-title"><span>buyer response</span></div>
                <div className="step-reason">
                  {buyerDecision.payload?.accepted ? 'ACCEPT' : 'REJECT'} — {buyerDecision.reasoning_summary}
                </div>
              </div>
            </div>
          )}
          {!agentActions.length && !buyerDecision && <div className="sysline">No agent actions yet.</div>}
        </div>
      </div>

      <div className="ops-col" style={{ borderRight: 'none' }}>
        <h3><ShieldCheck size={13} /> Policy</h3>
        {policyEvent ? (
          <div className="policy-grid">
            {(policyEvent.payload.checks || []).map(c => (
              <div key={c.rule} className={`policy-row ${c.result}`}>
                <span className="pi">{c.result === 'pass' ? <ShieldCheck size={15} /> : c.result === 'fail' ? <ShieldX size={15} /> : <CircleHelp size={15} />}</span>
                <span className="pn">{c.rule.replace(/_/g, ' ')}</span>
                <span className="pd">{c.detail}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="sysline">No deterministic evaluation yet.</div>
        )}

        <h3><CreditCard size={13} /> Payment</h3>
        <div className="kv">
          <b>Razorpay invoked</b>{' '}
          <span className={razorpayTouched ? 'intact-yes' : 'intact-no'}>{razorpayTouched ? 'YES' : 'NO'}</span>
        </div>
        {paid && (
          <div className="kv intact-yes"><BadgeCheck size={13} style={{ verticalAlign: -2 }} /> Paid via gated completion ✓</div>
        )}
        {!paid && razorpayTouched && <div className="kv">awaiting authorization / pending</div>}
        {!paid && !razorpayTouched && session.status !== 'completed' && (
          <div className="kv" style={{ color: 'var(--muted)' }}>No payment attempted.</div>
        )}
      </div>
    </>
  )
}

export default function MerchantAgentSection({ selected, onSelect }) {
  const sessions = useAgentSessions()
  if (!sessions.length) {
    return (
      <>
        <h3><Bot size={13} /> Merchant Agent</h3>
        <div className="sysline">No agent sessions yet.</div>
      </>
    )
  }
  return (
    <>
      <h3><Bot size={13} /> Merchant Agent</h3>
      {sessions.map(s => (
        <div
          key={s.agent_session_id}
          className={`session-item ${selected === s.agent_session_id ? 'selected' : ''}`}
          onClick={() => onSelect(s.agent_session_id)}
        >
          <div className="top">
            <span className="sid mono">{s.agent_session_id}</span>
          </div>
          <span className={`badge ${STATUS_COLORS[s.last.payload?.status || ''] || ''}`} style={{ marginTop: 6, display: 'inline-block', fontSize: 10 }}>
            {(s.last.payload?.reason_code || s.last.payload?.tool || '').replace(/_/g, ' ').slice(0, 40)}
          </span>
        </div>
      ))}
    </>
  )
}
