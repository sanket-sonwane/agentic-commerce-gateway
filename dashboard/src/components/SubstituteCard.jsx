import { useState } from 'react'
import { AlertTriangle, Repeat2, MoreHorizontal, Ban, Check, Loader2, PackageSearch } from 'lucide-react'
import CategoryIcon from './CategoryIcon.jsx'

/** "Item X unavailable — proceed with an alternative?" consent card. */
export default function SubstituteCard({ ev, onChoose, busy }) {
  const [sel, setSel] = useState(0)
  const resolved = ev.resolved

  return (
    <div className="sub-card">
      <div className="sub-warn">
        <AlertTriangle size={16} style={{ flex: 'none', marginTop: 1 }} />
        <span>
          <b>{ev.unavailable.name}</b> is out of stock — nothing was charged.
          {ev.options?.length ? ' Proceed with one of these instead?' : ''}
        </span>
      </div>

      {(ev.options || []).map((o, i) => (
        <button
          key={o.pack_id}
          className={`opt ${sel === i ? 'selected' : ''}`}
          disabled={!!resolved || busy}
          onClick={() => setSel(i)}
        >
          <span className="radio" />
          <span className="prod-ico"><CategoryIcon category={o.category} /></span>
          <span className="info"><b>{o.name}</b><span>{o.size} · {o.available_stock} in stock</span></span>
          <span className="price">₹{o.price}</span>
        </button>
      ))}

      {!resolved ? (
        <div className="sub-actions">
          <button className="btn primary" disabled={busy || !ev.options?.length} onClick={() => onChoose(sel)}>
            {busy ? <Loader2 size={14} /> : <Check size={14} />} Use selection
          </button>
          <button className="btn subtle" disabled={busy} onClick={() => onChoose('other')}>
            <MoreHorizontal size={14} /> Other options
          </button>
          <button className="btn ghost-danger" disabled={busy} onClick={() => onChoose('cancel')}>
            <Ban size={14} />
          </button>
        </div>
      ) : (
        <div className={`resolved-line ${ev.resolved}`}>
          {ev.resolved === 'chosen'
            ? <><Repeat2 size={14} /> Substitution accepted</>
            : ev.resolved === 'other'
              ? <><PackageSearch size={14} /> Asked for more options</>
              : <><Ban size={14} /> You declined the substitution — order dropped</>}
        </div>
      )}
    </div>
  )
}
