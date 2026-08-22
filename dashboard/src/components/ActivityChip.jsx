import { useState } from 'react'
import { ChevronDown, Search, Check } from 'lucide-react'
import CategoryIcon from './CategoryIcon.jsx'

/** Collapsible agent-activity chip. `children` renders the expanded body. */
export default function ActivityChip({ icon, label, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  const Ico = icon || Search
  return (
    <div className="activity-chip anim-in">
      <button className="chip-head" onClick={() => setOpen(o => !o)}>
        <Ico size={14} />
        <span>{label}</span>
        {children && <ChevronDown size={13} className={`chev ${open ? 'open' : ''}`} />}
      </button>
      {open && children && <div className="chip-body">{children}</div>}
    </div>
  )
}

export function ProductRows({ items }) {
  if (!items?.length) return null
  return (
    <div>
      {items.map(p => (
        <div className="prod-row" key={p.pack_id}>
          <span className="prod-ico"><CategoryIcon category={p.category} /></span>
          <span className="nm">{p.name} <small style={{ color: 'var(--faint)' }}>{p.size}</small></span>
          {p.quantity > 1 && <span className="qty-badge">×{p.quantity}</span>}
          <span className="pr">₹{p.price * (p.quantity || 1)}</span>
        </div>
      ))}
    </div>
  )
}

export function FoundChip({ ev }) {
  const candidates = ev.candidates || []
  return (
    <ActivityChip icon={Search} label={ev.summary || 'Searched the catalog'} defaultOpen={candidates.length > 0}>
      {candidates.length
        ? <ProductRows items={candidates} />
        : (
          <div style={{ display: 'flex', gap: 7, alignItems: 'center', fontSize: 12.5, color: 'var(--muted)', padding: '4px 2px' }}>
            <Check size={13} /> no matching products found
          </div>
        )}
    </ActivityChip>
  )
}
