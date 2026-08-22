import { Sparkles, User } from 'lucide-react'

/** Render **bold** segments without allowing HTML injection. */
function richText(text) {
  const parts = String(text).split(/(\*\*[^*]+\*\*)/g)
  return parts.map((p, i) =>
    p.startsWith('**') && p.endsWith('**') ? <b key={i}>{p.slice(2, -2)}</b> : <span key={i}>{p}</span>,
  )
}

export function fmtTime(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function MessageBubble({ role, content, ts }) {
  const isUser = role === 'user'
  return (
    <div className={`msg-row ${isUser ? 'user' : 'ai'}`}>
      {!isUser && <div className="avatar"><Sparkles size={16} /></div>}
      <div className="msg-body">
        <div className="bubble">{richText(content)}</div>
        {ts && <div className="msg-meta">{fmtTime(ts)}</div>}
      </div>
      {isUser && (
        <div className="avatar user-avatar"><User size={15} /></div>
      )}
    </div>
  )
}
