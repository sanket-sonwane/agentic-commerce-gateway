import { useMemo, useState } from 'react'
import { metaFor, actorColor, deriveStages } from '../utils/actionMeta.js'

/**
 * Audit timeline v2 — actor-colored rails, human titles with payload chips,
 * verdict stepper header, filters, and grouped agent reasoning.
 */

const FILTERS = [
  { key: 'all', label: 'All' },
  { key: 'money', label: 'Money' },
  { key: 'agent', label: 'Agent' },
  { key: 'system', label: 'System' },
]

export default function AuditTimeline({ events = [], limit = 250, stepper = true, compact = false }) {
  const [filter, setFilter] = useState('all')

  // Group consecutive agent_* events into one collapsible block.
  const blocks = useMemo(() => {
    let list = events
    if (filter !== 'all') {
      list = events.filter(e => {
        const kind = metaFor(e.action).kind
        if (filter === 'money') return kind === 'money'
        if (filter === 'agent') return kind === 'agent'
        return kind === 'system' || kind === 'buyer'
      })
    }
    if (compact) return list.slice(-12).map(e => ({ type: 'event', ev: e }))

    const out = []
    for (const e of list) {
      const isAgent = e.actor === 'merchant_agent' && e.action.startsWith('agent_')
      if (isAgent) {
        const last = out[out.length - 1]
        if (last?.type === 'agent-block') last.items.push(e)
        else out.push({ type: 'agent-block', items: [e], first: e })
      } else {
        out.push({ type: 'event', ev: e })
      }
    }
    return out.slice(-limit)
  }, [events, filter, limit, compact])

  const stages = useMemo(() => (stepper ? deriveStages(events) : null), [events, stepper])

  return (
    <div>
      {stages && (
        <div className="story-stepper" style={{ marginBottom: 12 }}>
          {stages.map(s => (
            <div key={s.key} className={`story-stage ${s.done ? 'done' : ''} ${s.outcome ? `outcome ${s.label.toLowerCase()}` : ''}`}>
              <span className="stage-dot" />
              <span className="stage-label">{s.label}</span>
            </div>
          ))}
        </div>
      )}

      {!compact && (
        <div className="tl-filters">
          {FILTERS.map(f => (
            <button key={f.key} className={`tl-filter ${filter === f.key ? 'on' : ''}`} onClick={() => setFilter(f.key)}>
              {f.label}
            </button>
          ))}
        </div>
      )}

      <div className="vtl">
        {blocks.map((b, i) => {
          if (b.type === 'agent-block') {
            return (
              <details key={`ab-${i}`} className="vtl agent-block">
                <summary>
                  <span className="ab-rail" style={{ background: actorColor('merchant_agent') }} />
                  Agent reasoning ({b.items.length})
                </summary>
                <div style={{ paddingLeft: 8 }}>
                  {b.items.map(ev => <Row key={ev.seq} ev={ev} />)}
                </div>
              </details>
            )
          }
          return <Row key={b.ev.seq} ev={b.ev} />
        })}
        {!blocks.length && <div className="muted">No matching events.</div>}
      </div>
    </div>
  )
}

function Row({ ev }) {
  const meta = metaFor(ev.action)
  const color = actorColor(ev.actor)
  const chips = []
  if (meta.chips) {
    try { chips.push(...(meta.chips(ev.payload) || [])) } catch { /* defensive */ }
  }
  return (
    <div className={`vtl-item ${ev.decision || ''}`}>
      <span className="vtl-dot actor-rail" style={{ borderColor: color, color }}>
        {meta.kind === 'money' ? '₹' : '•'}
      </span>
      <div className="vtl-title">
        <span>
          {meta.title}
          <span className="actor-tag" style={{ color }}>{ev.actor}</span>
        </span>
        <span className="vtl-time">{new Date(ev.timestamp + 'Z').toLocaleTimeString()}</span>
      </div>
      {!!chips.length && (
        <div className="chip-row">
          {chips.map((c, i) => (
            <span key={i} className={`payload-chip ${c.tone || ''} ${c.mono ? 'mono' : ''}`}>{c.label}</span>
          ))}
        </div>
      )}
      {ev.reasoning_summary && !chips.length && <div className="vtl-reason">{ev.reasoning_summary}</div>}
    </div>
  )
}
