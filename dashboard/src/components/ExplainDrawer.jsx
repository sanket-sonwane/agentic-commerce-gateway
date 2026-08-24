import { useEffect, useState } from 'react'
import { X, ShieldCheck, ShieldX, CircleHelp } from 'lucide-react'
import { merchantFetch } from '../api.js'

const RULE_LABELS = {
  spend_limit: 'Spend limit',
  stock_check: 'Stock',
  category_allowed: 'Category',
  delegation_expiry: 'Delegation',
  quantity_limit: 'Quantity',
}

/**
 * Trust drawer: WHY did money move / not move?
 * Sections: plain answer (/explain) → policy checks (session.gating_result)
 *           → audit facts. Zero backend changes — existing endpoints only.
 */
export default function ExplainDrawer({ sessionId, onClose }) {
  const [explanation, setExplanation] = useState(null)
  const [checks, setChecks] = useState(null)

  useEffect(() => {
    let alive = true
    async function load() {
      const x = await merchantFetch(`/explain/${sessionId}`)
      if (alive && x.ok) setExplanation(x.body)
      const s = await merchantFetch(`/checkout_sessions/${sessionId}`)
      if (alive && s.ok) {
        const g = s.body.checkout_session?.gating_result
        setChecks(Array.isArray(g?.checks) ? g.checks : null)
      }
    }
    load()
    return () => { alive = false }
  }, [sessionId])

  useEffect(() => {
    const onKey = e => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <aside className="explain-drawer" role="dialog" aria-label="Why this happened">
        <div className="drawer-head">
          <h3>Why this happened</h3>
          <span className="mono" style={{ fontSize: 10.5, color: 'var(--faint)' }}>{sessionId}</span>
          <button className="icon-btn" onClick={onClose} title="Close"><X size={15} /></button>
        </div>
        <div className="drawer-body">
          <div className="drawer-answer">{explanation?.explanation || 'Reading the audit trail…'}</div>

          <div className="ops-section-title">Deterministic checks</div>
          {checks?.length ? (
            <div className="policy-grid">
              {checks.map(c => (
                <div key={c.rule} className={`policy-row ${c.result}`}>
                  <span className="pi">
                    {c.result === 'pass' ? <ShieldCheck size={14} /> : c.result === 'fail' ? <ShieldX size={14} /> : <CircleHelp size={14} />}
                  </span>
                  <span className="pn">{RULE_LABELS[c.rule] || c.rule}</span>
                  <span className="pd">{c.detail}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="kv" style={{ color: 'var(--muted)' }}>
              Policy checks for this state come from the full audit trail in Merchant Ops.
            </div>
          )}

          {!!explanation?.timeline_facts?.length && (
            <>
              <div className="ops-section-title" style={{ marginTop: 18 }}>What the trail recorded</div>
              <ul className="drawer-facts">
                {explanation.timeline_facts.map((f, i) => <li key={i}>{f}</li>)}
              </ul>
            </>
          )}

          <button
            className="mini-btn full"
            style={{ marginTop: 16 }}
            onClick={() => { onClose(); window.dispatchEvent(new CustomEvent('acg:open-ops', { detail: sessionId })) }}
          >
            Open full audit trail
          </button>
        </div>
      </aside>
    </>
  )
}
