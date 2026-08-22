export default function StatusPill({ ev }) {
  const short = (ev.checkout_session_id || '').slice(0, 12)
  return (
    <div className="status-pill-row">
      <span className={`status-pill ${ev.status}`}>
        {short && <span className="mono">{short}</span>}
        {ev.status?.replace(/_/g, ' ')}
      </span>
    </div>
  )
}
