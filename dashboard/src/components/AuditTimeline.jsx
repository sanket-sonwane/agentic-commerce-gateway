import {
  Search, ReceiptText, PackageCheck, ShieldCheck, ShieldX, PencilLine,
  CreditCard, BadgeCheck, XCircle, Ban, UserCog, RotateCcw,
  Hourglass, Save, FileSignature, PlayCircle, CircleHelp, CheckCheck, Dot,
} from 'lucide-react'

export function actionVisual(ev) {
  const a = ev.action
  if (a.startsWith('discovery')) return { Icon: Search }
  if (a === 'checkout_create_requested' || a === 'checkout_created') return { Icon: ReceiptText }
  if (a.startsWith('reservation')) return { Icon: PackageCheck }
  if (a === 'inventory_adjusted') return { Icon: UserCog }
  if (a === 'gating_evaluated' || a === 'final_validation')
    return { Icon: ev.decision === 'declined' ? ShieldX : ShieldCheck }
  if (a === 'checkout_updated') return { Icon: PencilLine }
  if (a.startsWith('confirmation')) return { Icon: FileSignature }
  if (a === 'complete_requested') return { Icon: PlayCircle }
  if (a === 'razorpay_order_creation' || a === 'razorpay_order_created' || a === 'razorpay_token_charge') return { Icon: CreditCard }
  if (a === 'payment_success') return { Icon: BadgeCheck }
  if (a === 'payment_failure' || a === 'checkout_declined') return { Icon: XCircle }
  if (a === 'payment_pending' || a === 'awaiting_authorization') return { Icon: Hourglass }
  if (a === 'payment_token_saved') return { Icon: Save }
  if (a === 'checkout_completed') return { Icon: CheckCheck }
  if (a === 'checkout_cancelled') return { Icon: Ban }
  if (a === 'demo_reset') return { Icon: RotateCcw }
  if (ev.decision === 'hold' || ev.decision === 'pending') return { Icon: CircleHelp }
  return { Icon: Dot }
}

/**
 * Vertical audit timeline shared by the ExplainDrawer preview and OpsPanel.
 */
export default function AuditTimeline({ events, limit = 200 }) {
  const shown = events.slice(-limit)
  return (
    <div className="vtl">
      {shown.map(ev => {
        const { Icon } = actionVisual(ev)
        return (
          <div key={ev.seq} className={`vtl-item ${ev.decision || ''}`}>
            <span className="vtl-dot"><Icon size={10} /></span>
            <div className="vtl-title">
              <span>{ev.action.replace(/_/g, ' ')}</span>
              <span className="vtl-time">{new Date(ev.timestamp + 'Z').toLocaleTimeString()}</span>
            </div>
            {ev.reasoning_summary && <div className="vtl-reason">{ev.reasoning_summary}</div>}
          </div>
        )
      })}
      {!shown.length && <div className="kv" style={{ color: 'var(--muted)' }}>No events yet.</div>}
    </div>
  )
}
