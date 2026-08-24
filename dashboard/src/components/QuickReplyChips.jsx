/**
 * Quick-reply decision chips rendered beneath the message they belong to.
 * Chips auto-disable once answered; `resolved` on the parent event drives it.
 */
export default function QuickReplyChips({ choices, disabled, resolved }) {
  if (resolved) {
    return (
      <div className="qr-row resolved">
        {choices.map(c => (
          <span key={c.value} className={`qr-chip ${c.tone || ''} ${c.value === resolved ? 'picked' : ''}`}>
            {c.pickedLabel || c.label}
          </span>
        ))}
      </div>
    )
  }
  return (
    <div className="qr-row">
      {choices.map(c => (
        <button
          key={c.value}
          className={`qr-chip ${c.tone || ''}`}
          disabled={disabled}
          onClick={c.onClick}
        >
          {c.label}
        </button>
      ))}
    </div>
  )
}
