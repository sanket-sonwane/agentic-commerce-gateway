import { useEffect, useState } from 'react'

/**
 * Ephemeral "working" indicator shown while the agents negotiate.
 * Rotates through reassuring, human phases — never raw internals.
 */
const PHASES = ['Checking availability…', 'Preparing your order…', 'Almost there…']

export default function WorkingShimmer({ label }) {
  const [phase, setPhase] = useState(0)

  useEffect(() => {
    const t = setInterval(() => setPhase(p => Math.min(p + 1, PHASES.length - 1)), 4200)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="msg-row ai" aria-live="polite">
      <div className="avatar"><Sparkle /></div>
      <div className="msg-body">
        <div className="bubble working-bubble">
          <span className="shimmer-dots"><i /><i /><i /></span>
          <span className="working-label">{label || PHASES[phase]}</span>
        </div>
      </div>
    </div>
  )
}

function Sparkle() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3l1.9 5.8L19.7 10.7l-5.8 1.9L12 18.4l-1.9-5.8L4.3 10.7l5.8-1.9z" />
    </svg>
  )
}
