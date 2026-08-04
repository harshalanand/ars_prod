import { useEffect, useState } from 'react'
import { BookOpen, Bookmark } from 'lucide-react'
import { C } from '@/theme/colors'

// Simple, plain-language Help page: a bookmark rail + BRD/FSD sections.
// content = { title, tag, intro, sections: [{ id, emoji, title, lead, bullets?, rows? }] }
//   bullets: string[] (supports **bold**)      rows: [[term, description], …]
function rich(t) {
  return String(t || '').split(/(\*\*[^*]+\*\*)/g).map((s, i) =>
    s.startsWith('**') && s.endsWith('**')
      ? <strong key={i} style={{ fontWeight: 650, color: C.text }}>{s.slice(2, -2)}</strong>
      : <span key={i}>{s}</span>)
}

export default function HelpPage({ title, tag, intro, sections = [] }) {
  const [active, setActive] = useState(sections[0]?.id)
  useEffect(() => {
    const obs = new IntersectionObserver(
      es => es.forEach(e => { if (e.isIntersecting) setActive(e.target.id) }),
      { rootMargin: '-12% 0px -78% 0px' })
    sections.forEach(s => { const el = document.getElementById(s.id); if (el) obs.observe(el) })
    return () => obs.disconnect()
  }, [sections])
  const jump = id => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return (
    <div className="p-4">
      <div className="mb-4">
        <div className="flex items-center gap-2">
          <h1 className="page-title">{title}</h1>
          {tag && <span className="badge" style={{ background: C.primaryLt, color: C.primary, fontWeight: 700 }}>{tag}</span>}
        </div>
        <p className="text-[12px] text-gray-500 mt-1 max-w-3xl">{intro}</p>
      </div>

      <div className="flex gap-5 items-start">
        <aside className="hidden md:block shrink-0" style={{ width: 220, position: 'sticky', top: 12, alignSelf: 'flex-start' }}>
          <div className="card" style={{ padding: 10 }}>
            <div className="flex items-center gap-1.5 px-1 pb-2 mb-1" style={{ borderBottom: `1px solid ${C.cardBorder}` }}>
              <Bookmark size={13} color={C.primary} />
              <span className="text-[11px] font-bold uppercase tracking-wide" style={{ color: C.textSub }}>Contents</span>
            </div>
            <nav className="flex flex-col gap-0.5">
              {sections.map(s => {
                const on = active === s.id
                return (
                  <button key={s.id} onClick={() => jump(s.id)} className="text-left flex items-center gap-2 rounded-md transition-colors"
                    style={{ padding: '6px 8px', fontSize: 12, fontWeight: on ? 700 : 500, color: on ? C.primary : C.textSub, background: on ? C.primaryLt : 'transparent', borderLeft: `2px solid ${on ? C.primary : 'transparent'}` }}>
                    <span style={{ fontSize: 13 }}>{s.emoji}</span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title}</span>
                  </button>
                )
              })}
            </nav>
          </div>
        </aside>

        <div className="flex-1 min-w-0 flex flex-col gap-4">
          {sections.map(s => (
            <section key={s.id} id={s.id} className="card" style={{ padding: 18, scrollMarginTop: 12 }}>
              <div className="flex items-start gap-2.5">
                <span style={{ fontSize: 20, lineHeight: 1.2 }}>{s.emoji}</span>
                <div style={{ flex: 1 }}>
                  <h2 style={{ fontSize: 16, fontWeight: 700, color: C.text, margin: 0 }}>{s.title}</h2>
                  {s.lead && <p style={{ fontSize: 13, color: C.textSub, margin: '4px 0 0', maxWidth: '72ch', lineHeight: 1.55 }}>{rich(s.lead)}</p>}
                </div>
              </div>
              {s.bullets && (
                <ul className="mt-3 flex flex-col gap-2" style={{ listStyle: 'none', margin: '12px 0 0', padding: 0 }}>
                  {s.bullets.map((b, i) => (
                    <li key={i} style={{ position: 'relative', paddingLeft: 22, fontSize: 13, color: C.textSub, lineHeight: 1.55 }}>
                      <span style={{ position: 'absolute', left: 3, top: 7, width: 6, height: 6, borderRadius: 2, background: C.primary, transform: 'rotate(45deg)' }} />
                      {rich(b)}
                    </li>
                  ))}
                </ul>
              )}
              {s.rows && (
                <div className="mt-3 flex flex-col" style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, overflow: 'hidden' }}>
                  {s.rows.map(([term, desc], i) => (
                    <div key={i} style={{ display: 'flex', gap: 10, padding: '8px 12px', borderTop: i ? `1px solid ${C.cardBorder}` : 'none', background: i % 2 ? C.headerBg : C.card }}>
                      <div style={{ flex: '0 0 150px', fontSize: 12.5, fontWeight: 700, color: C.text }}>{term}</div>
                      <div style={{ flex: 1, fontSize: 12.5, color: C.textSub, lineHeight: 1.5 }}>{rich(desc)}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ))}
          <div className="text-center text-[11px] text-gray-400 py-2 flex items-center justify-center gap-1.5"><BookOpen size={12} /> ARS V2 Retail — help &amp; reference</div>
        </div>
      </div>
    </div>
  )
}
