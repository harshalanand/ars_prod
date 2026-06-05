/**
 * ListingPage — Build & view ARS_LISTING master table (Data Preparation)
 */
import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { listingAPI, gridBuilderAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  List, RefreshCw, Loader2, Database, Play, Pause, ChevronLeft, ChevronRight,
  Eye, BarChart3, Search, Filter, Download, X, XCircle, Square, Cpu, Zap,
  ChevronDown, ChevronUp, Activity, Clock, FileText, Maximize2, AlertTriangle,
} from 'lucide-react'
import { C } from '@/theme/colors'
import { BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import * as XLSX from 'xlsx'

/* ── Searchable Multi-Select (dropdown only on search) ────────────────── */
function SearchSelect({ label, items, selected, setSelected, placeholder }) {
  const [search, setSearch] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(0)
  // Tags are collapsed by default once the user picks more than a handful —
  // we only show the count + first 3, and let the user expand to see all.
  const [tagsExpanded, setTagsExpanded] = useState(false)
  const ref = useRef(null)
  const listRef = useRef(null)

  const filtered = items.filter(s =>
    search ? s.toLowerCase().includes(search.toLowerCase()) : false
  ).slice(0, 40)

  // Reset active index when filter results change
  useEffect(() => { setActiveIdx(0) }, [search])

  // Scroll active item into view when navigating with arrow keys
  useEffect(() => {
    if (!open || !listRef.current) return
    const el = listRef.current.querySelector(`[data-idx="${activeIdx}"]`)
    if (el) el.scrollIntoView({ block: 'nearest' })
  }, [activeIdx, open])

  // close dropdown on outside click
  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const toggle = (item) => {
    setSelected(prev => prev.includes(item) ? prev.filter(x => x !== item) : [...prev, item])
  }

  const handleKeyDown = (e) => {
    if (!open || filtered.length === 0) {
      if (e.key === 'ArrowDown' && search) setOpen(true)
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx(i => (i + 1) % filtered.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx(i => (i - 1 + filtered.length) % filtered.length)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const item = filtered[activeIdx]
      if (item) {
        toggle(item)
        setSearch('')
        setOpen(false)
      }
    } else if (e.key === 'Escape') {
      setOpen(false)
    } else if (e.key === 'Tab') {
      setOpen(false)
    }
  }

  return (
    <div style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: C.text, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
        <Filter size={11} color={C.primary}/>
        {label}
        {selected.length === 0 && (
          <span style={{ fontSize: 9, color: C.textMuted, fontWeight: 400 }}>All</span>
        )}
      </div>

      {/* Selected tags — collapsed by default once > 3 picks */}
      {selected.length > 0 && (() => {
        const COLLAPSE_LIMIT = 3
        const showAll = tagsExpanded || selected.length <= COLLAPSE_LIMIT
        const visible = showAll ? selected : selected.slice(0, COLLAPSE_LIMIT)
        const hidden  = selected.length - visible.length
        return (
          <div style={{ display: 'flex', gap: 2, flexWrap: 'wrap', marginBottom: 4, alignItems: 'center' }}>
            <span style={{
              fontSize: 8, fontWeight: 700, color: '#fff', background: C.primary,
              padding: '1px 5px', borderRadius: 3,
            }}>
              {selected.length} selected
            </span>
            {visible.map(s => (
              <span key={s} onClick={() => toggle(s)}
                title="Click to remove"
                style={{ fontSize: 8, padding: '1px 5px', borderRadius: 3, cursor: 'pointer',
                  background: C.primaryLt, color: C.primary, border: `1px solid ${C.primaryBd}` }}>
                {s} x
              </span>
            ))}
            {hidden > 0 && !tagsExpanded && (
              <button onClick={() => setTagsExpanded(true)}
                style={{ fontSize: 8, padding: '1px 5px', borderRadius: 3, cursor: 'pointer',
                  background: '#f1f5f9', color: C.textSub, border: '1px solid #e2e8f0',
                  display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                <ChevronDown size={8}/> +{hidden} more
              </button>
            )}
            {tagsExpanded && selected.length > COLLAPSE_LIMIT && (
              <button onClick={() => setTagsExpanded(false)}
                style={{ fontSize: 8, padding: '1px 5px', borderRadius: 3, cursor: 'pointer',
                  background: '#f1f5f9', color: C.textSub, border: '1px solid #e2e8f0',
                  display: 'inline-flex', alignItems: 'center', gap: 2 }}>
                <ChevronUp size={8}/> collapse
              </button>
            )}
            <button onClick={() => setSelected([])}
              style={{ fontSize: 8, color: C.red, background: 'none', border: 'none', cursor: 'pointer', textDecoration: 'underline' }}>
              Clear
            </button>
          </div>
        )
      })()}

      {/* Search + Paste input */}
      <div ref={ref} style={{ position: 'relative' }}>
        <Search size={9} style={{ position: 'absolute', left: 5, top: 7, color: C.textMuted, pointerEvents: 'none' }}/>
        <input value={search}
          onChange={e => { setSearch(e.target.value); setOpen(true) }}
          onFocus={() => { if (search) setOpen(true) }}
          onKeyDown={handleKeyDown}
          onPaste={e => {
            e.preventDefault()
            const pasted = e.clipboardData.getData('text')
            // Parse pasted: comma, newline, tab, space separated
            const vals = pasted.split(/[,\n\t;]+/).map(v => v.trim()).filter(Boolean)
            if (vals.length > 1) {
              // Multi-paste: add all valid items
              const valid = vals.filter(v => items.includes(v))
              if (valid.length > 0) {
                setSelected(prev => [...new Set([...prev, ...valid])])
                setSearch('')
              } else {
                // Try case-insensitive match
                const lower = items.map(i => ({ orig: i, low: i.toLowerCase() }))
                const matched = vals.map(v => lower.find(l => l.low === v.toLowerCase())?.orig).filter(Boolean)
                if (matched.length > 0) setSelected(prev => [...new Set([...prev, ...matched])])
              }
            } else {
              setSearch(pasted.trim())
              setOpen(true)
            }
          }}
          placeholder={placeholder || 'Search or paste multiple...'}
          style={{ height: 24, fontSize: 10, padding: '0 6px 0 18', borderRadius: 4,
            border: '1px solid #e2e8f0', outline: 'none', background: '#fff',
            width: '100%', boxSizing: 'border-box' }}/>

        {/* Dropdown */}
        {open && search && filtered.length > 0 && (
          <div ref={listRef} style={{ position: 'absolute', top: 26, left: 0, right: 0, zIndex: 20,
            background: '#fff', border: '1px solid #e2e8f0', borderRadius: 4, boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
            maxHeight: 120, overflowY: 'auto' }}>
            {filtered.map((item, idx) => {
              const isSel = selected.includes(item)
              const isActive = idx === activeIdx
              const bg = isActive ? '#dbeafe' : (isSel ? C.primaryLt : 'transparent')
              return (
                <div key={item} data-idx={idx}
                  onClick={() => { toggle(item); setSearch(''); setOpen(false) }}
                  onMouseEnter={() => setActiveIdx(idx)}
                  style={{ padding: '3px 8px', fontSize: 9, cursor: 'pointer',
                    background: bg,
                    color: isSel ? C.primary : C.text,
                    fontWeight: isSel ? 700 : 400,
                    borderLeft: isActive ? `2px solid ${C.primary}` : '2px solid transparent' }}>
                  {item} {isSel && '(selected)'}
                </div>
              )
            })}
          </div>
        )}
        {open && search && filtered.length === 0 && (
          <div style={{ position: 'absolute', top: 26, left: 0, right: 0, zIndex: 20,
            background: '#fff', border: '1px solid #e2e8f0', borderRadius: 4, padding: '6px 8px',
            fontSize: 9, color: C.textMuted }}>
            No match
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Dropdown Multi-Select (opens on click, shows all items as checkboxes) ── */
function DropdownMultiSelect({ label, items, selected, setSelected, placeholder }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const ref = useRef(null)

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) { setOpen(false); setSearch('') } }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = search
    ? items.filter(s => s.toLowerCase().includes(search.toLowerCase()))
    : items

  const toggle = (item) => {
    setSelected(prev => prev.includes(item) ? prev.filter(x => x !== item) : [...prev, item])
  }

  const displayLabel = selected.length === 0
    ? (placeholder || 'All (no filter)')
    : selected.length === 1
      ? selected[0]
      : `${selected[0]} +${selected.length - 1} more`

  return (
    <div ref={ref} style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, position: 'relative' }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: C.text, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 4 }}>
        <Filter size={11} color={C.primary}/>
        {label}
        {selected.length > 0 && (
          <span style={{ fontSize: 9, fontWeight: 700, color: '#fff', background: C.primary,
            padding: '1px 5px', borderRadius: 3, marginLeft: 2 }}>
            {selected.length}
          </span>
        )}
        {selected.length === 0 && (
          <span style={{ fontSize: 9, color: C.textMuted, fontWeight: 400 }}>All</span>
        )}
      </div>

      {/* Trigger button */}
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          height: 28, padding: '0 8px', borderRadius: 4, border: `1px solid ${open ? C.primary : '#e2e8f0'}`,
          background: '#fff', cursor: 'pointer', fontSize: 10, color: selected.length ? C.text : C.textMuted,
          outline: 'none', boxSizing: 'border-box',
        }}>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{displayLabel}</span>
        <ChevronDown size={11} style={{ flexShrink: 0, marginLeft: 4, color: C.textSub, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.15s' }}/>
      </button>

      {/* Dropdown panel */}
      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% - 4px)', left: 0, right: 0, zIndex: 50,
          background: '#fff', border: `1px solid ${C.primary}`, borderRadius: 6,
          boxShadow: '0 6px 20px rgba(0,0,0,0.12)', paddingBottom: 4,
        }}>
          {/* Search inside dropdown */}
          {items.length > 8 && (
            <div style={{ padding: '6px 8px 4px', borderBottom: '1px solid #f1f5f9' }}>
              <input
                autoFocus
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search..."
                style={{ width: '100%', height: 22, fontSize: 9, padding: '0 6px', borderRadius: 4,
                  border: '1px solid #e2e8f0', outline: 'none', boxSizing: 'border-box' }}/>
            </div>
          )}
          {/* Clear / Select All row */}
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 8px 2px', borderBottom: '1px solid #f1f5f9' }}>
            <button onClick={() => setSelected([])}
              style={{ fontSize: 8, color: C.red, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
              Clear all
            </button>
            <button onClick={() => setSelected(filtered)}
              style={{ fontSize: 8, color: C.primary, background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
              Select all
            </button>
          </div>
          {/* Items */}
          <div style={{ maxHeight: 160, overflowY: 'auto' }}>
            {filtered.length === 0 ? (
              <div style={{ padding: '6px 8px', fontSize: 9, color: C.textMuted }}>No match</div>
            ) : filtered.map(item => {
              const isSel = selected.includes(item)
              return (
                <label key={item}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6, padding: '3px 10px',
                    cursor: 'pointer', fontSize: 10,
                    background: isSel ? C.primaryLt : 'transparent',
                    color: isSel ? C.primary : C.text,
                    fontWeight: isSel ? 600 : 400,
                  }}>
                  <input type="checkbox" checked={isSel} onChange={() => toggle(item)}
                    style={{ accentColor: C.primary, width: 11, height: 11, flexShrink: 0 }}/>
                  {item}
                </label>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

/* ── Checkbox group (inline multi-select for small fixed option sets) ─────── */
function CheckboxGroup({ label, items, selected, setSelected, color, vertical }) {
  if (!items || items.length === 0) return (
    <div>
      <div style={{ fontSize: 9, fontWeight: 700, color: color || C.textSub, letterSpacing: '.05em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 9, color: C.textMuted, fontStyle: 'italic' }}>— no options —</div>
    </div>
  )
  const toggle = (item) =>
    setSelected(prev => prev.includes(item) ? prev.filter(x => x !== item) : [...prev, item])
  const allSel = items.length > 0 && items.every(i => selected.includes(i))
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 5 }}>
        <div style={{ fontSize: 9, fontWeight: 700, color: color || C.textSub, letterSpacing: '.05em' }}>{label}</div>
        {selected.length > 0 && (
          <span style={{ fontSize: 8, fontWeight: 700, color: '#fff', background: color || C.primary,
            padding: '1px 5px', borderRadius: 3 }}>
            {selected.length}
          </span>
        )}
        {selected.length > 0 && (
          <button onClick={() => setSelected([])}
            style={{ fontSize: 8, color: C.red, background: 'none', border: 'none', cursor: 'pointer', padding: 0, lineHeight: 1 }}>
            clear
          </button>
        )}
        {!allSel && items.length > 1 && (
          <button onClick={() => setSelected(items)}
            style={{ fontSize: 8, color: C.primary, background: 'none', border: 'none', cursor: 'pointer', padding: 0, lineHeight: 1 }}>
            all
          </button>
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: vertical ? 'column' : 'row', flexWrap: vertical ? 'nowrap' : 'wrap', gap: vertical ? 3 : '4px 10px' }}>
        {items.map(item => {
          const checked = selected.includes(item)
          return (
            <label key={item} style={{
              display: 'flex', alignItems: 'center', gap: 4,
              cursor: 'pointer', userSelect: 'none',
              fontSize: 10, fontWeight: checked ? 700 : 400,
              color: checked ? (color || C.primary) : C.text,
            }}>
              <input type="checkbox" checked={checked} onChange={() => toggle(item)}
                style={{ accentColor: color || C.primary, width: 11, height: 11, cursor: 'pointer' }}/>
              {item}
            </label>
          )
        })}
      </div>
    </div>
  )
}

/* ── Presentational helpers (defined once, outside the page component) ───── */
function KpiTile({ icon: Icon, label, value, accent, sub, onClick }) {
  const clickable = typeof onClick === 'function'
  return (
    <div onClick={onClick} title={clickable ? 'Click for details' : undefined}
      style={{
        background: '#fff', border: '1px solid #e2e8f0', borderRadius: 7,
        padding: '5px 8px 5px 11px', display: 'flex', alignItems: 'center', gap: 6,
        boxShadow: '0 1px 2px rgba(0,0,0,0.03)', position: 'relative', overflow: 'hidden', minHeight: 40,
        cursor: clickable ? 'pointer' : 'default',
        transition: 'box-shadow .15s, transform .1s',
      }}
      onMouseEnter={clickable ? (e) => { e.currentTarget.style.boxShadow = '0 2px 6px rgba(0,0,0,0.08)' } : undefined}
      onMouseLeave={clickable ? (e) => { e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.03)' } : undefined}>
      <div style={{ position: 'absolute', top: 0, left: 0, width: 3, bottom: 0, background: accent }}/>
      {Icon && <Icon size={13} color={accent} style={{ flexShrink: 0 }}/>}
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0, lineHeight: 1.15 }}>
        <div style={{ fontSize: 8, fontWeight: 700, color: C.textSub, letterSpacing: '.04em', whiteSpace: 'nowrap' }}>
          {(label || '').toUpperCase()}
        </div>
        {sub && <div style={{ fontSize: 8, color: C.textMuted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{sub}</div>}
      </div>
      <div style={{ fontSize: 15, fontWeight: 800, color: accent, fontVariantNumeric: 'tabular-nums', lineHeight: 1, whiteSpace: 'nowrap', flexShrink: 0 }}>
        {(value ?? 0).toLocaleString()}
      </div>
    </div>
  )
}

function InsightTile({ label, value, accent }) {
  return (
    <div style={{ background: '#fff', border: '1px solid #f1f5f9', borderRadius: 8, padding: '6px 10px' }}>
      <div style={{ fontSize: 8, fontWeight: 700, color: C.textSub, letterSpacing: '.04em' }}>{(label || '').toUpperCase()}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: accent || C.text, marginTop: 1, fontVariantNumeric: 'tabular-nums' }}>
        {(value ?? 0).toLocaleString()}
      </div>
    </div>
  )
}

function RankSelector({ dir, setDir, n, setN }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <div style={{ display: 'flex', borderRadius: 4, overflow: 'hidden', border: '1px solid #e2e8f0' }}>
        {['top', 'bottom'].map(v => (
          <button key={v} onClick={() => setDir(v)}
            style={{
              fontSize: 9, fontWeight: 700, padding: '2px 8px', cursor: 'pointer',
              border: 'none', textTransform: 'uppercase', letterSpacing: '.04em',
              background: dir === v ? '#1f2937' : '#fff',
              color: dir === v ? '#fff' : '#64748b',
            }}>
            {v}
          </button>
        ))}
      </div>
      <select value={n} onChange={e => setN(parseInt(e.target.value, 10))}
        style={{
          height: 22, fontSize: 10, fontWeight: 600, borderRadius: 4,
          border: '1px solid #e2e8f0', padding: '0 4px', background: '#fff', cursor: 'pointer',
        }}>
        {[5, 10, 15, 20, 30, 50].map(x => <option key={x} value={x}>{x}</option>)}
      </select>
    </div>
  )
}

function ChartCard({ title, subtitle, right, children }) {
  const [fs, setFs] = useState(false)
  const NORMAL_H = 240
  const FS_H = 'calc(80vh - 100px)'
  const body = typeof children === 'function' ? children(NORMAL_H) : children
  const fsBody = typeof children === 'function' ? children(FS_H) : children

  React.useEffect(() => {
    if (!fs) return
    const onKey = (e) => { if (e.key === 'Escape') setFs(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [fs])

  return (
    <>
      {fs && (
        <div onClick={() => setFs(false)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 2000 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 12, width: '90vw', maxHeight: '90vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)', overflow: 'hidden' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px', borderBottom: '1px solid #e2e8f0', flexShrink: 0 }}>
              <div>
                <div style={{ fontWeight: 700, fontSize: 13, color: C.text }}>{title}</div>
                {subtitle && <div style={{ fontSize: 10, color: C.textMuted, marginTop: 1 }}>{subtitle}</div>}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {right}
                <button onClick={() => setFs(false)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, color: C.textSub }}><X size={16}/></button>
              </div>
            </div>
            <div style={{ flex: 1, padding: 16, minHeight: 0 }}>{fsBody}</div>
          </div>
        </div>
      )}
      <div style={{ background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', padding: 12, boxShadow: '0 1px 3px rgba(0,0,0,0.03)' }}>
        <div style={{ marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end' }}>
          <div>
            <div style={{ fontWeight: 700, fontSize: 12, color: C.text }}>{title}</div>
            {subtitle && <div style={{ fontSize: 9, color: C.textMuted, marginTop: 1 }}>{subtitle}</div>}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {right}
            <button onClick={() => setFs(true)} title="Fullscreen" style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 2, color: C.textMuted, lineHeight: 0 }}><Maximize2 size={12}/></button>
          </div>
        </div>
        {body}
      </div>
    </>
  )
}

function ParamGroup({ title, color, children }) {
  return (
    <div style={{ borderLeft: `3px solid ${color}`, paddingLeft: 10 }}>
      <div style={{ fontSize: 9, fontWeight: 700, color, letterSpacing: '.05em', marginBottom: 6 }}>{title.toUpperCase()}</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>{children}</div>
    </div>
  )
}

function ParamInput({ label, value, setter, step, hint, tip, min }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '78px 60px 1fr', alignItems: 'center', gap: 4 }} title={tip}>
      <span style={{ fontSize: 10, color: C.textSub }}>{label}</span>
      <input type="number" step={step} min={min} value={value} onChange={e => setter(e.target.value)}
        style={{ height: 24, fontSize: 11, fontWeight: 700, textAlign: 'center', borderRadius: 4,
          border: '1px solid #e2e8f0', background: '#f8fafc', padding: '0 4px', width: '100%', boxSizing: 'border-box' }}/>
      <span style={{ fontSize: 9, color: C.textMuted }}>{hint}</span>
    </div>
  )
}

function ToggleRow({ checked, setChecked, label, color, hint }) {
  return (
    <div onClick={() => setChecked(c => !c)} title={hint}
      style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none', padding: '2px 0' }}>
      <div style={{ width: 14, height: 14, borderRadius: 3, flexShrink: 0,
        border: `2px solid ${checked ? color : C.textMuted}`,
        background: checked ? color : 'transparent',
        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        {checked && <span style={{ color: '#fff', fontSize: 9, fontWeight: 800, lineHeight: 1 }}>✓</span>}
      </div>
      <span style={{ fontSize: 10, color: checked ? color : C.textSub, fontWeight: checked ? 700 : 500 }}>{label}</span>
    </div>
  )
}

const pillStyle = (color) => ({
  fontSize: 9, fontWeight: 700, color, background: `${color}15`, padding: '2px 8px', borderRadius: 4,
  border: `1px solid ${color}40`, display: 'inline-flex', alignItems: 'center', gap: 4,
})

const statusPillStyle = (status) => {
  const s = (status || '').toUpperCase()
  const color = s.includes('FAIL') || s.includes('ERROR') || s.includes('REJECT') ? '#dc2626'
    : s.includes('SUCCESS') || s.includes('ALLOC') || s.includes('DONE') || s.includes('OK') ? '#059669'
    : s.includes('PEND') || s.includes('PARTIAL') ? '#d97706'
    : '#64748b'
  return pillStyle(color)
}

export default function ListingPage() {
  const navigate = useNavigate()
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [paused, setPaused] = useState(false)
  const abortRef = useRef(null)
  const [summary, setSummary] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewPage, setPreviewPage] = useState(1)
  const [previewPageSize, setPreviewPageSize] = useState(100)
  const [globalSearch, setGlobalSearch] = useState('')
  const [previewTable, setPreviewTable] = useState('working') // 'working' | 'listing' | 'alloc'

  // Column filters for preview (key = column name, value = filter text)
  const [colFilters, setColFilters] = useState({})

  // Generate settings
  const [rdcMode, setRdcMode] = useState('all')
  const [crossFrom, setCrossFrom] = useState([])
  const [selectedStores, setSelectedStores] = useState([])
  const [selectedMajCats, setSelectedMajCats] = useState([])
  const [selectedSsn, setSelectedSsn] = useState([])
  const [expandedChart, setExpandedChart] = useState(null) // 'ssn' | 'div' | null

  const ssnOptions = useMemo(() =>
    [...new Set((config?.ssns || []).filter(Boolean))].sort()
  , [config?.ssns])
  const [runMode, setRunMode] = useState('listing') // 'listing' | 'full'
  // 'st_maj_rng' (default — 1 line per WERKS+MAJ_CAT+RNG_SEG)
  // 'st_maj'     (1 line per WERKS+MAJ_CAT)
  // 'each'       (no aggregation, keep every MIX line)
  const [mixMode, setMixMode] = useState('st_maj_rng')
  // Configurable variables
  const [stockThresholdPct, setStockThresholdPct] = useState(0.6)   // OPT_TYPE threshold (60%)
  const [excessMultiplier, setExcessMultiplier] = useState(2.0)     // Excess = STK > X × OPT_MBQ
  const [holdDays, setHoldDays] = useState(0)                       // OPT_MBQ_WH hold days
  const [ageThreshold, setAgeThreshold] = useState(15)              // AGE < X → use PER_OPT_SALE
  const [reqWeight, setReqWeight] = useState(0.4)                   // Store ranking: req weight
  const [fillWeight, setFillWeight] = useState(0.6)                 // Store ranking: fill weight
  // Secondary-grid dispatch cap toggle (default ON). Main pass enforces
  // cap = 130% on every Secondary grid.
  const [applySecCapInNormal, setApplySecCapInNormal] = useState(true)
  const [defaultAcsD, setDefaultAcsD] = useState(18)              // Default ACS_D fallback
  const [enableMinSize, setEnableMinSize] = useState(false)        // Toggle min size check
  const [minSizeCount, setMinSizeCount] = useState(3)             // Min sizes for TBL listing
  // PRI_CT%>=100 gate applied per opt_type (TBL always on). Off = allow
  // RL/TBC to list/allocate even if primary grid coverage is below 100%.
  const [priCheckRL, setPriCheckRL]   = useState(false)
  const [priCheckTBC, setPriCheckTBC] = useState(false)
  // MBQ cap — active only when the corresponding PRI gate is OFF (unchecked).
  // Prevents over-allocation: total SHIP_QTY per store ≤ cap% of MJ_MBQ.
  // TBL has no MJ-cap (removed 2026-05-16) — bounded only by SZ_REQ.
  // Per-OPT_TYPE dispatch caps (Decision 4-B). Anchored to MJ_MBQ_ORIG on
  // the backend.  Defaults follow Grid MBQ Growth % automatically — these
  // state values are ONLY consulted when the corresponding PRI ≥ 100%
  // toggle is ON (the field surfaces inline next to that toggle).  TBL
  // has no PRI toggle and always inherits growth %.
  const [rlMbqCapPct,  setRlMbqCapPct]  = useState(110)
  const [tbcMbqCapPct, setTbcMbqCapPct] = useState(110)
  // MJ_MBQ growth headroom (Allocation Gate).  Slider value applies only
  // when mbqGrowthUseDefault is OFF.  Checked = force 100% (strict cap).
  // Unchecked = scale MJ_MBQ → MJ_MBQ_REV by this %, then MJ_REQ_REV is
  // re-derived from the scaled MBQ.
  const [mjReqGrowthPct, setMjReqGrowthPct] = useState(110)
  const [mbqGrowthUseDefault, setMbqGrowthUseDefault] = useState(true)
  // Parking mode: false (default) = single-parked (block new runs while a
  // session is awaiting review); true = multi-parked (stack snapshots).
  const [allowMultiParked, setAllowMultiParked] = useState(false)
  const [previewExpanded, setPreviewExpanded] = useState(false)
  const [majCatModalOpen, setMajCatModalOpen] = useState(false)
  const [storeModalOpen, setStoreModalOpen] = useState(false)
  const [mcFilter, setMcFilter]   = useState('')
  const [mcSortCol, setMcSortCol] = useState('totalAlloc')
  const [mcSortDir, setMcSortDir] = useState('desc')
  // Store-wise drill-down from the MAJ_CAT modal: maj_cat + (optional) rdc.
  const [storeDrillOpen,     setStoreDrillOpen]     = useState(false)
  const [storeDrillMajCat,   setStoreDrillMajCat]   = useState('')
  const [storeDrillRdc,      setStoreDrillRdc]      = useState(null)
  const [storeDrillData,     setStoreDrillData]     = useState([])
  const [storeDrillLoading,  setStoreDrillLoading]  = useState(false)
  const [storeDrillSortCol,  setStoreDrillSortCol]  = useState('alloc_qty')
  const [storeDrillSortDir,  setStoreDrillSortDir]  = useState('desc')

  // ── Per-cell calc popover (Phase 1B) ─────────────────────────────────
  // Anchored panel showing the formula + values for the metric clicked.
  // calcPopover = { metric, mc, rdc, cell, anchor: {x,y}, sloc?: [...] }
  const [calcPopover, setCalcPopover] = useState(null)

  // ── OPT-wise drill modal (Phase 2) ───────────────────────────────────
  // Triggered when the user clicks the MAJ_CAT name (or an aggregate
  // cell) — shows one row per (WERKS, GEN_ART_NUMBER, CLR).
  const [optModalOpen,     setOptModalOpen]     = useState(false)
  const [optModalMajCat,   setOptModalMajCat]   = useState('')
  const [optModalRdc,      setOptModalRdc]      = useState(null)
  const [optModalWerks,    setOptModalWerks]    = useState(null)
  const [optModalData,     setOptModalData]     = useState([])
  const [optModalLoading,  setOptModalLoading]  = useState(false)
  const [optModalSortCol,  setOptModalSortCol]  = useState('OPT_PRIORITY_RANK')
  const [optModalSortDir,  setOptModalSortDir]  = useState('asc')

  // ── VAR_ART-wise drill modal (Phase 2) ───────────────────────────────
  // Triggered when the user clicks an OPT row in the OPT modal — shows
  // one row per (VAR_ART, SZ) for that OPT.
  const [varModalOpen,    setVarModalOpen]    = useState(false)
  const [varModalCtx,     setVarModalCtx]     = useState(null)   // { mc, werks, gen_art, clr, rdc }
  const [varModalData,    setVarModalData]    = useState([])
  const [varModalLoading, setVarModalLoading] = useState(false)
  const [varModalSortCol, setVarModalSortCol] = useState('SZ')
  const [varModalSortDir, setVarModalSortDir] = useState('asc')

  // Drill stack — single source of truth for current drill level within
  // the MAJ_CAT modal. The same modal frame swaps content based on the
  // current entry; user navigates back via the Back button (popDrill).
  //   null                                       → MAJ_CAT root view
  //   { view: 'store', mc, rdc }                 → store-wise table
  //   { view: 'opt',   mc, rdc, werks }          → OPT-wise table
  //   { view: 'var',   mc, rdc, werks, gen_art,
  //                    clr, opt }                → VAR_ART × SZ table
  const [drill, setDrill] = useState(null)

  // Pop one level off the drill (back navigation). At root → no-op
  // (close button is what exits the modal entirely).
  const popDrill = () => {
    setDrill(d => {
      if (!d) return null
      if (d.view === 'var')
        return { view: 'opt', mc: d.mc, rdc: d.rdc, werks: d.werks }
      if (d.view === 'opt')
        return { view: 'store', mc: d.mc, rdc: d.rdc }
      return null
    })
  }

  // Store-wise drill — sets drill state and fires the data fetch.
  // Called from MAJ_CAT row name click. rdc=null means all RDCs.
  const openStoreDrillNew = (mc, rdc) => {
    setStoreDrillMajCat(mc)
    setStoreDrillRdc(rdc || null)
    setStoreDrillData([])
    setStoreDrillLoading(true)
    setDrill({ view: 'store', mc, rdc: rdc || null })
    setMajCatModalOpen(true)
    listingAPI.storeByMajCat(mc, rdc || undefined)
      .then(res => setStoreDrillData(res?.data?.data || []))
      .catch(() => setStoreDrillData([]))
      .finally(() => setStoreDrillLoading(false))
  }

  // OPT-wise drill — accepts an optional `werks` so the store-drill view
  // can pass through to a per-store OPT view. mc/rdc/werks may be null.
  const openOptModal = (mc, rdc, werks) => {
    setOptModalMajCat(mc)
    setOptModalRdc(rdc || null)
    setOptModalWerks(werks || null)
    setOptModalData([])
    setOptModalLoading(true)
    setDrill({ view: 'opt', mc, rdc: rdc || null, werks: werks || null })
    listingAPI.optSummary(mc, rdc || undefined, werks || undefined)
      .then(res => setOptModalData(res?.data?.data || []))
      .catch(() => setOptModalData([]))
      .finally(() => setOptModalLoading(false))
  }

  // VAR_ART × SZ drill — fired from an OPT row click. Filters to that
  // specific store + OPT.
  const openVarDrill = (mc, werks, genArt, clr, rdc, opt) => {
    setVarModalCtx({ mc, werks, gen_art: genArt, clr: clr || '', rdc: rdc || null, opt })
    setVarModalData([])
    setVarModalLoading(true)
    setDrill({ view: 'var', mc, rdc: rdc || null, werks, gen_art: genArt, clr: clr || '', opt })
    listingAPI.varSummary(mc, werks, genArt, clr || '', rdc || undefined)
      .then(res => setVarModalData(res?.data?.data || []))
      .catch(() => setVarModalData([]))
      .finally(() => setVarModalLoading(false))
  }

  // ── Parallel allocation (Part 8) ─────────────────────────────────────
  const [allocationMode, setAllocationMode] = useState('pandas') // 'sequential' | 'pandas'
  const [allocOtFilter, setAllocOtFilter]   = useState('all')   // 'all' | 'rl' | 'rl_tbc'
  // Default 4 (not 8) — on Azure SQL with a small SKU, 8 workers cause
  // 'generic waitable object' deadlocks (tempdb metadata + memory-grant
  // contention) that retry_on_deadlock has to absorb. 4 is the sweet spot:
  // Stage C ~30 min instead of ~20 min, but near-zero deadlocks. Power
  // users on bigger SQL SKUs can still bump to 8 manually.
  const [parallelWorkers, setParallelWorkers] = useState(4)               // 2..8
  // Writer-queue mode (Pattern A): routes all DB writes through ONE thread
  // to eliminate the 4–8 worker deadlocks. null = use backend .env default;
  // true/false = per-run override from this toggle.
  const [useWriterQueue, setUseWriterQueue] = useState(true)
  const [allocBatchId, setAllocBatchId] = useState(null)
  const [allocProgress, setAllocProgress] = useState(null)
  const [allocFailed, setAllocFailed] = useState([])
  const [retryingFailed, setRetryingFailed] = useState(false)
  const allocPollRef = useRef(null)
  // Async-mode tracking: /listing/generate now returns immediately with a
  // session_id and the real work runs in a background thread. We poll the
  // session row to know when it flips from RUNNING to SUCCESS/FAILED.
  const [activeSessionId, setActiveSessionId] = useState(null)
  const [activeSession, setActiveSession] = useState(null)
  const sessionPollRef = useRef(null)

  // Hierarchy gap pre-flight check — surfaces MAJ_CATs in MSA that are
  // missing from ARS_GRID_HIERARCHY (or have NULL grid columns). Listing
  // joins against this table, so gaps cause those MAJ_CATs to drop out.
  const [hierGaps, setHierGaps] = useState(null)
  const [hierGapsDismissed, setHierGapsDismissed] = useState(false)
  const [hierGapsExpanded, setHierGapsExpanded] = useState(false)

  // Park-then-promote: list of sessions awaiting Approve/Reject.
  const [parkedRuns, setParkedRuns] = useState([])
  const [parkedExpanded, setParkedExpanded] = useState(false)
  const [parkedLoading, setParkedLoading] = useState(false)
  const [parkedDetailSid, setParkedDetailSid] = useState(null)
  const [parkedDetail, setParkedDetail] = useState(null)
  const [parkedDetailLoading, setParkedDetailLoading] = useState(false)
  const [parkedActionBusy, setParkedActionBusy] = useState(false)
  // 'alloc' = ARS_ALLOC_PARKED, 'listing' = ARS_LISTING_WORKING_PARKED.
  // Both tables move atomically on Approve/Reject; this just toggles which
  // rows the drawer is currently showing.
  const [parkedDetailWhich, setParkedDetailWhich] = useState('alloc')

  // Top-N chart selectors (top vs bottom + count) for stores & maj_cats
  const [storeRankDir, setStoreRankDir] = useState('top')      // 'top' | 'bottom'
  const [storeRankN,   setStoreRankN]   = useState(10)
  const [majcatRankDir, setMajcatRankDir] = useState('top')
  const [majcatRankN,   setMajcatRankN]   = useState(10)
  const [cancellingBatch, setCancellingBatch] = useState(false)
  // RDC stock-vs-alloc contribution chart — fetched on selectedMajCats change
  const [contribData, setContribData] = useState([])

  // ── Live: backend-detected active job + last-update ticker ──────────
  // activeJob is the server's view of any Python allocation run currently
  // in flight on the backend (regardless of who started it). Polled every
  // few seconds so the page picks up runs initiated outside this tab.
  const [activeJob, setActiveJob] = useState(null)
  const [lastUpdate, setLastUpdate] = useState(Date.now())
  // Re-render every second so "X sec ago" stays live without re-fetching.
  const [now, setNow] = useState(Date.now())
  const activeJobPollRef = useRef(null)

  // (Async/job/cancel facility removed — listing runs synchronously again)

  // loadConfig({ quiet: true }) suppresses the "Failed to load config" toast
  // — for background polls (active-job watcher, post-generate refresh) where
  // a transient backend timeout shouldn't spam the user. Only the initial
  // page-load call and explicit user refreshes pass quiet=false.
  const loadConfig = useCallback(async (opts = {}) => {
    const { quiet = false } = opts
    try {
      const { data } = await listingAPI.config({ quiet })
      setConfig(data.data)
      // Restore saved settings from DB
      const s = data.data?.settings
      if (s) {
        if (s.stock_threshold_pct) setStockThresholdPct(parseFloat(s.stock_threshold_pct))
        if (s.excess_multiplier) setExcessMultiplier(parseFloat(s.excess_multiplier))
        if (s.hold_days) setHoldDays(parseInt(s.hold_days, 10))
        if (s.age_threshold) setAgeThreshold(parseInt(s.age_threshold, 10))
        if (s.mix_mode) setMixMode(s.mix_mode)
        if (s.rdc_mode) setRdcMode(s.rdc_mode)
        if (s.run_mode) setRunMode(s.run_mode)
        if (s.req_weight) setReqWeight(parseFloat(s.req_weight))
        if (s.fill_weight) setFillWeight(parseFloat(s.fill_weight))
        if (s.apply_sec_cap_in_normal !== undefined)
          setApplySecCapInNormal(s.apply_sec_cap_in_normal === 'true' || s.apply_sec_cap_in_normal === true)
        if (s.default_acs_d) setDefaultAcsD(parseFloat(s.default_acs_d))
        if (s.min_size_count) setMinSizeCount(parseInt(s.min_size_count, 10))
        if (s.pri_ct_check_rl !== undefined)
          setPriCheckRL(s.pri_ct_check_rl === 'true' || s.pri_ct_check_rl === true)
        if (s.pri_ct_check_tbc !== undefined)
          setPriCheckTBC(s.pri_ct_check_tbc === 'true' || s.pri_ct_check_tbc === true)
        if (s.rl_mbq_cap_pct !== undefined)  setRlMbqCapPct(parseFloat(s.rl_mbq_cap_pct) || 110)
        if (s.tbc_mbq_cap_pct !== undefined) setTbcMbqCapPct(parseFloat(s.tbc_mbq_cap_pct) || 110)
        if (s.mj_req_growth_pct !== undefined) {
          const v = parseFloat(s.mj_req_growth_pct) || 100
          // Reflect persisted value: 100 → use-default checked; >100 → unchecked + slider.
          setMjReqGrowthPct(v === 100 ? 110 : v)
          setMbqGrowthUseDefault(v === 100)
        }
        if (s.allow_multi_parked !== undefined)
          setAllowMultiParked(s.allow_multi_parked === 'true' || s.allow_multi_parked === true)
      }
    } catch {
      // Only toast for foreground (user-initiated) calls — the api.js
      // interceptor has already suppressed its own toast when quiet=true.
      if (!quiet) toast.error('Failed to load config')
    }
  }, [])

  const loadSummary = useCallback(async (opts = {}) => {
    const { quiet = false } = opts
    try {
      const { data } = await listingAPI.summary({ quiet })
      setSummary(data.data)
    } catch {}
  }, [])

  const loadHierGaps = useCallback(async () => {
    try {
      const { data } = await gridBuilderAPI.hierarchyGaps()
      setHierGaps(data?.data || null)
    } catch {
      // non-fatal — endpoint may not exist on older backends
      setHierGaps(null)
    }
  }, [])

  // Park-then-promote loaders + actions.
  const loadParkedRuns = useCallback(async () => {
    setParkedLoading(true)
    try {
      const { data } = await listingAPI.parkedRuns(false)
      setParkedRuns(data?.runs || [])
    } catch {
      // network/permission failures are non-fatal here
    } finally {
      setParkedLoading(false)
    }
  }, [])

  const fetchParkedDetail = useCallback(async (sid, which) => {
    setParkedDetailLoading(true)
    try {
      const { data } = await listingAPI.parkedRunDetail(sid, {
        page: 1, page_size: 200, which,
      })
      setParkedDetail(data?.data || null)
    } catch {
      setParkedDetail(null)
    } finally {
      setParkedDetailLoading(false)
    }
  }, [])

  const openParkedDetail = useCallback(async (sid) => {
    setParkedDetailSid(sid)
    setParkedDetail(null)
    setParkedDetailWhich('alloc')
    fetchParkedDetail(sid, 'alloc')
  }, [fetchParkedDetail])

  const switchParkedDetailTab = useCallback((which) => {
    if (which === parkedDetailWhich) return
    setParkedDetailWhich(which)
    if (parkedDetailSid) fetchParkedDetail(parkedDetailSid, which)
  }, [parkedDetailWhich, parkedDetailSid, fetchParkedDetail])

  const closeParkedDetail = useCallback(() => {
    setParkedDetailSid(null)
    setParkedDetail(null)
    setParkedDetailWhich('alloc')
  }, [])

  const handleApproveParked = useCallback(async (sid) => {
    if (!sid) return
    if (!window.confirm(
      `Approve session ${sid}?\n\nAll 6 snapshots will move to history and be removed from the parked queue:\n  • ARS_ALLOC_HISTORY\n  • ARS_LISTING_WORKING_HISTORY\n  • ARS_LISTING_HISTORY\n  • ARS_MSA_TOTAL_HISTORY\n  • ARS_MSA_GEN_ART_HISTORY\n  • ARS_MSA_VAR_ART_HISTORY`
    )) return
    setParkedActionBusy(true)
    try {
      const { data } = await listingAPI.approveParked(sid)
      if (data?.already_approved) {
        toast(`Session ${sid} was already approved`, { icon: 'ℹ️' })
      } else {
        const by = data?.by_table || {}
        const total = (data?.approved_rows || 0).toLocaleString()
        const breakdown = Object.entries(by)
          .filter(([, n]) => (n || 0) > 0)
          .map(([k, n]) => `${k}: ${(n || 0).toLocaleString()}`)
          .join(', ')
        toast.success(
          breakdown
            ? `Approved ${total} rows → history (${breakdown})`
            : `Approved ${total} rows → history`
        )
      }
      closeParkedDetail()
      loadParkedRuns()
    } catch {
      // toast already shown by the response interceptor
    } finally {
      setParkedActionBusy(false)
    }
  }, [closeParkedDetail, loadParkedRuns])

  const handleRejectParked = useCallback(async (sid) => {
    if (!sid) return
    const note = window.prompt(
      `Reject session ${sid}? Rows stay in ARS_ALLOC_PARKED with PARK_STATUS='REJECTED'. Optional note:`,
      ''
    )
    if (note === null) return  // user cancelled
    setParkedActionBusy(true)
    try {
      const { data } = await listingAPI.rejectParked(sid, note)
      toast(
        `Rejected ${(data?.rejected_rows || 0).toLocaleString()} rows`,
        { icon: '🚫' }
      )
      closeParkedDetail()
      loadParkedRuns()
    } catch {
      // toast already shown
    } finally {
      setParkedActionBusy(false)
    }
  }, [closeParkedDetail, loadParkedRuns])

  useEffect(() => { loadConfig(); loadSummary(); loadParkedRuns(); loadHierGaps() }, [])

  // Auto-detect RDC(s) from selected stores via store_rdc_map
  const storeRdcMap = config?.store_rdc_map || {}
  const autoRdcs = [...new Set((selectedStores || []).map(s => storeRdcMap[s]).filter(Boolean))]
  const otherRdcs = (config?.rdcs || []).filter(r => !autoRdcs.includes(r))

  const handlePause = () => {
    setPaused(p => !p)
    toast(paused ? 'Resumed' : 'Paused — click Resume to continue', { icon: paused ? '\u25b6' : '\u23f8' })
  }

  const handleForceStop = async () => {
    // 1. Abort the local in-flight HTTP request to /listing/generate.
    //    Important: this only stops the browser\u2192server connection \u2014 the
    //    backend already spawned a worker thread which keeps running
    //    until we explicitly tell it to die.
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }

    // 2. Tell the backend to actually kill the running job.
    //    - cancelBatch: hard-cancel \u2014 sets the in-process cancel event so
    //      worker threads exit, KILLs each worker's SQL Server SPID, marks
    //      PENDING/IN_PROGRESS queue rows FAILED. (Only useful once Stage
    //      C has started; before that no alloc batch exists.)
    //    - killSession: marks the session row FAILED. Works in any stage,
    //      including Stage A/B "preparing..." where no batch exists yet.
    const stops = []
    if (allocBatchId)    stops.push(['batch',   listingAPI.cancelBatch(allocBatchId)])
    if (activeSessionId) stops.push(['session', listingAPI.killSession(activeSessionId)])

    // Fallback: if neither id is set locally (e.g. user reloaded the page
    // mid-run), discover the active session via /active-job and kill that.
    if (stops.length === 0) {
      try {
        const { data } = await listingAPI.activeJob()
        const sid = data?.session_id || data?.data?.session_id
        if (sid) stops.push(['session', listingAPI.killSession(sid)])
      } catch { /* no active job \u2014 local-only stop */ }
    }

    if (stops.length > 0) {
      const results = await Promise.allSettled(stops.map(([, p]) => p))
      const failed  = results.filter(r => r.status === 'rejected')
      if (failed.length === stops.length) {
        toast.error(failed[0].reason?.response?.data?.detail || 'Stop failed')
      } else if (failed.length) {
        toast(`Stopped \u2014 ${stops.length - failed.length}/${stops.length} kill calls succeeded`, { icon: '\u26a0' })
      } else {
        toast.success('Stopped \u2014 backend job killed', { icon: '\u23f9' })
      }
    } else {
      toast('Stopped (local only \u2014 no active backend job)', { icon: '\u23f9' })
    }

    setGenerating(false)
    setPaused(false)
    setActiveJob(null)
    // Clear stale local refs so the activeJob 5s poll's `!allocBatchId`
    // guard correctly admits a fresh run later (and doesn't re-display
    // the just-cancelled batch as RUNNING). The session-status poll's
    // useEffect will tear itself down when activeSessionId becomes null.
    setAllocBatchId(null)
    setActiveSessionId(null)
    setActiveSession(null)
    setAllocProgress(null)
    setAllocFailed([])
    // Refresh active-job poll so the banner clears immediately
    try {
      const { data } = await listingAPI.activeJob()
      setActiveJob(data?.data || data)
    } catch { /* ignore */ }
  }

  const handleGenerate = async () => {
    if (parkedRuns.length > 0 && !allowMultiParked) {
      toast.error('A parked session is awaiting review — approve or reject it from the Parked Runs section, or enable "Allow multiple parked" to stack snapshots.')
      return
    }
    const missingCount = hierGaps?.missing?.length || 0
    if (missingCount > 0 && !hierGapsDismissed) {
      toast.error(`${missingCount} MAJ_CATs are missing from ARS_GRID_HIERARCHY and will be skipped. Review the banner above and click "Generate Anyway" to proceed.`)
      setHierGapsExpanded(true)
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setGenerating(true)
    try {
      const payload = {
        rdc_mode: rdcMode,
        store_codes: selectedStores,
        maj_cat_values: selectedMajCats,
        run_mode: runMode,
        mix_mode: mixMode,
        stock_threshold_pct: parseFloat(stockThresholdPct) || 0.6,
        excess_multiplier: parseFloat(excessMultiplier) || 2.0,
        hold_days: parseInt(holdDays, 10) || 0,
        age_threshold: parseInt(ageThreshold, 10) || 15,
        req_weight: parseFloat(reqWeight) || 0.4,
        fill_weight: parseFloat(fillWeight) || 0.6,
        apply_sec_cap_in_normal: !!applySecCapInNormal,
        default_acs_d: parseFloat(defaultAcsD) || 18,
        min_size_count: enableMinSize ? (parseInt(minSizeCount, 10) || 3) : 0,
        pri_ct_check_rl: !!priCheckRL,
        pri_ct_check_tbc: !!priCheckTBC,
        // Checkbox forces strict 100%; otherwise the slider value applies.
        mj_req_growth_pct: mbqGrowthUseDefault ? 100 : (parseFloat(mjReqGrowthPct) || 100),
        // Per-OPT_TYPE dispatch caps.  PRI ≥ 100% toggle action:
        //   • PRI ≥ 100% (RL) ON  → RL cap = Grid MBQ Growth % (default)
        //   • PRI ≥ 100% (RL) OFF → RL cap = user-set "RL Dispatch Cap %" slider
        //   • Same for TBC
        //   • TBL has no PRI toggle and ALWAYS tracks growth (default)
        // The Dispatch Cap slider is rendered only when the PRI toggle is
        // OFF — that's the path that actually consumes its value.
        rl_mbq_cap_pct:  priCheckRL
          ? (mbqGrowthUseDefault ? 100 : (parseFloat(mjReqGrowthPct) || 100))
          : (parseFloat(rlMbqCapPct)  || 100),
        tbc_mbq_cap_pct: priCheckTBC
          ? (mbqGrowthUseDefault ? 100 : (parseFloat(mjReqGrowthPct) || 100))
          : (parseFloat(tbcMbqCapPct) || 100),
        tbl_mbq_cap_pct: mbqGrowthUseDefault ? 100 : (parseFloat(mjReqGrowthPct) || 100),
        allocation_mode: allocationMode,
        parallel_workers: parseInt(parallelWorkers, 10) || 8,
        use_writer_queue: useWriterQueue,
        ssn_values: selectedSsn,
        opt_types: ({ all: ['RL','TBC','TBL'], rl: ['RL'], rl_tbc: ['RL','TBC'] })[allocOtFilter] || ['RL','TBC','TBL'],
        allow_multi_parked: !!allowMultiParked,
      }
      if (rdcMode === 'own') {
        payload.rdc_values = autoRdcs
      } else if (rdcMode === 'cross') {
        payload.cross_from = crossFrom
        payload.cross_to = autoRdcs
      }
      // Reset previous batch state so the progress panel doesn't show stale data.
      setAllocBatchId(null); setAllocProgress(null); setAllocFailed([])
      // Async: backend returns {session_id, alloc_batch_id, status:'RUNNING'}
      // within milliseconds and runs the actual work in a thread. We then
      // poll /listing/sessions/{id} for overall status and (for parallel
      // modes) /listing/alloc-progress for per-MAJ_CAT progress.
      const { data } = await listingAPI.generate(payload, { signal: controller.signal })
      const newSessionId = data?.data?.session_id || null
      const newBatchId   = data?.data?.alloc_batch_id || null
      toast.success(data.message || 'Generation started in background')
      if (newSessionId) setActiveSessionId(newSessionId)
      if (newBatchId)   setAllocBatchId(newBatchId)
      // Immediate first fetches so panels populate without waiting 3s.
      if (newBatchId) {
        try {
          const { data: pd } = await listingAPI.allocProgress(newBatchId)
          setAllocProgress(pd?.progress || null)
          setAllocFailed(pd?.failed || [])
        } catch { /* ignore */ }
      }
    } catch (e) {
      if (e.name === 'CanceledError' || e.code === 'ERR_CANCELED') {
        // Force stop — already handled in handleForceStop
      } else {
        toast.error(e.response?.data?.detail || 'Generate failed')
      }
      setGenerating(false)
    }
    // Note: we do NOT setGenerating(false) on success — the background
    // job is still running. The session-status poll below clears it
    // when the row flips from RUNNING to SUCCESS/FAILED.
  }

  // ── Live progress polling for the current allocation batch ──────────
  // Polls /listing/alloc-progress every 3s while a parallel run is in
  // flight. Stops automatically once nothing is PENDING / IN_PROGRESS.
  useEffect(() => {
    if (!allocBatchId) return
    if (!generating && allocProgress &&
        allocProgress.pending === 0 && allocProgress.in_progress === 0) {
      return  // already complete — no need to poll
    }
    const tick = async () => {
      try {
        const { data } = await listingAPI.allocProgress(allocBatchId)
        setAllocProgress(data?.progress || null)
        setAllocFailed(data?.failed || [])
        setLastUpdate(Date.now())
        if (data?.progress &&
            data.progress.pending === 0 && data.progress.in_progress === 0) {
          if (allocPollRef.current) {
            clearInterval(allocPollRef.current)
            allocPollRef.current = null
          }
        }
      } catch { /* swallow — keep polling */ }
    }
    tick()
    allocPollRef.current = setInterval(tick, 3000)
    return () => {
      if (allocPollRef.current) {
        clearInterval(allocPollRef.current)
        allocPollRef.current = null
      }
    }
  }, [allocBatchId, generating])

  // ── Session-status polling (async generate flow) ─────────────────────
  // /listing/generate now returns immediately and the real work runs in a
  // background thread. Poll the session row every 3s. When STATUS flips
  // out of RUNNING (SUCCESS or FAILED), clear the in-flight UI state and
  // refresh the page-level data.
  useEffect(() => {
    if (!activeSessionId) return
    const tick = async () => {
      try {
        const { data } = await listingAPI.session(activeSessionId)
        const sess = data?.session || null
        setActiveSession(sess)
        if (sess && sess.status && sess.status !== 'RUNNING') {
          if (sessionPollRef.current) {
            clearInterval(sessionPollRef.current)
            sessionPollRef.current = null
          }
          setGenerating(false)
          setPaused(false)
          if (sess.status === 'SUCCESS') {
            const parkedSuffix = sess.parked_status === 'PARKED'
              ? ' — parked for review'
              : sess.parked_status === 'SKIPPED_ERROR'
                ? ' (parking skipped — see logs)'
                : ''
            const msg = `Listing complete: ${
              (sess.alloc_rows || 0).toLocaleString()} rows in ${
              sess.duration_sec != null ? sess.duration_sec.toFixed(1) + 's' : '—'
            }${parkedSuffix}`
            if (sess.parked_status === 'SKIPPED_ERROR') {
              toast(msg, { icon: '⚠️' })
            } else {
              toast.success(msg)
            }
            // Refresh page data now that work is done.
            loadConfig(); loadSummary(); setColFilters({}); loadPreview(1, {})
            loadParkedRuns()
          } else if (sess.status === 'CANCELLED') {
            // User explicitly cancelled — show a neutral toast (not an
            // error). The cancel is permanent: queue rows are CANCELLED
            // (not FAILED), so retry-failed cannot resurrect them.
            toast(`Listing cancelled: ${sess.error_msg || 'stopped by user'}`,
                  { icon: '⏹' })
          } else {
            toast.error(`Listing FAILED: ${sess.error_msg || 'unknown error'}`)
          }
        }
      } catch { /* keep polling */ }
    }
    tick()
    sessionPollRef.current = setInterval(tick, 3000)
    return () => {
      if (sessionPollRef.current) {
        clearInterval(sessionPollRef.current)
        sessionPollRef.current = null
      }
    }
  }, [activeSessionId])

  // ── Detect any Python job already running on the server ────────────
  // Calls /listing/active-job on mount and every 5s. If the server reports
  // an in-flight batch and we don't already have one locally, adopt it so
  // the Live Run dashboard updates without the user having to refresh.
  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const { data } = await listingAPI.activeJob()
        if (cancelled) return
        let job = data?.active || null
        // Defensive: if the backend reports a job whose linked session has
        // been CANCELLED, treat it as inactive. Belt-and-braces against any
        // future regression where queue rows are wrongly left in PENDING/
        // IN_PROGRESS for a cancelled session.
        if (job?.batch_id) {
          try {
            const { data: sd } = await listingAPI.session(job.batch_id)
            const status = sd?.session?.status
            if (status === 'CANCELLED' || status === 'FAILED') job = null
          } catch { /* keep job — best-effort guard */ }
        }
        setActiveJob(job)
        setLastUpdate(Date.now())
        if (job && !allocBatchId) {
          setAllocBatchId(job.batch_id)
          setAllocProgress(job.progress || null)
          setAllocFailed(job.failed || [])
          if (job.mode) setAllocationMode(job.mode)
        }
      } catch { /* ignore — keep polling */ }
    }
    tick()
    activeJobPollRef.current = setInterval(tick, 5000)
    return () => {
      cancelled = true
      if (activeJobPollRef.current) {
        clearInterval(activeJobPollRef.current)
        activeJobPollRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── 1s ticker so "updated Xs ago" stays live without extra fetches ─
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  // ── RDC stock vs alloc contribution — refetch when MAJ_CAT selection
  //    changes or alloc completion ticks. Uses the live summary as a
  //    revision marker so the chart updates as new MAJ_CATs DONE.
  useEffect(() => {
    let cancelled = false
    const fetchContrib = async () => {
      if (!config?.listing_exists) { setContribData([]); return }
      try {
        const { data } = await listingAPI.contribution(selectedMajCats || [])
        if (!cancelled) setContribData(data?.data || [])
      } catch { /* quiet — error toast already suppressed */ }
    }
    fetchContrib()
    return () => { cancelled = true }
  }, [
    config?.listing_exists,
    selectedMajCats.join('|'),
    summary?.alloc_rows,        // refetch as more rows allocate
    allocProgress?.done,        // refetch as MAJ_CATs complete
  ])

  const handleCancelBatch = async () => {
    if (!allocBatchId) return
    if (!window.confirm(`Force-cancel batch ${allocBatchId}? Pending/in-progress MAJ_CATs will be marked FAILED.`)) return
    setCancellingBatch(true)
    try {
      const { data } = await listingAPI.cancelBatch(allocBatchId)
      toast.success(`Cancelled ${data.cancelled} row(s)`)
      try {
        const { data: pd } = await listingAPI.allocProgress(allocBatchId)
        setAllocProgress(pd?.progress || null)
        setAllocFailed(pd?.failed || [])
      } catch { /* ignore */ }
      setActiveJob(null)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Cancel failed')
    } finally {
      setCancellingBatch(false)
    }
  }

  const handleRetryFailed = async () => {
    if (!allocBatchId) return
    setRetryingFailed(true)
    try {
      const { data } = await listingAPI.retryFailed({
        batch_id: allocBatchId,
        allocation_mode: allocationMode,
        parallel_workers: parseInt(parallelWorkers, 10) || 8,
      })
      // The backend now returns one of:
      //   { retried > 0,  still_failed, progress, failed }  → real retry happened
      //   { retried = 0,  message }                          → nothing was failed (in-flight)
      const retried = data.retried || 0
      const stillFailed = data.still_failed
      if (retried === 0 && data.message) {
        // Already-running case — show backend's explanation, don't claim success.
        toast(data.message, { icon: 'i', duration: 6000 })
      } else if (stillFailed != null && stillFailed > 0) {
        toast.error(
          `Retried ${retried} MAJ_CAT(s); ${stillFailed} still failed. Check logs.`,
          { duration: 8000 },
        )
      } else {
        toast.success(`Retried ${retried} failed MAJ_CAT(s) — all succeeded.`)
      }
      // Push the post-retry progress/failed straight from the response so
      // the UI updates immediately, even if the next poll is a few seconds
      // away. Fall back to a fresh poll if the backend didn't include them.
      if (data.progress) {
        setAllocProgress(data.progress)
        setAllocFailed(data.failed || [])
      } else {
        try {
          const { data: pd } = await listingAPI.allocProgress(allocBatchId)
          setAllocProgress(pd?.progress || null)
          setAllocFailed(pd?.failed || [])
        } catch { /* ignore */ }
      }
      loadSummary()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Retry failed')
    } finally {
      setRetryingFailed(false)
    }
  }

  const getActiveFilters = (overrideFilters) => {
    const f = overrideFilters !== undefined ? overrideFilters : colFilters
    const active = {}
    for (const [k, v] of Object.entries(f)) {
      if (v && v.trim()) active[k] = v.trim()
    }
    return active
  }

  const loadPreview = async (page = 1, overrideFilters, overrideSearch, overrideTable) => {
    setLoading(true)
    try {
      const tbl = overrideTable || previewTable
      const params = { page, page_size: previewPageSize, table: tbl }
      const active = getActiveFilters(overrideFilters)
      if (Object.keys(active).length > 0) params.filters = JSON.stringify(active)
      const srch = overrideSearch !== undefined ? overrideSearch : globalSearch
      if (srch && srch.trim()) params.search = srch.trim()
      const { data } = await listingAPI.preview(params)
      setPreview(data.data)
      setPreviewPage(page)
    } catch (e) {
      if (e.response?.status === 404) setPreview(null)
      else toast.error('Failed to load preview')
    } finally { setLoading(false) }
  }

  const handleFilterKeyDown = (e) => {
    if (e.key === 'Enter') loadPreview(1)
  }

  const clearAllFilters = () => {
    setColFilters({})
    loadPreview(1, {})
  }

  const handleExport = async () => {
    try {
      toast.loading('Exporting...', { id: 'export' })
      const params = { table: previewTable }
      const active = getActiveFilters()
      if (Object.keys(active).length > 0) params.filters = JSON.stringify(active)
      const { data } = await listingAPI.export(params)
      const url = URL.createObjectURL(data)
      const a = document.createElement('a')
      a.href = url
      a.download = previewTable === 'working' ? 'ARS_LISTING_WORKING.xlsx'
                 : previewTable === 'alloc'   ? 'ARS_ALLOC_WORKING.xlsx'
                 : 'ARS_LISTING.xlsx'
      a.click()
      URL.revokeObjectURL(url)
      toast.success('Export complete', { id: 'export' })
    } catch (e) {
      toast.error('Export failed', { id: 'export' })
    }
  }

  // no manual RDC toggle needed — auto-detected from stores

  const totalPages = preview ? Math.ceil(preview.total / preview.page_size) : 0
  const hasColFilters = Object.values(colFilters).some(v => v && v.trim())

  const _btn = (active, color = C.primary) => ({
    height: 24, fontSize: 9, fontWeight: 700, borderRadius: 4, cursor: 'pointer', padding: '0 10px',
    background: active ? color : '#fff', color: active ? '#fff' : C.textSub,
    border: `1px solid ${active ? color : '#e2e8f0'}`,
  })
  const _lbl = { fontSize: 8, fontWeight: 600, color: C.textSub, marginBottom: 2, letterSpacing: '.03em' }
  const _inp = { height: 22, fontSize: 11, fontWeight: 700, textAlign: 'center', borderRadius: 4,
    border: `1px solid ${C.inputBd}`, background: C.inputBg, padding: '0 4px', width: '100%' }
  const _card = { background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 6, padding: '8px 10px' }

  // Chart data derivation
  // OPT_TYPE color map: RL=green, NL=purple, TBL=blue, TBC=amber, MIX=red, UNTAGGED=grey
  const OPT_COLOR = { RL: '#059669', NL: '#7c3aed', TBL: '#2563eb', TBC: '#d97706', MIX: '#dc2626', UNTAGGED: '#9ca3af' }
  const PIE_COLORS_FALLBACK = ['#059669', '#2563eb', '#d97706', '#dc2626', '#7c3aed', '#06b6d4']
  const optTypeChartData = summary?.by_opt_type
    ? Object.entries(summary.by_opt_type).map(([k, v]) => ({ name: k, value: v, color: OPT_COLOR[k] || '#9ca3af' }))
    : []
  const allocChartData = summary?.alloc_by_opt_type
    ? Object.entries(summary.alloc_by_opt_type).filter(([, v]) => v > 0).map(([k, v]) => ({ name: k, qty: v, color: OPT_COLOR[k] || '#4f46e5' }))
    : []

  // Derived metrics for the new layout — REQ/MBQ/ALLOC_QTY/HOLD_QTY are
  // backed by FLOAT in SQL but represent unit counts, so round to int
  // before display.
  const totalAllocQty = Math.round(summary?.by_rdc ? summary.by_rdc.reduce((s,r) => s + (r.alloc_qty || 0), 0) : 0)
  const totalHoldQty  = Math.round(summary?.totals?.hold_qty ?? (summary?.by_rdc ? summary.by_rdc.reduce((s,r) => s + (r.hold_qty || 0), 0) : 0))
  const holdByRdc     = (summary?.by_rdc || []).map(r => ({ rdc: String(r.rdc ?? ''), hold_qty: r.hold_qty || 0 }))
  const newPct = summary?.totals?.total ? Math.round((summary.totals.new / summary.totals.total) * 100) : 0
  const allocPct = summary?.totals?.total && summary?.alloc_rows ? Math.round((summary.alloc_rows / summary.totals.total) * 100) : 0
  const avgPerStore = summary?.totals?.stores ? Math.round(summary.totals.total / summary.totals.stores) : 0

  // Top/Bottom MAJ_CAT chart — sorted on the fly per current selectors
  const sortedMajCats = [...(summary?.by_maj_cat || [])]
    .sort((a, b) => (b.alloc_qty || 0) - (a.alloc_qty || 0))
  const rankedMajCats = (majcatRankDir === 'top'
    ? sortedMajCats.slice(0, majcatRankN)
    : sortedMajCats.slice(-majcatRankN)
  ).reverse() // largest at top in horizontal bar

  // Top/Bottom store chart — same shape, derived from summary.by_store
  const sortedStores = [...(summary?.by_store || [])]
    .sort((a, b) => (b.alloc_qty || 0) - (a.alloc_qty || 0))
  const rankedStores = (storeRankDir === 'top'
    ? sortedStores.slice(0, storeRankN)
    : sortedStores.filter(s => (s.alloc_qty || 0) > 0).slice(-storeRankN)
  ).reverse()

  // Store-status chart (STSTATUS from Master_ALC_INPUT_ST_MASTER)
  const STSTATUS_COLORS = ['#059669', '#2563eb', '#d97706', '#7c3aed', '#dc2626', '#06b6d4', '#9ca3af']
  const storeStatusChartData = (summary?.by_store_status || [])
    .map((r, i) => ({ name: r.status, value: r.count, color: STSTATUS_COLORS[i % STSTATUS_COLORS.length] }))

  // Hub-wise allocation chart (joined via store master HUB column)
  const hubChartData = (summary?.by_hub || [])
    .filter(r => (r.alloc_qty || 0) > 0 || (r.hold_qty || 0) > 0)
    .map(r => ({ hub: r.hub, alloc_qty: r.alloc_qty || 0, hold_qty: r.hold_qty || 0 }))

  const hierMissing = hierGaps?.missing || []
  const hierPartial = hierGaps?.partial || []
  const hierHasGaps = hierMissing.length > 0 || hierPartial.length > 0

  return (
    <div style={{ color: C.text, fontFamily: 'inherit', display: 'flex', flexDirection: 'column', gap: 10, padding: '4px 2px' }}>

      {/* ═══════════ ARS_GRID_HIERARCHY Pre-Flight Banner ═══════════ */}
      {hierHasGaps && (
        <div style={{
          background: hierGapsDismissed ? '#fffbeb' : '#fef3c7',
          border: `1px solid ${hierGapsDismissed ? '#fcd34d' : '#f59e0b'}`,
          borderLeft: `4px solid #d97706`,
          borderRadius: 8, padding: '10px 14px',
          boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <AlertTriangle size={18} color="#b45309" style={{ flexShrink: 0 }}/>
            <div style={{ flex: 1, fontSize: 12, color: '#78350f' }}>
              <span style={{ fontWeight: 700, color: '#92400e' }}>
                {hierMissing.length > 0 && `${hierMissing.length} MAJ_CAT${hierMissing.length === 1 ? '' : 's'} missing`}
                {hierMissing.length > 0 && hierPartial.length > 0 && ' · '}
                {hierPartial.length > 0 && `${hierPartial.length} partial`}
                {' '}from ARS_GRID_HIERARCHY
              </span>
              <span style={{ marginLeft: 6, color: '#78350f' }}>
                — these will be skipped during listing. Run the relevant grids in Grid Builder to populate them.
              </span>
              {hierGaps?.expected != null && (
                <span style={{ marginLeft: 6, color: '#a16207', fontWeight: 600 }}>
                  ({hierGaps.covered}/{hierGaps.expected} covered)
                </span>
              )}
            </div>
            <button onClick={() => setHierGapsExpanded(v => !v)}
              style={{ height: 26, padding: '0 10px', fontSize: 11, fontWeight: 700,
                background: '#fff', color: '#92400e', border: '1px solid #fcd34d',
                borderRadius: 5, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4 }}>
              {hierGapsExpanded ? <ChevronUp size={12}/> : <ChevronDown size={12}/>}
              {hierGapsExpanded ? 'Hide' : 'Details'}
            </button>
            <button onClick={() => navigate('/data-prep/store-stock')}
              style={{ height: 26, padding: '0 10px', fontSize: 11, fontWeight: 700,
                background: '#d97706', color: '#fff', border: 'none',
                borderRadius: 5, cursor: 'pointer' }}>
              Grid Builder →
            </button>
            <button onClick={loadHierGaps}
              title="Re-check after running grids"
              style={{ height: 26, width: 26, background: '#fff', color: '#92400e',
                border: '1px solid #fcd34d', borderRadius: 5, cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <RefreshCw size={12}/>
            </button>
            <button onClick={() => setHierGapsDismissed(true)}
              title="Acknowledge and allow Generate anyway"
              style={{ height: 26, padding: '0 10px', fontSize: 10, fontWeight: 700,
                background: hierGapsDismissed ? '#fbbf24' : '#fff',
                color: hierGapsDismissed ? '#fff' : '#92400e',
                border: '1px solid #fcd34d', borderRadius: 5,
                cursor: hierGapsDismissed ? 'default' : 'pointer' }}
              disabled={hierGapsDismissed}>
              {hierGapsDismissed ? '✓ Acknowledged' : 'Generate Anyway'}
            </button>
          </div>

          {hierGapsExpanded && (
            <div style={{ marginTop: 10, paddingTop: 10, borderTop: '1px dashed #fcd34d',
              display: 'grid', gridTemplateColumns: hierPartial.length > 0 ? '1fr 1fr' : '1fr', gap: 12 }}>
              {hierMissing.length > 0 && (
                <div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#92400e',
                    textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4 }}>
                    Missing ({hierMissing.length})
                  </div>
                  <div style={{ maxHeight: 120, overflowY: 'auto', display: 'flex',
                    flexWrap: 'wrap', gap: 4, padding: 4, background: '#fff',
                    border: '1px solid #fde68a', borderRadius: 4 }}>
                    {hierMissing.map(mc => (
                      <span key={mc} style={{
                        fontSize: 10, fontWeight: 700, padding: '2px 6px',
                        background: '#fef3c7', color: '#92400e',
                        borderRadius: 3, fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
                      }}>{mc}</span>
                    ))}
                  </div>
                </div>
              )}
              {hierPartial.length > 0 && (
                <div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: '#92400e',
                    textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4 }}>
                    Partial — NULL grid columns ({hierPartial.length})
                  </div>
                  <div style={{ maxHeight: 120, overflowY: 'auto',
                    background: '#fff', border: '1px solid #fde68a', borderRadius: 4 }}>
                    {hierPartial.map(p => (
                      <div key={p.maj_cat} style={{
                        display: 'flex', gap: 6, padding: '3px 6px', fontSize: 10,
                        borderBottom: '1px solid #fef3c7' }}>
                        <span style={{ fontWeight: 700, color: '#92400e',
                          fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
                          minWidth: 80 }}>{p.maj_cat}</span>
                        <span style={{ color: '#a16207' }}>
                          {(p.null_cols || []).join(', ')}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ═══════════ Page Header + Primary Actions ═══════════ */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: 'linear-gradient(135deg, #fff 0%, #f8fafc 100%)',
        border: `1px solid ${C.cardBorder}`, borderRadius: 10, padding: '10px 14px',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
      }}>
        <div>
          <h1 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ width: 28, height: 28, borderRadius: 7, background: `linear-gradient(135deg, ${C.primary}, #7c3aed)`,
              display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 6px rgba(79,70,229,0.3)' }}>
              <List size={14} color="#fff"/>
            </div>
            Listing Generation &amp; Allocation
          </h1>
          <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4, paddingLeft: 38 }}>
            Score, rank, list, and allocate options across stores · output → ARS_LISTING / ARS_LISTING_WORKING / ARS_ALLOC_WORKING
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {generating ? (
            <>
              <button onClick={handlePause}
                style={{ height: 38, borderRadius: 8, fontSize: 12, fontWeight: 700, color: '#fff', padding: '0 16px', cursor: 'pointer',
                  background: paused ? 'linear-gradient(135deg, #059669, #047857)' : 'linear-gradient(135deg, #d97706, #b45309)',
                  border: 'none', display: 'flex', alignItems: 'center', gap: 6,
                  boxShadow: paused ? '0 2px 6px rgba(5,150,105,0.3)' : '0 2px 6px rgba(217,119,6,0.3)' }}>
                {paused ? <><Play size={14}/> Resume</> : <><Pause size={14}/> Pause</>}
              </button>
              <button onClick={handleForceStop}
                style={{ height: 38, borderRadius: 8, fontSize: 12, fontWeight: 700, color: '#fff', padding: '0 16px', cursor: 'pointer',
                  background: 'linear-gradient(135deg, #dc2626, #b91c1c)',
                  border: 'none', display: 'flex', alignItems: 'center', gap: 6,
                  boxShadow: '0 2px 6px rgba(220,38,38,0.3)' }}>
                <Square size={13}/> Stop
              </button>
            </>
          ) : (
            <>
              {/* Allocation-mode selector — only relevant when allocation runs.
                  Compact inline group to the left of Generate. */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: 6, padding: '4px 10px',
                background: '#fff', border: `1px solid ${C.cardBorder}`,
                borderRadius: 8, height: 38,
              }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: C.textMuted,
                  textTransform: 'uppercase', letterSpacing: 0.4 }}>OPT Types</span>
                {[['all','All (RL→TBC→TBL)'],['rl','RL only'],['rl_tbc','RL + TBC']].map(([val, label]) => (
                  <button key={val} onClick={() => setAllocOtFilter(val)}
                    style={{ height: 26, padding: '0 9px', fontSize: 10, fontWeight: 600,
                      borderRadius: 5, cursor: 'pointer',
                      border: allocOtFilter === val ? `1.5px solid #4f46e5` : `1px solid ${C.cardBorder}`,
                      background: allocOtFilter === val ? '#ede9fe' : '#fff',
                      color: allocOtFilter === val ? '#4f46e5' : C.textSub }}>
                    {label}
                  </button>
                ))}
                <span style={{ fontSize: 10, fontWeight: 700, color: C.textMuted,
                  textTransform: 'uppercase', letterSpacing: 0.4 }}>Alloc</span>
                <select value={allocationMode}
                  onChange={(e) => setAllocationMode(e.target.value)}
                  style={{ height: 26, fontSize: 11, fontWeight: 600,
                    border: `1px solid ${C.cardBorder}`, borderRadius: 6,
                    padding: '0 6px', background: '#fff', color: C.text,
                    cursor: 'pointer' }}>
                  <option value="pandas">Pandas (in-memory)</option>
                  <option value="sequential">Sequential (fallback)</option>
                </select>
                {allocationMode !== 'sequential' && (
                  <>
                    <span style={{ fontSize: 10, color: C.textMuted }}
                      title="Number of parallel worker threads. Capped at 8 — more would saturate the Python GIL in this uvicorn process and freeze unrelated requests like /auth/login.">
                      workers
                    </span>
                    <input type="number" min={2} max={8}
                      value={parallelWorkers}
                      onChange={(e) => setParallelWorkers(e.target.value)}
                      style={{ width: 48, height: 26, fontSize: 11,
                        border: `1px solid ${C.cardBorder}`, borderRadius: 6,
                        padding: '0 6px', textAlign: 'center' }}/>
                    {/* Writer-queue toggle (Pattern A) — routes all DB writes
                        through one thread to eliminate worker-worker deadlocks.
                        Recommended ON for 4+ workers. */}
                    <span style={{ fontSize: 10, fontWeight: 700, color: C.textMuted,
                      textTransform: 'uppercase', letterSpacing: 0.4, marginLeft: 4 }}
                      title="Routes all DB writes through a single thread to prevent the &quot;deadlocked on lock | generic waitable object&quot; failures. Recommended ON for 4+ workers. OFF reverts to the legacy direct-write path.">
                      Writer-Q
                    </span>
                    <button type="button"
                      onClick={() => setUseWriterQueue(v => !v)}
                      title={useWriterQueue
                        ? 'ON — single-writer mode (no deadlocks). Click to switch off.'
                        : 'OFF — legacy mode (workers write directly, risk of deadlocks). Click to switch on.'}
                      style={{ height: 26, padding: '0 10px', fontSize: 10, fontWeight: 700,
                        borderRadius: 5, cursor: 'pointer',
                        border: `1.5px solid ${useWriterQueue ? '#059669' : C.cardBorder}`,
                        background: useWriterQueue ? '#d1fae5' : '#fff',
                        color: useWriterQueue ? '#065f46' : C.textSub,
                        display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%',
                        background: useWriterQueue ? '#059669' : '#cbd5e1' }}/>
                      {useWriterQueue ? 'ON' : 'OFF'}
                    </button>
                  </>
                )}
              </div>
              {/* Parking mode moved to Settings → Application (admin-only).
                  `allowMultiParked` is still loaded from /listing/config and
                  read by the run-block logic below. */}
              {(() => {
                const parkedBlocks = parkedRuns.length > 0 && !allowMultiParked
                return (
                  <button onClick={handleGenerate}
                    disabled={parkedBlocks}
                    title={parkedBlocks ? 'A parked session is awaiting review — approve/reject it, or ask an admin to switch Parking to Multiple in Settings → Application' : undefined}
                    style={{ height: 38, borderRadius: 8, fontSize: 13, fontWeight: 700, color: '#fff', padding: '0 22px',
                      cursor: parkedBlocks ? 'not-allowed' : 'pointer',
                      background: parkedBlocks ? '#94a3b8' : runMode === 'full' ? 'linear-gradient(135deg, #7c3aed, #9333ea)' : 'linear-gradient(135deg, #4f46e5, #7c3aed)',
                      border: 'none', display: 'flex', alignItems: 'center', gap: 6,
                      boxShadow: parkedBlocks ? 'none' : '0 3px 8px rgba(79,70,229,0.3)',
                      opacity: parkedBlocks ? 0.7 : 1 }}>
                    <Play size={15}/> Generate {runMode === 'full' ? 'Full Pipeline' : 'Listing'}
                  </button>
                )
              })()}
              {config?.listing_exists && (
                <button onClick={handleExport}
                  style={{ height: 38, borderRadius: 8, fontSize: 12, fontWeight: 600, color: C.green, padding: '0 14px',
                    background: '#fff', border: `1px solid ${C.greenBd}`, cursor: 'pointer',
                    display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Download size={13}/> Export
                </button>
              )}
              <button onClick={() => navigate('/data-prep/listing/logs')}
                title="Review past Generate sessions and per-session logs"
                style={{ height: 38, borderRadius: 8, fontSize: 12, fontWeight: 600,
                  color: C.text, padding: '0 14px',
                  background: '#fff', border: `1px solid ${C.cardBorder}`,
                  cursor: 'pointer',
                  display: 'flex', alignItems: 'center', gap: 6 }}>
                <FileText size={13}/> View Logs
              </button>
            </>
          )}
        </div>
      </div>

      {/* ═══════════ KPI Tiles — top-line numbers ═══════════ */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 6 }}>
        <KpiTile icon={Database} label="MSA" value={config?.msa_gen_art_rows} accent="#0891b2"
          sub={(() => {
            const mcCount = (summary?.by_maj_cat || []).length
            const qtyPart = summary?.msa_qty != null
              ? `${(summary.msa_qty || 0).toLocaleString()} qty · gen-art`
              : 'gen-art rows'
            return mcCount > 0 ? `${qtyPart} · ${mcCount} MAJ_CATs` : qtyPart
          })()}
          onClick={(summary?.by_maj_cat || []).length > 0 ? () => setMajCatModalOpen(true) : undefined}/>
        <KpiTile icon={Database} label="Grid" value={config?.grid_gen_art_rows} accent="#0891b2" sub="grid rows"/>
        {/* Stores: show "listed / active" — e.g. 5 / 346 active */}
        <KpiTile icon={List} label="Stores"
          value={summary?.listed_store_count ?? config?.store_count}
          accent={C.blue}
          sub={summary?.listed_store_count != null
            ? `${(summary.listed_store_count || 0).toLocaleString()} of ${(summary?.active_store_count ?? config?.store_count ?? 0).toLocaleString()} active`
            : `${(config?.store_count || 0).toLocaleString()} active`}
          onClick={(summary?.by_store || []).length > 0 ? () => setStoreModalOpen(true) : undefined}/>
        <KpiTile icon={List} label="Listing"
          value={config?.listing_exists ? (config?.listing_rows || 0) : 0}
          accent={config?.listing_exists ? C.green : C.textMuted}
          sub={config?.listing_exists
            ? `${(summary?.totals?.options || 0).toLocaleString()} distinct options`
            : 'not generated yet'}/>
        <KpiTile icon={List} label="NEW Items" value={summary?.totals?.new}
          accent={C.amber}
          sub={summary?.totals?.total ? `${newPct}% of total · ${(summary?.totals?.new_options||0).toLocaleString()} options` : '—'}/>
        <KpiTile icon={BarChart3} label="Total Alloc Qty" value={totalAllocQty}
          accent={C.primary}
          sub={summary?.alloc_rows ? `${(summary?.alloc_rows||0).toLocaleString()} rows · ${allocPct}% of listing` : 'no allocation yet'}/>
        <KpiTile icon={BarChart3} label="Total Hold Qty" value={totalHoldQty}
          accent="#f59e0b"
          sub={totalHoldQty > 0 ? 'reserved for NL/TBL' : 'no hold'}/>
      </div>

      {/* ═══════════ Parked Runs — review queue ═══════════ */}
      {(() => {
        const count = parkedRuns?.length || 0
        if (count === 0 && !parkedExpanded) return null
        return (
          <div style={{ background: C.card, border: `1px solid ${C.cardBorder}`,
            borderRadius: 10, boxShadow: '0 1px 3px rgba(0,0,0,0.04)' }}>
            <button
              onClick={() => setParkedExpanded(e => !e)}
              style={{ width: '100%', background: 'transparent', border: 'none',
                padding: '8px 12px', display: 'flex', justifyContent: 'space-between',
                alignItems: 'center', cursor: 'pointer', borderBottom: parkedExpanded
                  ? `1px solid ${C.cardBorder}` : 'none' }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: C.text,
                display: 'flex', alignItems: 'center', gap: 6 }}>
                <Clock size={12} color={C.amber}/>
                Parked Runs <span style={pillStyle(C.amber)}>{count}</span>
                <span style={{ fontWeight: 400, color: C.textMuted, fontSize: 10 }}>
                  awaiting review — Approve to move 5 tables to history, Reject to discard
                </span>
              </span>
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <button
                  onClick={(e) => { e.stopPropagation(); loadParkedRuns() }}
                  title="Refresh parked runs"
                  style={{ height: 22, padding: '0 6px', borderRadius: 4, fontSize: 9,
                    background: '#fff', border: '1px solid #e2e8f0', cursor: 'pointer',
                    display: 'flex', alignItems: 'center', gap: 3, color: C.textSub }}>
                  {parkedLoading ? <Loader2 size={9} className="animate-spin"/> : <RefreshCw size={9}/>}
                </button>
                {parkedExpanded ? <ChevronUp size={14}/> : <ChevronDown size={14}/>}
              </span>
            </button>
            {parkedExpanded && count > 0 && (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
                  <thead>
                    <tr style={{ background: C.headerBg }}>
                      {['Session', 'Started', 'User',
                        'Parked alloc', 'Parked listing',
                        'Ship Qty', 'Hold Qty', 'Run', 'Actions'].map(h => (
                        <th key={h} style={{ padding: '5px 8px', textAlign: 'left',
                          borderBottom: '1px solid #e2e8f0', fontWeight: 700, fontSize: 9,
                          color: C.textSub, whiteSpace: 'nowrap' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {parkedRuns.map(r => (
                      <tr key={r.session_id} style={{ borderBottom: '1px solid #f1f5f9' }}>
                        <td style={{ padding: '5px 8px', fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}>
                          {r.session_id}
                        </td>
                        <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', color: C.textSub }}>
                          {r.started_at ? r.started_at.replace('T', ' ').substring(0, 19) : '—'}
                        </td>
                        <td style={{ padding: '5px 8px', color: C.textSub }}>{r.user_name || '—'}</td>
                        <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: 700 }}>
                          {(r.alloc_parked_rows ?? r.parked_rows ?? 0).toLocaleString()}
                        </td>
                        <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: 700 }}>
                          {(r.listing_parked_rows ?? 0).toLocaleString()}
                        </td>
                        <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub }}>
                          {(r.ship_qty_total || 0).toLocaleString()}
                        </td>
                        <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub }}>
                          {(r.hold_qty_total || 0).toLocaleString()}
                        </td>
                        <td style={{ padding: '5px 8px' }}>
                          <span style={statusPillStyle(r.run_status)}>{r.run_status || '—'}</span>
                        </td>
                        <td style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>
                          <button
                            onClick={() => openParkedDetail(r.session_id)}
                            style={{ height: 22, padding: '0 8px', marginRight: 4,
                              borderRadius: 4, fontSize: 9, fontWeight: 600,
                              background: '#fff', color: C.textSub, border: '1px solid #e2e8f0',
                              cursor: 'pointer' }}>
                            Review
                          </button>
                          <button
                            disabled={parkedActionBusy}
                            onClick={() => handleApproveParked(r.session_id)}
                            style={{ height: 22, padding: '0 8px', marginRight: 4,
                              borderRadius: 4, fontSize: 9, fontWeight: 700,
                              background: C.green, color: '#fff', border: 'none',
                              cursor: parkedActionBusy ? 'not-allowed' : 'pointer',
                              opacity: parkedActionBusy ? 0.5 : 1 }}>
                            Approve
                          </button>
                          <button
                            disabled={parkedActionBusy}
                            onClick={() => handleRejectParked(r.session_id)}
                            style={{ height: 22, padding: '0 8px',
                              borderRadius: 4, fontSize: 9, fontWeight: 700,
                              background: '#fef2f2', color: C.red, border: '1px solid #fecaca',
                              cursor: parkedActionBusy ? 'not-allowed' : 'pointer',
                              opacity: parkedActionBusy ? 0.5 : 1 }}>
                            Reject
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )
      })()}

      {/* ═══════════ Parked-run detail drawer ═══════════ */}
      {parkedDetailSid && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
          zIndex: 50, display: 'flex', alignItems: 'center', justifyContent: 'center',
          padding: 20 }}
          onClick={closeParkedDetail}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 10, width: '95vw', maxWidth: 1400,
              maxHeight: '90vh', display: 'flex', flexDirection: 'column',
              boxShadow: '0 20px 50px rgba(0,0,0,0.25)' }}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`,
              display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
                  Parked snapshot — review
                </div>
                <div style={{ fontSize: 10, color: C.textSub,
                  fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}>
                  session {parkedDetailSid}
                  {parkedDetail?.total != null &&
                    ` · ${parkedDetail.total.toLocaleString()} rows total · showing ${parkedDetail.rows?.length || 0}`}
                </div>
                <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                  {[
                    ['alloc',   'Alloc rows',  'ARS_ALLOC_PARKED'],
                    ['listing', 'Listing rows', 'ARS_LISTING_WORKING_PARKED'],
                  ].map(([v, label, hint]) => (
                    <button key={v}
                      onClick={() => switchParkedDetailTab(v)}
                      title={hint}
                      style={{ height: 22, padding: '0 10px', borderRadius: 4,
                        fontSize: 10, fontWeight: 700, cursor: 'pointer',
                        background: parkedDetailWhich === v ? C.primary : '#fff',
                        color:      parkedDetailWhich === v ? '#fff' : C.textSub,
                        border:     `1px solid ${parkedDetailWhich === v ? C.primary : '#e2e8f0'}`,
                      }}>
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <button
                  disabled={parkedActionBusy}
                  onClick={() => handleApproveParked(parkedDetailSid)}
                  style={{ height: 28, padding: '0 14px', borderRadius: 6, fontSize: 11,
                    fontWeight: 700, background: C.green, color: '#fff', border: 'none',
                    cursor: parkedActionBusy ? 'not-allowed' : 'pointer',
                    opacity: parkedActionBusy ? 0.5 : 1 }}>
                  Approve → History (5 tables)
                </button>
                <button
                  disabled={parkedActionBusy}
                  onClick={() => handleRejectParked(parkedDetailSid)}
                  style={{ height: 28, padding: '0 14px', borderRadius: 6, fontSize: 11,
                    fontWeight: 700, background: '#fef2f2', color: C.red,
                    border: '1px solid #fecaca',
                    cursor: parkedActionBusy ? 'not-allowed' : 'pointer',
                    opacity: parkedActionBusy ? 0.5 : 1 }}>
                  Reject
                </button>
                <button onClick={closeParkedDetail}
                  style={{ height: 28, width: 28, borderRadius: 6, background: '#f1f5f9',
                    border: '1px solid #e2e8f0', cursor: 'pointer',
                    display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <X size={14}/>
                </button>
              </div>
            </div>
            <div style={{ flex: 1, overflow: 'auto', padding: '0 0 8px' }}>
              {parkedDetailLoading && (
                <div style={{ padding: 24, textAlign: 'center', color: C.textSub }}>
                  <Loader2 size={16} className="animate-spin"/> loading rows…
                </div>
              )}
              {!parkedDetailLoading && parkedDetail?.rows?.length > 0 && (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 9 }}>
                  <thead>
                    <tr style={{ background: C.headerBg, position: 'sticky', top: 0, zIndex: 1 }}>
                      {parkedDetail.columns.map(c => (
                        <th key={c} style={{ padding: '5px 6px', textAlign: 'left',
                          borderBottom: '1px solid #e2e8f0', fontWeight: 700, fontSize: 8,
                          color: C.textSub, whiteSpace: 'nowrap', background: C.headerBg }}>
                          {c}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {parkedDetail.rows.map((row, i) => (
                      <tr key={i} style={{ borderBottom: '1px solid #f1f5f9' }}>
                        {parkedDetail.columns.map(c => (
                          <td key={c} style={{ padding: '4px 6px', whiteSpace: 'nowrap',
                            color: C.text, fontFamily: typeof row[c] === 'number'
                              ? 'ui-monospace, Menlo, Consolas, monospace' : 'inherit',
                            textAlign: typeof row[c] === 'number' ? 'right' : 'left' }}>
                            {row[c] === null || row[c] === undefined ? ''
                              : typeof row[c] === 'number' ? row[c].toLocaleString()
                              : String(row[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {!parkedDetailLoading && !parkedDetail?.rows?.length && (
                <div style={{ padding: 24, textAlign: 'center', color: C.textSub }}>
                  no rows
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ═══════════ Live Run Dashboard ═══════════ */}
      {(generating || (allocBatchId && allocProgress) || activeJob) && (() => {
        const MODE_INFO = {
          pandas:     { label: 'Pandas (in-memory)', color: '#06b6d4', icon: Database },
          sequential: { label: 'Sequential',         color: '#64748b', icon: Loader2 },
        }
        // Server-detected stage (from /listing/active-job): listing → alloc → complete.
        // We expand the 3 server states into a 5-step visual breakup so the
        // user sees the natural sub-phases of each stage. Mapping:
        //   server 'listing'  -> first 3 pills (rules → tier → explode) all "in progress"
        //   server 'alloc'    -> 4th pill (waterfall) lit
        //   server 'complete' -> 5th pill (reflect) lit, all prior green
        const STAGES = [
          { key: 'rules',     group: 'listing',  label: 'A1 · Rules' },
          { key: 'tier',      group: 'listing',  label: 'A2 · Tier · Rank' },
          { key: 'explode',   group: 'listing',  label: 'B · Build alloc rows' },
          { key: 'waterfall', group: 'alloc',    label: 'C · Waterfall (per MAJ_CAT)' },
          { key: 'reflect',   group: 'complete', label: 'D · Reflect · Finalise' },
        ]
        const serverStage = activeJob?.stage
                         || (allocProgress
                              ? (allocProgress.pending > 0 || allocProgress.in_progress > 0
                                 ? 'alloc' : 'complete')
                              : (generating ? 'listing' : null))
        const m = MODE_INFO[allocationMode] || MODE_INFO.pandas
        const ModeIcon = m.icon
        const isRunning = generating
                       || (allocProgress && (allocProgress.pending > 0 || allocProgress.in_progress > 0))
                       || !!activeJob
        const isComplete = allocProgress
                        && allocProgress.pending === 0 && allocProgress.in_progress === 0
                        && !activeJob
        const pct = allocProgress?.pct ?? 0
        const ageSec = Math.max(0, Math.floor((now - lastUpdate) / 1000))
        // Live elapsed: prefer server-reported elapsed (works even after refresh)
        const elapsedSec = activeJob?.elapsed_sec != null
          ? Math.floor(activeJob.elapsed_sec + (now - lastUpdate) / 1000)
          : null
        const fmtElapsed = (s) => {
          if (s == null) return null
          const m_ = Math.floor(s / 60), sec_ = s % 60
          return m_ > 0 ? `${m_}m ${sec_}s` : `${sec_}s`
        }
        const fmtTime = (iso) => {
          if (!iso) return null
          try { return new Date(iso).toLocaleString() } catch { return null }
        }
        const isAdoptedFromServer = !!activeJob && !generating
        // Completion timestamp from server (when batch finished)
        const completedAt = activeJob?.completed_at || null
        // Store-level progress (from /listing/active-job)
        const storeProg = activeJob?.store_progress || null
        return (
        <div style={{
          background: '#fff',
          border: `1px solid ${isRunning ? `${m.color}55` : C.cardBorder}`,
          borderRadius: 10, padding: '10px 14px',
          boxShadow: isRunning
            ? `0 0 0 1px ${m.color}22, 0 2px 6px ${m.color}11`
            : '0 1px 3px rgba(0,0,0,0.04)',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: C.text,
              display: 'flex', alignItems: 'center', gap: 6 }}>
              <Activity size={14} color={isRunning ? m.color : C.textMuted}/>
              {isAdoptedFromServer ? 'Backend Job (live)' : 'Live Run'}
            </div>
            {/* Mode badge */}
            <div style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              padding: '3px 9px', borderRadius: 14, fontSize: 10, fontWeight: 700,
              color: '#fff', background: `linear-gradient(135deg, ${m.color}, ${m.color}cc)`,
              boxShadow: `0 1px 3px ${m.color}55`,
            }}>
              <ModeIcon size={11} className={isRunning ? 'animate-spin' : ''}/>
              {m.label}
              {allocationMode === 'pandas' && (
                <span style={{ opacity: 0.85 }}>· {parallelWorkers}w</span>
              )}
            </div>
            {/* Status pill */}
            <span style={{
              fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 10,
              color: isComplete ? '#059669' : isRunning ? '#d97706' : C.textMuted,
              background: isComplete ? '#d1fae5' : isRunning ? '#fef3c7' : '#f1f5f9',
              border: `1px solid ${isComplete ? '#a7f3d0' : isRunning ? '#fde68a' : '#e2e8f0'}`,
              display: 'inline-flex', alignItems: 'center', gap: 4,
            }}>
              {isRunning && <Loader2 size={10} className="animate-spin"/>}
              {isComplete ? 'COMPLETE'
                : isAdoptedFromServer ? 'RUNNING (backend)'
                : isRunning ? (allocBatchId ? 'RUNNING' : 'STARTING…')
                : 'IDLE'}
            </span>
            {/* While running: live update ticker. After complete: completion time only. */}
            {!isComplete && isRunning && (
              <span style={{
                fontSize: 10, color: C.textMuted,
                display: 'inline-flex', alignItems: 'center', gap: 3,
              }} title={`Last poll: ${new Date(lastUpdate).toLocaleTimeString()}`}>
                <Clock size={10}/>
                updated {ageSec}s ago
              </span>
            )}
            <div style={{ flex: 1 }}/>
            {isComplete && completedAt ? (
              <span style={{ fontSize: 10, color: C.textMuted, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <Clock size={10}/>
                completed <strong style={{ color: C.text }}>{fmtTime(completedAt)}</strong>
              </span>
            ) : elapsedSec != null && (
              <span style={{ fontSize: 10, color: C.textMuted }}>
                elapsed <strong style={{ color: C.text }}>{fmtElapsed(elapsedSec)}</strong>
              </span>
            )}
            {allocBatchId && (
              <span style={{ fontSize: 10, color: C.textMuted }}>
                session <code style={{ background: '#f1f5f9', padding: '1px 4px', borderRadius: 3 }}>{allocBatchId}</code>
              </span>
            )}
            {/* Cancel button — only useful while a batch is genuinely active */}
            {isRunning && allocBatchId && !generating && (
              <button onClick={handleCancelBatch} disabled={cancellingBatch}
                title="Mark all PENDING/IN_PROGRESS rows as FAILED"
                style={{
                  height: 24, padding: '0 10px', borderRadius: 5, fontSize: 10, fontWeight: 700,
                  color: '#fff', cursor: cancellingBatch ? 'not-allowed' : 'pointer',
                  background: cancellingBatch ? '#94a3b8' : 'linear-gradient(135deg, #dc2626, #b91c1c)',
                  border: 'none', display: 'inline-flex', alignItems: 'center', gap: 4,
                }}>
                <Square size={10}/> {cancellingBatch ? 'Cancelling…' : 'Cancel batch'}
              </button>
            )}
          </div>

          {/* Stage strip — 5 pills mapped to 3 server-side groups */}
          {serverStage && (() => {
            const groupOrder = { listing: 0, alloc: 1, complete: 2 }
            const sgIdx = groupOrder[serverStage] ?? 0
            return (
              <div style={{ display: 'flex', gap: 4, marginTop: 8, alignItems: 'center' }}>
                {STAGES.map((st) => {
                  const stIdx = groupOrder[st.group] ?? 0
                  const reached = stIdx < sgIdx
                  const current = stIdx === sgIdx && serverStage !== 'complete'
                  const allDone = serverStage === 'complete'
                  const isDone = allDone || reached
                  return (
                    <div key={st.key} style={{
                      flex: 1, height: 24, borderRadius: 4,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 9, fontWeight: 700, letterSpacing: '.03em',
                      color: current || isDone ? '#fff' : C.textMuted,
                      background: current
                        ? `linear-gradient(135deg, ${m.color}, ${m.color}bb)`
                        : isDone ? '#10b981' : '#f1f5f9',
                      border: `1px solid ${current ? m.color : isDone ? '#10b981' : '#e2e8f0'}`,
                    }}>
                      {current && <Loader2 size={9} className="animate-spin" style={{ marginRight: 4 }}/>}
                      {st.label}
                    </div>
                  )
                })}
              </div>
            )
          })()}
          {/* Single combined progress bar.
              Threshold = MAJ_CAT × STORE (e.g. 10 × 300 = 3000 work-units).
              Each completed MAJ_CAT covers all stores, so combined % is the
              same as MAJ_CAT %; only the percentage is shown — no values. */}
          {(() => {
            const mcDone  = allocProgress?.done  ?? 0
            const mcTotal = allocProgress?.total ?? 0
            const stTotal = storeProg?.total ?? 0
            const totalUnits = mcTotal * stTotal
            const doneUnits  = mcDone  * stTotal
            const combinedPct = totalUnits > 0
              ? Math.round(1000 * doneUnits / totalUnits) / 10
              : 0
            const failed = (allocProgress?.failed ?? 0) > 0
            return (
              <div style={{ marginTop: 8 }}>
                <div style={{ height: 16, background: '#f1f5f9', borderRadius: 6,
                  overflow: 'hidden', position: 'relative' }}>
                  <div style={{
                    height: '100%',
                    width: totalUnits > 0
                      ? `${Math.max(0, Math.min(100, combinedPct))}%`
                      : (generating ? '100%' : '0%'),
                    background: totalUnits > 0
                      ? (failed
                          ? 'linear-gradient(90deg, #f59e0b, #d97706)'
                          : `linear-gradient(90deg, ${m.color}, ${m.color}aa)`)
                      : `linear-gradient(90deg, ${m.color}, ${m.color}aa)`,
                    transition: 'width 0.4s ease',
                  }}/>
                  <div style={{ position: 'absolute', inset: 0,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: 13, fontWeight: 900, color: '#dc2626',
                    letterSpacing: '.04em',
                    textShadow: '0 0 4px rgba(255,255,255,0.9), 0 0 2px rgba(255,255,255,0.9)' }}>
                    {totalUnits > 0
                      ? `${combinedPct}%`
                      : (generating ? 'preparing…'
                        : (isComplete ? 'no MAJ_CATs queued' : ''))}
                  </div>
                </div>
              </div>
            )
          })()}
          {/* Counts row — only show when the queue was actually seeded
              (i.e. an allocation run, not listing-only). For listing-only
              runs we show a one-line explainer instead of all-zeros counts. */}
          {allocProgress && allocProgress.total > 0 ? (
            <div style={{ display: 'flex', gap: 14, marginTop: 6, fontSize: 11, flexWrap: 'wrap' }}>
              <span style={{ color: C.textMuted }}>
                Pending <strong style={{ color: C.text }}>{allocProgress.pending}</strong>
              </span>
              <span style={{ color: C.textMuted }}>
                In progress <strong style={{ color: C.text }}>{allocProgress.in_progress}</strong>
              </span>
              <span style={{ color: '#10b981' }}>
                Done <strong>{allocProgress.done}</strong>
              </span>
              <span style={{ color: allocProgress.failed > 0 ? '#dc2626' : C.textMuted }}>
                Failed <strong>{allocProgress.failed}</strong>
              </span>
              {allocProgress.elapsed_sec != null && (
                <span style={{ color: C.textMuted, marginLeft: 'auto' }}>
                  Elapsed <strong style={{ color: C.text }}>{Math.round(allocProgress.elapsed_sec)}s</strong>
                </span>
              )}
            </div>
          ) : isComplete && allocProgress && allocProgress.total === 0 ? (
            <div style={{ marginTop: 6, fontSize: 11, color: C.textMuted,
              display: 'flex', alignItems: 'center', gap: 6 }}>
              <List size={11} color={m.color}/>
              No MAJ_CATs were queued — Stage B produced 0 alloc rows (check filters or sequential mode)
            </div>
          ) : generating && (
            <div style={{ marginTop: 6, fontSize: 11, color: C.textMuted, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Loader2 size={11} className="animate-spin" color={m.color}/>
              Building listing &amp; preparing allocation queue…
            </div>
          )}
          {/* Failed list + retry button */}
          {allocFailed && allocFailed.length > 0 && (
            <div style={{ marginTop: 10, padding: '8px 10px',
              background: '#fef2f2', border: '1px solid #fecaca',
              borderRadius: 6 }}>
              <div style={{ display: 'flex', alignItems: 'center',
                justifyContent: 'space-between', marginBottom: 6 }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: '#dc2626' }}>
                  Failed MAJ_CATs ({allocFailed.length})
                </div>
                <button onClick={handleRetryFailed} disabled={retryingFailed || generating}
                  style={{ height: 26, borderRadius: 6, fontSize: 11, fontWeight: 700,
                    color: '#fff', padding: '0 12px',
                    background: retryingFailed
                      ? '#94a3b8'
                      : 'linear-gradient(135deg, #f59e0b, #d97706)',
                    border: 'none', cursor: retryingFailed ? 'not-allowed' : 'pointer',
                    display: 'flex', alignItems: 'center', gap: 4 }}>
                  {retryingFailed ? 'Retrying...' : 'Retry Failed'}
                </button>
              </div>
              <div style={{ maxHeight: 120, overflowY: 'auto' }}>
                <table style={{ width: '100%', fontSize: 10, borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ color: C.textMuted, textAlign: 'left' }}>
                      <th style={{ padding: '2px 6px' }}>MAJ_CAT</th>
                      <th style={{ padding: '2px 6px' }}>Attempts</th>
                      <th style={{ padding: '2px 6px' }}>Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allocFailed.map((f) => (
                      <tr key={f.maj_cat} style={{ borderTop: '1px solid #fee2e2' }}>
                        <td style={{ padding: '2px 6px', fontWeight: 600 }}>{f.maj_cat}</td>
                        <td style={{ padding: '2px 6px' }}>{f.attempts}</td>
                        <td style={{ padding: '2px 6px', color: '#7f1d1d',
                          maxWidth: 600, overflow: 'hidden', textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap' }}>{f.error}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
        )
      })()}

      {/* ═══════════ Filters + Run Mode ═══════════ */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 280px', gap: 8 }}>
        <SearchSelect label="Select Store"   items={config?.stores || []}
          selected={selectedStores}  setSelected={setSelectedStores}  placeholder="Search store..."/>
        <SearchSelect label="Select MAJ_CAT" items={config?.maj_cats || []}
          selected={selectedMajCats} setSelected={setSelectedMajCats} placeholder="Search MAJ_CAT..."/>
        <div style={_card}>
          <div style={{ ..._lbl, display: 'flex', alignItems: 'center', gap: 4 }}>
            <Play size={9} color={C.primary}/> RUN MODE
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 4, marginTop: 4 }}>
            {[['listing','Listing only', C.primary],
              ['full','Full Pipeline', '#7c3aed']].map(([v, l, clr]) => (
              <button key={v} onClick={() => setRunMode(v)}
                style={{ ..._btn(runMode===v, clr), height: 28, fontSize: 10 }}>{l}</button>
            ))}
          </div>
          <div style={{ fontSize: 9, color: C.textMuted, marginTop: 5 }}>
            {runMode === 'full'
              ? 'MSA Stock Calc → Grid Build → Listing → Allocation (one click)'
              : 'Listing → Allocation (skip MSA & Grid)'}
          </div>
        </div>
      </div>


      {/* ═══════════ RDC Scope + MIX Aggregation ═══════════ */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
        <div style={_card}>
          <div style={_lbl}>RDC SCOPE</div>
          <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
            {[['all','All RDCs','See every RDC'],
              ['own','Own','Auto-detected from selected stores'],
              ['cross','Cross','Pull stock from other RDCs']].map(([v, l, hint]) => (
              <button key={v} onClick={() => { setRdcMode(v); setCrossFrom([]) }}
                title={hint}
                style={{ ..._btn(rdcMode===v), height: 28, padding: '0 14px', fontSize: 10 }}>{l}</button>
            ))}
          </div>
          {rdcMode === 'own' && autoRdcs.length > 0 && (
            <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 4, fontSize: 9, flexWrap: 'wrap' }}>
              <span style={{ color: C.textMuted }}>Detected:</span>
              {autoRdcs.map(r => <span key={r} style={pillStyle(C.primary)}>{r}</span>)}
            </div>
          )}
          {rdcMode === 'cross' && otherRdcs.length > 0 && (
            <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 4, fontSize: 9, flexWrap: 'wrap' }}>
              <span style={{ color: C.textMuted }}>Pull from:</span>
              {otherRdcs.map(r => {
                const on = crossFrom.includes(r)
                return <button key={r}
                  onClick={() => setCrossFrom(p => on ? p.filter(x=>x!==r) : [...p, r])}
                  style={{ ..._btn(on, C.amber), height: 22, fontSize: 9, padding: '0 8px' }}>{r}</button>
              })}
            </div>
          )}
        </div>

        <div style={_card}>
          <div style={_lbl}>MIX-LINE AGGREGATION</div>
          <div style={{ display: 'flex', gap: 4, marginTop: 4 }}>
            {[['st_maj_rng','MAJ + RNG','1 row per store × MAJ_CAT × RNG_SEG'],
              ['st_maj','MAJ only','1 row per store × MAJ_CAT'],
              ['each','Each','Keep every MIX line']].map(([v, l, hint]) => (
              <button key={v} onClick={() => setMixMode(v)} title={hint}
                style={{ ..._btn(mixMode===v, '#0891b2'), height: 28, padding: '0 14px', fontSize: 10 }}>{l}</button>
            ))}
          </div>
          <div style={{ fontSize: 9, color: C.textMuted, marginTop: 5 }}>
            {mixMode === 'st_maj_rng' && 'Default — 1 line per store × MAJ_CAT × range segment'}
            {mixMode === 'st_maj'     && '1 line per store × MAJ_CAT (collapses range segments)'}
            {mixMode === 'each'       && 'No aggregation — every MIX line preserved'}
          </div>
        </div>
      </div>

      {/* ═══════════ Tunable Parameters (grouped) ═══════════ */}
      <div style={{ ..._card, padding: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
          <div style={{ ..._lbl, marginBottom: 0 }}>TUNABLE PARAMETERS</div>
          <div style={{ flex: 1, height: 1, background: '#f1f5f9' }}/>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 16 }}>
          <ParamGroup title="Stock & Excess" color="#0891b2">
            <ParamInput label="Stock %"    value={stockThresholdPct} setter={setStockThresholdPct} step={0.05}
              hint={`${Math.round(stockThresholdPct*100)}%`}    tip="Threshold to classify as RL/NL"/>
            <ParamInput label="Excess ×"   value={excessMultiplier}  setter={setExcessMultiplier}  step={0.5}
              hint={`${excessMultiplier}× OPT_MBQ`}             tip="Excess if STK > X × OPT_MBQ"/>
            <ParamInput label="Hold Days"  value={holdDays}          setter={setHoldDays}          step={1}
              hint={`${holdDays}d`}                             tip="OPT_MBQ_WH hold lookback window"/>
            <ParamInput label="AGE <"      value={ageThreshold}      setter={setAgeThreshold}      step={1}
              hint={`${ageThreshold}d`}                         tip="Use PER_OPT_SALE if AGE < X days"/>
          </ParamGroup>

          <ParamGroup title="Season (SSN)" color="#0891b2">
            <CheckboxGroup label="" items={ssnOptions}
              selected={selectedSsn} setSelected={setSelectedSsn} color="#0891b2" vertical/>
          </ParamGroup>

          <ParamGroup title="Allocation Gates" color={C.green}>
            <ToggleRow checked={priCheckRL}  setChecked={setPriCheckRL}
              label="PRI ≥ 100% (RL)"  color="#0891b2"
              hint={priCheckRL
                ? `ON — RL listed only if PRI_CT% ≥ 100; cap = Grid Growth (${mbqGrowthUseDefault ? 100 : mjReqGrowthPct}%)`
                : `OFF — RL cap = user-set Dispatch Cap (${rlMbqCapPct}%)`}/>
            {!priCheckRL && (
              <ParamInput label="RL Dispatch Cap %" value={rlMbqCapPct} setter={setRlMbqCapPct} step={5} min={50} max={200}
                hint={`Ship up to ${rlMbqCapPct}% × MJ_MBQ_ORIG`}
                tip="When PRI ≥ 100% (RL) is OFF, this caps the total RL SHIP_QTY at cap% × MJ_MBQ_ORIG per (WERKS, MAJ_CAT). When the toggle is ON, the RL cap follows the Grid MBQ Growth % (default) instead."/>
            )}
            <ToggleRow checked={priCheckTBC} setChecked={setPriCheckTBC}
              label="PRI ≥ 100% (TBC)" color="#0891b2"
              hint={priCheckTBC
                ? `ON — TBC listed only if PRI_CT% ≥ 100; cap = Grid Growth (${mbqGrowthUseDefault ? 100 : mjReqGrowthPct}%)`
                : `OFF — TBC cap = user-set Dispatch Cap (${tbcMbqCapPct}%)`}/>
            {!priCheckTBC && (
              <ParamInput label="TBC Dispatch Cap %" value={tbcMbqCapPct} setter={setTbcMbqCapPct} step={5} min={50} max={200}
                hint={`Ship up to ${tbcMbqCapPct}% × MJ_MBQ_ORIG`}
                tip="When PRI ≥ 100% (TBC) is OFF, this caps the total TBC SHIP_QTY at cap% × MJ_MBQ_ORIG per (WERKS, MAJ_CAT). When the toggle is ON, the TBC cap follows the Grid MBQ Growth % (default) instead."/>
            )}
            <ToggleRow checked={mbqGrowthUseDefault} setChecked={setMbqGrowthUseDefault}
              label="Use Default 100% (MBQ)" color={C.green}
              hint={mbqGrowthUseDefault
                ? "strict cap — MBQ growth disabled"
                : `All non-pivot grid MBQs → *_MBQ_REV at ${mjReqGrowthPct}% (per MAJ_CAT)`}/>
            {!mbqGrowthUseDefault && (
              <ParamInput label="Grid MBQ Growth %" value={mjReqGrowthPct} setter={setMjReqGrowthPct} step={5} min={100} max={200}
                hint={`+${mjReqGrowthPct - 100}% headroom (all grids); RL/TBC/TBL caps inherit this %`}
                tip="Scale every non-pivot grid's MBQ to *_MBQ_REV per MAJ_CAT (MJ + FAB + MICRO_MVGR + M_VND_CD + RNG_SEG …). Multiplier reads *_MBQ_ORIG so re-runs never compound. *_REQ_REV is re-derived as MAX(0, MBQ_REV − STK_TTL); engine consumes the lifted columns via *_MBQ / *_REQ. Sec-cap automatically widens to max(130%, growth%). Per-OPT_TYPE dispatch caps follow this %; override per OPT_TYPE only via the PRI ≥ 100% toggles above."/>
            )}
            <ToggleRow checked={enableMinSize} setChecked={setEnableMinSize}
              label="Min sizes for TBL" color="#7c3aed"
              hint="Reject TBL options that have fewer than X distinct sizes"/>
            {enableMinSize && (
              <ParamInput label="Min size #" value={minSizeCount} setter={setMinSizeCount} step={1} min={1}
                hint={`≥ ${minSizeCount} sizes`}/>
            )}
          </ParamGroup>

          <ParamGroup title="Store Ranking" color={C.blue}>
            <ParamInput label="Req %"  value={reqWeight}   setter={setReqWeight}   step={0.1}
              hint={`${Math.round(reqWeight*100)}%`}  tip="Weight for OPT_REQ"/>
            <ParamInput label="Fill %" value={fillWeight}  setter={setFillWeight}  step={0.1}
              hint={`${Math.round(fillWeight*100)}%`} tip="Weight for fill rate"/>
            <ParamInput label="ACS_D"  value={defaultAcsD} setter={setDefaultAcsD} step={1}
              hint={`def=${defaultAcsD}`}             tip="Default AGE-of-Comparable-Stock fallback"/>
          </ParamGroup>

          {/* Fallback Allocation panel removed 2026-05-16. See
              backend/app/docs/processes/fallback_archived.md for the
              previous F0–F5 design. Sec-grid Cap toggle was preserved
              and moved here as a standalone control. */}
          <ParamGroup title="Secondary-grid Cap" color={C.amber}>
            <ToggleRow checked={applySecCapInNormal} setChecked={setApplySecCapInNormal}
              label="Sec-grid Cap 130%" color={C.primary}
              hint="Cap Secondary grids at 130% of MBQ in main pass"/>
          </ParamGroup>
        </div>
      </div>

      {/* ═══════════ Insight tiles + Charts ═══════════ */}
      {summary && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 8 }}>
            <InsightTile label="Stores covered"   value={summary.totals?.stores}            accent={C.blue}/>
            <InsightTile label="RDCs"             value={summary.totals?.rdcs}              accent={C.primary}/>
            <InsightTile label="Distinct Options" value={summary.totals?.options}           accent="#0891b2"/>
            <InsightTile label="New Options"      value={summary.totals?.new_options}       accent={C.amber}/>
            <InsightTile label="Avg / Store"      value={avgPerStore}                       accent={C.text}/>
            <InsightTile label="Working rows"     value={summary.working_rows}              accent={C.text}/>
            <InsightTile label="Allocated rows"   value={summary.alloc_rows}                accent={C.green}/>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
            <ChartCard title="OPT_TYPE Distribution" subtitle="Listing rows split by classification">
              {(h) => optTypeChartData.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <PieChart>
                    <Pie data={optTypeChartData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={80}
                      label={({cx, cy, midAngle, innerRadius, outerRadius, value}) => {
                        const r = innerRadius + (outerRadius - innerRadius) * 0.5
                        const x = cx + r * Math.cos(-midAngle * Math.PI / 180)
                        const y = cy + r * Math.sin(-midAngle * Math.PI / 180)
                        const total = optTypeChartData.reduce((s, e) => s + e.value, 0)
                        const pct = total > 0 ? Math.round(value / total * 100) : 0
                        return <text x={x} y={y} textAnchor="middle" dominantBaseline="central" fontSize={10} fontWeight={700} fill="#fff">{value.toLocaleString()} ({pct}%)</text>
                      }} labelLine={false}>
                      {optTypeChartData.map((entry, i) => (
                        <Cell key={i} fill={entry.color || PIE_COLORS_FALLBACK[i % PIE_COLORS_FALLBACK.length]} />
                      ))}
                    </Pie>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No data</div>
              )}
            </ChartCard>

            <ChartCard title="Allocation Quantity by Type" subtitle="Total units allocated per OPT_TYPE">
              {(h) => allocChartData.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={allocChartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis dataKey="name" fontSize={11} />
                    <YAxis fontSize={11} />
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Bar dataKey="qty" radius={[4, 4, 0, 0]} label={{ position: 'top', fontSize: 10, fontWeight: 700, fill: '#374151' }}>
                      {allocChartData.map((entry, i) => (
                        <Cell key={i} fill={entry.color || '#4f46e5'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No allocation data</div>
              )}
            </ChartCard>

            <ChartCard title="Allocation by RDC"
              subtitle="Distribution of allocated units across RDCs"
              right={
                <div style={{ fontSize: 10, color: C.textMuted }}>
                  Total: <b style={{ color: C.text }}>{totalAllocQty.toLocaleString()}</b>
                </div>
              }>
              {(h) => summary.by_rdc?.length > 0 && summary.by_rdc.some(r => r.alloc_qty > 0) ? (
                <ResponsiveContainer width="100%" height={h}>
                  <PieChart>
                    <Pie data={summary.by_rdc.filter(r => r.alloc_qty > 0)} dataKey="alloc_qty" nameKey="rdc" cx="50%" cy="50%" outerRadius={80}
                      label={({cx, cy, midAngle, innerRadius, outerRadius, value}) => {
                        const r = innerRadius + (outerRadius - innerRadius) * 0.5
                        const x = cx + r * Math.cos(-midAngle * Math.PI / 180)
                        const y = cy + r * Math.sin(-midAngle * Math.PI / 180)
                        const total = summary.by_rdc.filter(rr => rr.alloc_qty > 0).reduce((s, e) => s + e.alloc_qty, 0)
                        const pct = total > 0 ? Math.round(value / total * 100) : 0
                        return <text x={x} y={y} textAnchor="middle" dominantBaseline="central" fontSize={9} fontWeight={700} fill="#fff">{value.toLocaleString()} ({pct}%)</text>
                      }} labelLine={false}>
                      {summary.by_rdc.filter(r => r.alloc_qty > 0).map((_, i) => (
                        <Cell key={i} fill={['#4f46e5', '#059669', '#d97706', '#2563eb', '#7c3aed', '#06b6d4', '#dc2626', '#ec4899'][i % 8]} />
                      ))}
                    </Pie>
                    <Tooltip formatter={(v) => v.toLocaleString()} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No allocation data</div>
              )}
            </ChartCard>

            <ChartCard
              title={`${majcatRankDir === 'top' ? 'Top' : 'Bottom'} ${majcatRankN} MAJ_CATs`}
              subtitle="Allocated qty by major category"
              right={<RankSelector dir={majcatRankDir} setDir={setMajcatRankDir} n={majcatRankN} setN={setMajcatRankN}/>}>
              {(h) => rankedMajCats.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={rankedMajCats} layout="vertical" margin={{ top: 5, right: 30, left: 10, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis type="number" fontSize={10} />
                    <YAxis type="category" dataKey="maj_cat" fontSize={10} width={90} interval={0}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Bar dataKey="alloc_qty" fill={majcatRankDir === 'top' ? C.primary : '#9ca3af'} radius={[0, 4, 4, 0]}
                      label={{ position: 'right', fontSize: 9, fontWeight: 700, fill: C.text,
                        formatter: (v) => v.toLocaleString() }}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No data</div>
              )}
            </ChartCard>

            <ChartCard
              title={`${storeRankDir === 'top' ? 'Top' : 'Bottom'} ${storeRankN} Stores`}
              subtitle="Allocated qty by store (WERKS)"
              right={<RankSelector dir={storeRankDir} setDir={setStoreRankDir} n={storeRankN} setN={setStoreRankN}/>}>
              {(h) => rankedStores.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={rankedStores} layout="vertical" margin={{ top: 5, right: 30, left: 10, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis type="number" fontSize={10} />
                    <YAxis type="category" dataKey="werks" fontSize={10} width={70} interval={0}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Bar dataKey="alloc_qty" fill={storeRankDir === 'top' ? '#0891b2' : '#9ca3af'} radius={[0, 4, 4, 0]}
                      label={{ position: 'right', fontSize: 9, fontWeight: 700, fill: C.text,
                        formatter: (v) => v.toLocaleString() }}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No store-level data</div>
              )}
            </ChartCard>

            <ChartCard title="Store Status (STSTATUS)"
              subtitle="Active store master breakdown"
              right={<div style={{ fontSize: 10, color: C.textMuted }}>
                Total: <b style={{ color: C.text }}>
                  {storeStatusChartData.reduce((s, e) => s + (e.value || 0), 0).toLocaleString()}
                </b>
              </div>}>
              {(h) => storeStatusChartData.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <PieChart>
                    <Pie data={storeStatusChartData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={80}
                      label={({cx, cy, midAngle, innerRadius, outerRadius, value}) => {
                        const r = innerRadius + (outerRadius - innerRadius) * 0.5
                        const x = cx + r * Math.cos(-midAngle * Math.PI / 180)
                        const y = cy + r * Math.sin(-midAngle * Math.PI / 180)
                        const total = storeStatusChartData.reduce((s, e) => s + e.value, 0)
                        const pct = total > 0 ? Math.round(value / total * 100) : 0
                        return <text x={x} y={y} textAnchor="middle" dominantBaseline="central" fontSize={9} fontWeight={700} fill="#fff">{value.toLocaleString()} ({pct}%)</text>
                      }} labelLine={false}>
                      {storeStatusChartData.map((entry, i) => (
                        <Cell key={i} fill={entry.color}/>
                      ))}
                    </Pie>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No status data</div>
              )}
            </ChartCard>

            <ChartCard title="Hold Qty by RDC"
              subtitle="Hold (WH − base) qty reserved per RDC"
              right={
                <div style={{ fontSize: 10, color: C.textMuted }}>
                  Total: <b style={{ color: C.text }}>{totalHoldQty.toLocaleString()}</b>
                </div>
              }>
              {(h) => holdByRdc.some(r => r.hold_qty > 0) ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={holdByRdc.filter(r => r.hold_qty > 0)} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis dataKey="rdc" fontSize={10}/>
                    <YAxis fontSize={10}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Bar dataKey="hold_qty" fill="#f59e0b" radius={[4, 4, 0, 0]}
                      label={{ position: 'top', fontSize: 10, fontWeight: 700, fill: '#374151',
                        formatter: (v) => v.toLocaleString() }}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No hold qty</div>
              )}
            </ChartCard>

            {/* Allocation by HUB — joined via store master HUB column */}
            <ChartCard title="Allocation by HUB"
              subtitle="Allocated qty grouped by store hub"
              right={<div style={{ fontSize: 10, color: C.textMuted }}>
                Total: <b style={{ color: C.text }}>
                  {Math.round(hubChartData.reduce((s, e) => s + (e.alloc_qty || 0), 0)).toLocaleString()}
                </b>
              </div>}>
              {(h) => hubChartData.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={hubChartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis dataKey="hub" fontSize={10}/>
                    <YAxis fontSize={10}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Legend />
                    <Bar dataKey="alloc_qty" name="Alloc" fill="#7c3aed" radius={[4, 4, 0, 0]}
                      label={{ position: 'top', fontSize: 10, fontWeight: 700, fill: '#374151',
                        formatter: (v) => v.toLocaleString() }}/>
                    <Bar dataKey="hold_qty" name="Hold" fill="#f59e0b" radius={[4, 4, 0, 0]}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No hub data — add HUB column to store master</div>
              )}
            </ChartCard>

            {/* Alloc Qty by Season */}
            <ChartCard title="Alloc Qty by Season (SSN)"
              subtitle="Allocated qty grouped by season"
              right={
                <button onClick={() => setExpandedChart('ssn')}
                  style={{ background: 'transparent', border: '1px solid #e2e8f0', borderRadius: 4,
                           padding: '2px 8px', fontSize: 10, cursor: 'pointer', color: C.textSub }}>
                  Expand
                </button>
              }>
              {(h) => (summary?.by_ssn || []).length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={summary.by_ssn} layout="vertical" margin={{ top: 5, right: 50, left: 10, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis type="number" fontSize={10}/>
                    <YAxis type="category" dataKey="ssn" fontSize={10} width={80} interval={0}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Bar dataKey="alloc_qty" name="Alloc" fill={C.primary} radius={[0, 4, 4, 0]}
                      label={{ position: 'right', fontSize: 9, fontWeight: 700, fill: C.text,
                        formatter: (v) => v.toLocaleString() }}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No season data</div>
              )}
            </ChartCard>

            {/* Alloc & Hold by Division */}
            <ChartCard title="Alloc & Hold by Division (DIV)"
              subtitle="Allocated and hold qty grouped by division"
              right={
                <button onClick={() => setExpandedChart('div')}
                  style={{ background: 'transparent', border: '1px solid #e2e8f0', borderRadius: 4,
                           padding: '2px 8px', fontSize: 10, cursor: 'pointer', color: C.textSub }}>
                  Expand
                </button>
              }>
              {(h) => (summary?.by_div || []).length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={summary.by_div} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis dataKey="div" fontSize={10}/>
                    <YAxis fontSize={10}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Legend/>
                    <Bar dataKey="alloc_qty" name="Alloc" fill="#7c3aed" radius={[4, 4, 0, 0]}/>
                    <Bar dataKey="hold_qty" name="Hold" fill="#f59e0b" radius={[4, 4, 0, 0]}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>No division data</div>
              )}
            </ChartCard>

            {/* Contribution: stock vs alloc per RDC, filtered by selected MAJ_CAT(s) */}
            <ChartCard title="RDC Stock vs Alloc"
              subtitle={selectedMajCats.length > 0
                ? `${selectedMajCats.length} MAJ_CAT${selectedMajCats.length > 1 ? 's' : ''} selected`
                : 'all MAJ_CATs (select MAJ_CATs to filter)'}
              right={<div style={{ fontSize: 10, color: C.textMuted }}>
                Cont %: <b style={{ color: C.green }}>
                  {(() => {
                    const stk = contribData.reduce((s, r) => s + (r.stock || 0), 0)
                    const alc = contribData.reduce((s, r) => s + (r.alloc || 0), 0)
                    return stk > 0 ? `${Math.round(100 * alc / stk)}%` : '—'
                  })()}
                </b>
              </div>}>
              {(h) => contribData.length > 0 ? (
                <ResponsiveContainer width="100%" height={h}>
                  <BarChart data={contribData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                    <XAxis dataKey="rdc" fontSize={10}/>
                    <YAxis fontSize={10}/>
                    <Tooltip formatter={(v) => v.toLocaleString()}/>
                    <Legend />
                    <Bar dataKey="stock" name="Stock" fill="#0891b2" radius={[4, 4, 0, 0]}/>
                    <Bar dataKey="alloc" name="Alloc" fill="#dc2626" radius={[4, 4, 0, 0]}/>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', color: C.textMuted, fontSize: 11 }}>
                  {config?.listing_exists ? 'No data for selection' : 'Generate listing first'}
                </div>
              )}
            </ChartCard>
          </div>

          {/* ALLOC_STATUS pill row — only when there's allocation status data */}
          {summary.by_alloc_status && Object.keys(summary.by_alloc_status).length > 0 && (
            <div style={{ ..._card, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', padding: '8px 12px' }}>
              <div style={{ ..._lbl, marginBottom: 0 }}>ALLOC_STATUS</div>
              {Object.entries(summary.by_alloc_status).map(([s, n]) => (
                <span key={s} style={statusPillStyle(s)}>
                  {s}: <b>{(n||0).toLocaleString()}</b>
                </span>
              ))}
            </div>
          )}
        </>
      )}

      {/* ═══════════ Preview Table ═══════════ */}
      <div style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10, overflow: 'hidden', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' }}>
        <div style={{ padding: '8px 12px', background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}`,
          display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Eye size={13} color={C.textSub}/>
            {[['working', 'Working', C.green], ['listing', 'Full Listing', C.primary], ['alloc', 'Alloc', C.amber]].map(([v, l, clr]) => (
              <button key={v}
                onClick={() => { setPreviewTable(v); setColFilters({}); loadPreview(1, {}, undefined, v) }}
                style={{ height: 24, fontSize: 10, fontWeight: 700, borderRadius: 4, padding: '0 10px', cursor: 'pointer',
                  background: previewTable === v ? clr : '#fff',
                  color: previewTable === v ? '#fff' : C.textSub,
                  border: `1px solid ${previewTable === v ? clr : '#e2e8f0'}` }}>
                {l}
              </button>
            ))}
            {preview && <span style={{ fontSize: 10, color: C.textMuted, marginLeft: 4 }}>({preview.total.toLocaleString()} rows)</span>}
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            <div style={{ position: 'relative' }}>
              <Search size={11} style={{ position: 'absolute', left: 6, top: 6, color: C.textMuted, pointerEvents: 'none' }}/>
              <input value={globalSearch}
                onChange={e => setGlobalSearch(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') loadPreview(1, undefined, globalSearch) }}
                placeholder="Search all columns..."
                style={{ height: 24, width: 220, fontSize: 10, padding: '0 6px 0 22', borderRadius: 4,
                  border: `1px solid ${globalSearch ? C.primaryBd : '#e2e8f0'}`,
                  background: globalSearch ? '#eff6ff' : '#fff', outline: 'none' }}/>
            </div>
            <select value={previewPageSize}
              onChange={e => setPreviewPageSize(parseInt(e.target.value, 10))}
              style={{ height: 24, fontSize: 10, borderRadius: 3, border: '1px solid #e2e8f0', padding: '0 4px' }}>
              {[50, 100, 200, 500, 1000, 2000, 5000].map(n => (
                <option key={n} value={n}>{n} rows</option>
              ))}
            </select>
            {hasColFilters && (
              <button onClick={clearAllFilters}
                style={{ height: 24, padding: '0 8px', borderRadius: 3, fontSize: 9, fontWeight: 600,
                  background: '#fef2f2', color: C.red, border: '1px solid #fecaca', cursor: 'pointer',
                  display: 'flex', alignItems: 'center', gap: 3 }}>
                <X size={9}/> Clear Filters
              </button>
            )}
            <button onClick={() => loadPreview(1)} disabled={loading}
              style={{ height: 24, padding: '0 10px', borderRadius: 3, fontSize: 10, fontWeight: 700,
                background: C.primary, color: '#fff', border: 'none', cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 4 }}>
              {loading ? <Loader2 size={10} className="animate-spin"/> : <RefreshCw size={10}/>} Fetch
            </button>
            <button onClick={() => setPreviewExpanded(e => !e)}
              style={{ height: 24, padding: '0 10px', borderRadius: 3, fontSize: 10, fontWeight: 600,
                background: previewExpanded ? '#f0fdf4' : '#f8fafc', color: previewExpanded ? '#059669' : C.textSub,
                border: `1px solid ${previewExpanded ? '#bbf7d0' : '#e2e8f0'}`, cursor: 'pointer' }}>
              {previewExpanded ? 'Collapse' : 'Expand'}
            </button>
          </div>
        </div>

        {preview?.data?.length > 0 ? (
          <>
            <div style={{ overflowX: 'auto', maxHeight: previewExpanded ? 'calc(100vh - 350px)' : '400px' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 9 }}>
                <thead>
                  <tr style={{ background: C.headerBg }}>
                    {preview.columns.map(col => (
                      <th key={col} style={{ padding: '5px 6px', textAlign: col === 'IS_NEW' ? 'center' : 'left',
                        borderBottom: '1px solid #e2e8f0', fontWeight: 700, fontSize: 8,
                        color: C.textSub, whiteSpace: 'nowrap', position: 'sticky', top: 0, background: C.headerBg, zIndex: 2 }}>
                        {col}
                      </th>
                    ))}
                  </tr>
                  <tr style={{ background: '#f1f5f9' }}>
                    {preview.columns.map(col => (
                      <th key={`f-${col}`} style={{ padding: '2px 2px', borderBottom: '1px solid #e2e8f0',
                        position: 'sticky', top: 23, background: '#f1f5f9', zIndex: 2 }}>
                        <div style={{ position: 'relative' }}>
                          <Filter size={7} style={{ position: 'absolute', left: 2, top: 5, color: colFilters[col] ? C.primary : '#cbd5e1', pointerEvents: 'none' }}/>
                          <input
                            value={colFilters[col] || ''}
                            onChange={e => setColFilters(prev => ({ ...prev, [col]: e.target.value }))}
                            onKeyDown={handleFilterKeyDown}
                            style={{ width: '100%', minWidth: 30, height: 18, fontSize: 8, padding: '0 3px 0 12',
                              border: `1px solid ${colFilters[col] ? C.primaryBd : '#e2e8f0'}`, borderRadius: 2,
                              outline: 'none', background: colFilters[col] ? '#eff6ff' : '#fff', boxSizing: 'border-box' }}
                          />
                        </div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data.map((row, i) => (
                    <tr key={i} style={{ background: row.IS_NEW ? '#fffbeb' : i % 2 ? '#fafbfc' : '#fff' }}>
                      {preview.columns.map(col => (
                        <td key={col} style={{ padding: '3px 6px', borderBottom: '1px solid #f1f5f9',
                          whiteSpace: 'nowrap', fontFamily: typeof row[col] === 'number' ? 'monospace' : 'inherit',
                          textAlign: col === 'IS_NEW' ? 'center' : typeof row[col] === 'number' ? 'right' : 'left',
                          color: col === 'IS_NEW' ? (row[col] ? C.amber : C.green)
                            : col === 'OPT_TYPE' ? (row[col] === 'RL' ? C.green : row[col] === 'NL' ? C.amber : row[col] === 'MIX-L' ? C.red : C.textMuted)
                            : C.text,
                          fontWeight: col === 'IS_NEW' || col === 'OPT_TYPE' ? 700 : 400 }}>
                          {col === 'IS_NEW' ? (row[col] ? 'NEW' : 'OK')
                            : col === 'OPT_TYPE' ? (row[col] || '-')
                            : col === 'GEN_ART_NUMBER' || col === 'ARTICLE_NUMBER' || col === 'MATNR'
                              ? row[col] ?? ''
                            : typeof row[col] === 'number'
                              ? (col.toUpperCase().includes('CONT') ? row[col].toFixed(4)
                                : col.toUpperCase().includes('SAL') || col.toUpperCase().includes('SALE') ? row[col].toFixed(2)
                                : Math.round(row[col]).toLocaleString())
                            : row[col] ?? ''}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div style={{ padding: '6px 12px', borderTop: '1px solid #e2e8f0', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 10, color: C.textMuted }}>
                Page {previewPage} of {totalPages} ({preview.total.toLocaleString()} rows)
              </span>
              <div style={{ display: 'flex', gap: 4 }}>
                <button disabled={previewPage <= 1} onClick={() => loadPreview(previewPage - 1)}
                  style={{ height: 24, fontSize: 10, padding: '0 8px', borderRadius: 3, border: '1px solid #e2e8f0',
                    background: '#fff', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 2,
                    opacity: previewPage <= 1 ? 0.4 : 1 }}>
                  <ChevronLeft size={11}/> Prev
                </button>
                <button disabled={previewPage >= totalPages} onClick={() => loadPreview(previewPage + 1)}
                  style={{ height: 24, fontSize: 10, padding: '0 8px', borderRadius: 3, border: '1px solid #e2e8f0',
                    background: '#fff', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 2,
                    opacity: previewPage >= totalPages ? 0.4 : 1 }}>
                  Next <ChevronRight size={11}/>
                </button>
              </div>
            </div>
          </>
        ) : (
          <div style={{ padding: 30, textAlign: 'center' }}>
            <Database size={28} style={{ color: '#c7d2fe', margin: '0 auto 8px' }}/>
            <div style={{ fontSize: 12, fontWeight: 600, color: C.textSub }}>
              {config?.listing_exists ? 'Click Fetch to load preview' : 'Generate listing first'}
            </div>
          </div>
        )}
      </div>

      {/* ═══════════ Chart expand modal (SSN / DIV) ═══════════ */}
      {expandedChart && (() => {
        const isSSN = expandedChart === 'ssn'
        const chartData = isSSN ? (summary?.by_ssn || []) : (summary?.by_div || [])
        const title = isSSN ? 'Alloc Qty by Season (SSN)' : 'Alloc & Hold by Division (DIV)'
        return (
          <div onClick={() => setExpandedChart(null)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.55)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              zIndex: 1100,
            }}>
            <div onClick={(e) => e.stopPropagation()}
              style={{
                background: '#fff', borderRadius: 10, width: '92vw', maxWidth: 1100,
                maxHeight: '90vh', display: 'flex', flexDirection: 'column',
                boxShadow: '0 16px 48px rgba(0,0,0,0.22)',
              }}>
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '12px 18px', borderBottom: '1px solid #e2e8f0',
              }}>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{title}</div>
                  <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                    {chartData.length} group(s) · total alloc: {chartData.reduce((s, r) => s + (r.alloc_qty || 0), 0).toLocaleString()}
                  </div>
                </div>
                <button onClick={() => setExpandedChart(null)}
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, color: C.textSub }}>
                  <X size={18}/>
                </button>
              </div>
              <div style={{ flex: 1, padding: '16px 18px', overflowY: 'auto' }}>
                <ResponsiveContainer width="100%" height={Math.max(380, chartData.length * 36)}>
                  {isSSN ? (
                    <BarChart data={chartData} layout="vertical" margin={{ top: 5, right: 80, left: 10, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                      <XAxis type="number" fontSize={11}/>
                      <YAxis type="category" dataKey="ssn" fontSize={11} width={100} interval={0}/>
                      <Tooltip formatter={(v) => v.toLocaleString()}/>
                      <Bar dataKey="alloc_qty" name="Alloc" fill={C.primary} radius={[0, 4, 4, 0]}
                        label={{ position: 'right', fontSize: 10, fontWeight: 700, fill: C.text,
                          formatter: (v) => v.toLocaleString() }}/>
                    </BarChart>
                  ) : (
                    <BarChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 40 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9"/>
                      <XAxis dataKey="div" fontSize={11} angle={-25} textAnchor="end"/>
                      <YAxis fontSize={11}/>
                      <Tooltip formatter={(v) => v.toLocaleString()}/>
                      <Legend/>
                      <Bar dataKey="alloc_qty" name="Alloc" fill="#7c3aed" radius={[4, 4, 0, 0]}/>
                      <Bar dataKey="hold_qty"  name="Hold"  fill="#f59e0b" radius={[4, 4, 0, 0]}/>
                    </BarChart>
                  )}
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        )
      })()}

      {/* ═══ MAJ_CAT modal — one row per MAJ_CAT, RDC-wise columns ═══ */}
      {majCatModalOpen && (() => {
        const raw     = summary?.by_maj_cat_rdc || []
        const rdcs    = [...new Set(raw.map(r => r.rdc))].sort()
        const majcats = [...new Set(raw.map(r => r.maj_cat))].sort()
        // Residual FNL_Q = stock − alloc − hold (clamped at 0). REQ_REM = req − alloc
        // (also clamped). Both computed here so the modal stays in sync with
        // whatever the backend returns.
        const residual = c => Math.max(0, (c.stock_avail || 0) - (c.alloc_qty || 0) - (c.hold_qty || 0))
        const reqRem   = c => Math.max(0, (c.req_qty || 0) - (c.alloc_qty || 0))
        // REQ% = ALLOC / REQ × 100 (how much of REQ got filled). FILL% =
        // (STORE_STK + ALLOC) / MBQ × 100 (post-alloc fill rate against MBQ).
        const reqPct  = c => (c.req_qty || 0) > 0 ? (c.alloc_qty || 0) / c.req_qty * 100 : 0
        const fillPct = c => (c.mbq_qty || 0) > 0 ? ((c.store_stk || 0) + (c.alloc_qty || 0)) / c.mbq_qty * 100 : 0
        // lookup[maj_cat][rdc] = {alloc_qty, stock_avail, hold_qty, req_qty, mbq_qty, store_stk, excess_stk}
        const lookup = {}
        raw.forEach(r => {
          if (!lookup[r.maj_cat]) lookup[r.maj_cat] = {}
          lookup[r.maj_cat][r.rdc] = {
            alloc_qty:   r.alloc_qty   || 0,
            stock_avail: r.stock_avail || 0,
            hold_qty:    r.hold_qty    || 0,
            req_qty:     r.req_qty     || 0,
            mbq_qty:     r.mbq_qty     || 0,
            store_stk:   r.store_stk   || 0,
            excess_stk:  r.excess_stk  || 0,
          }
        })
        let rows = majcats.map(mc => {
          const d          = lookup[mc] || {}
          const totalAlloc = rdcs.reduce((s, rdc) => s + (d[rdc]?.alloc_qty   || 0), 0)
          const totalStock = rdcs.reduce((s, rdc) => s + (d[rdc]?.stock_avail || 0), 0)
          const totalHold  = rdcs.reduce((s, rdc) => s + (d[rdc]?.hold_qty    || 0), 0)
          const totalReq   = rdcs.reduce((s, rdc) => s + (d[rdc]?.req_qty     || 0), 0)
          const totalMbq   = rdcs.reduce((s, rdc) => s + (d[rdc]?.mbq_qty     || 0), 0)
          const totalStkS  = rdcs.reduce((s, rdc) => s + (d[rdc]?.store_stk   || 0), 0)
          const totalExcess= rdcs.reduce((s, rdc) => s + (d[rdc]?.excess_stk  || 0), 0)
          const totalFnl   = Math.max(0, totalStock - totalAlloc - totalHold)
          const totalReqRem= Math.max(0, totalReq - totalAlloc)
          const totalPct   = totalStock > 0 ? totalAlloc / totalStock * 100 : 0
          const totalReqPct  = totalReq > 0 ? totalAlloc / totalReq * 100 : 0
          const totalFillPct = totalMbq > 0 ? (totalStkS + totalAlloc) / totalMbq * 100 : 0
          return { maj_cat: mc, d, totalAlloc, totalStock, totalHold, totalReq,
                   totalReqRem, totalFnl, totalPct, totalMbq, totalStkS, totalExcess,
                   totalReqPct, totalFillPct }
        }).filter(r => r.totalAlloc > 0 || r.totalStock > 0 || r.totalReq > 0 || r.totalHold > 0 || r.totalMbq > 0)

        // filter
        if (mcFilter.trim()) {
          rows = rows.filter(r => r.maj_cat.toLowerCase().includes(mcFilter.trim().toLowerCase()))
        }

        // sort
        rows = [...rows].sort((a, b) => {
          let av, bv
          if (mcSortCol === 'maj_cat') { av = a.maj_cat; bv = b.maj_cat }
          else if (mcSortCol === 'totalAlloc') { av = a.totalAlloc; bv = b.totalAlloc }
          else if (mcSortCol === 'totalStock') { av = a.totalStock; bv = b.totalStock }
          else if (mcSortCol === 'totalHold')  { av = a.totalHold;  bv = b.totalHold }
          else if (mcSortCol === 'totalReq')   { av = a.totalReq;   bv = b.totalReq }
          else if (mcSortCol === 'totalReqRem'){ av = a.totalReqRem;bv = b.totalReqRem }
          else if (mcSortCol === 'totalFnl')   { av = a.totalFnl;   bv = b.totalFnl }
          else if (mcSortCol === 'totalPct')     { av = a.totalPct;     bv = b.totalPct }
          else if (mcSortCol === 'totalMbq')     { av = a.totalMbq;     bv = b.totalMbq }
          else if (mcSortCol === 'totalStkS')    { av = a.totalStkS;    bv = b.totalStkS }
          else if (mcSortCol === 'totalExcess')  { av = a.totalExcess;  bv = b.totalExcess }
          else if (mcSortCol === 'totalReqPct')  { av = a.totalReqPct;  bv = b.totalReqPct }
          else if (mcSortCol === 'totalFillPct') { av = a.totalFillPct; bv = b.totalFillPct }
          else if (mcSortCol.startsWith('alloc_')) { const rdc = mcSortCol.slice(6); av = a.d[rdc]?.alloc_qty || 0; bv = b.d[rdc]?.alloc_qty || 0 }
          else if (mcSortCol.startsWith('stock_')) { const rdc = mcSortCol.slice(6); av = a.d[rdc]?.stock_avail || 0; bv = b.d[rdc]?.stock_avail || 0 }
          else if (mcSortCol.startsWith('stkS_'))  { const rdc = mcSortCol.slice(5); av = a.d[rdc]?.store_stk  || 0; bv = b.d[rdc]?.store_stk  || 0 }
          else if (mcSortCol.startsWith('excess_')){ const rdc = mcSortCol.slice(7); av = a.d[rdc]?.excess_stk || 0; bv = b.d[rdc]?.excess_stk || 0 }
          else if (mcSortCol.startsWith('hold_'))  { const rdc = mcSortCol.slice(5); av = a.d[rdc]?.hold_qty   || 0; bv = b.d[rdc]?.hold_qty   || 0 }
          else if (mcSortCol.startsWith('req_'))   { const rdc = mcSortCol.slice(4); av = a.d[rdc]?.req_qty    || 0; bv = b.d[rdc]?.req_qty    || 0 }
          else if (mcSortCol.startsWith('mbq_'))   { const rdc = mcSortCol.slice(4); av = a.d[rdc]?.mbq_qty    || 0; bv = b.d[rdc]?.mbq_qty    || 0 }
          else if (mcSortCol.startsWith('rrem_'))  { const rdc = mcSortCol.slice(5); av = reqRem(a.d[rdc] || {}); bv = reqRem(b.d[rdc] || {}) }
          else if (mcSortCol.startsWith('fnl_'))   { const rdc = mcSortCol.slice(4); av = residual(a.d[rdc] || {}); bv = residual(b.d[rdc] || {}) }
          else if (mcSortCol.startsWith('rpct_'))  { const rdc = mcSortCol.slice(5); av = reqPct(a.d[rdc] || {}); bv = reqPct(b.d[rdc] || {}) }
          else if (mcSortCol.startsWith('fpct_'))  { const rdc = mcSortCol.slice(5); av = fillPct(a.d[rdc] || {}); bv = fillPct(b.d[rdc] || {}) }
          else if (mcSortCol.startsWith('pct_'))   { const rdc = mcSortCol.slice(4);  const ca = a.d[rdc] || {}; const cb = b.d[rdc] || {}; av = ca.stock_avail > 0 ? ca.alloc_qty / ca.stock_avail : 0; bv = cb.stock_avail > 0 ? cb.alloc_qty / cb.stock_avail : 0 }
          else { av = a.totalAlloc; bv = b.totalAlloc }
          if (av === bv) return 0
          if (mcSortCol === 'maj_cat') return mcSortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
          return mcSortDir === 'asc' ? av - bv : bv - av
        })

        const grandAlloc = rows.reduce((s, r) => s + r.totalAlloc, 0)
        const grandStock = rows.reduce((s, r) => s + r.totalStock, 0)
        const grandHold  = rows.reduce((s, r) => s + r.totalHold,  0)
        const grandReq   = rows.reduce((s, r) => s + r.totalReq,   0)
        const grandMbq   = rows.reduce((s, r) => s + r.totalMbq,   0)
        const grandStkS  = rows.reduce((s, r) => s + r.totalStkS,  0)
        const grandExcess= rows.reduce((s, r) => s + r.totalExcess,0)
        const grandFnl   = Math.max(0, grandStock - grandAlloc - grandHold)
        const grandReqRem= Math.max(0, grandReq - grandAlloc)
        const grandReqPct  = grandReq > 0 ? grandAlloc / grandReq * 100 : 0
        const grandFillPct = grandMbq > 0 ? (grandStkS + grandAlloc) / grandMbq * 100 : 0

        const exportMajCatExcel = () => {
          // Column order matches the on-screen modal:
          // MBQ, STOCK, STORE_STK, EXCESS_STK, REQ, ALLOC, REQ%, FILL%, REQ_REM, HOLD, MSA_REM, STK%.
          const headers = ['#', 'MAJ_CAT']
          rdcs.forEach(rdc => {
            headers.push(
              `${rdc} MBQ`,        `${rdc} STOCK`,     `${rdc} STORE_STK`,
              `${rdc} EXCESS_STK`, `${rdc} REQ`,       `${rdc} ALLOC`,
              `${rdc} REQ%`,       `${rdc} FILL%`,     `${rdc} REQ_REM`,
              `${rdc} HOLD`,       `${rdc} MSA_REM`,   `${rdc} STK%`,
            )
          })
          headers.push(
            'TOTAL MBQ',        'TOTAL STOCK',     'TOTAL STORE_STK',
            'TOTAL EXCESS_STK', 'TOTAL REQ',       'TOTAL ALLOC',
            'TOTAL REQ%',       'TOTAL FILL%',     'TOTAL REQ_REM',
            'TOTAL HOLD',       'TOTAL MSA_REM',   'TOTAL STK%',
          )
          const data = rows.map((row, i) => {
            const r = { '#': i + 1, MAJ_CAT: row.maj_cat }
            rdcs.forEach(rdc => {
              const cell = row.d[rdc] || { alloc_qty: 0, stock_avail: 0, hold_qty: 0, req_qty: 0, mbq_qty: 0, store_stk: 0, excess_stk: 0 }
              const pct   = cell.stock_avail > 0 ? parseFloat((cell.alloc_qty / cell.stock_avail * 100).toFixed(1)) : 0
              const rpct  = parseFloat(reqPct(cell).toFixed(1))
              const fpct  = parseFloat(fillPct(cell).toFixed(1))
              r[`${rdc} MBQ`]        = cell.mbq_qty
              r[`${rdc} STOCK`]      = cell.stock_avail
              r[`${rdc} STORE_STK`]  = cell.store_stk
              r[`${rdc} EXCESS_STK`] = cell.excess_stk
              r[`${rdc} REQ`]        = cell.req_qty
              r[`${rdc} ALLOC`]      = cell.alloc_qty
              r[`${rdc} REQ%`]       = rpct
              r[`${rdc} FILL%`]      = fpct
              r[`${rdc} REQ_REM`]    = reqRem(cell)
              r[`${rdc} HOLD`]       = cell.hold_qty
              r[`${rdc} MSA_REM`]    = residual(cell)
              r[`${rdc} STK%`]       = pct
            })
            r['TOTAL MBQ']        = row.totalMbq
            r['TOTAL STOCK']      = row.totalStock
            r['TOTAL STORE_STK']  = row.totalStkS
            r['TOTAL EXCESS_STK'] = row.totalExcess
            r['TOTAL REQ']        = row.totalReq
            r['TOTAL ALLOC']      = row.totalAlloc
            r['TOTAL REQ%']       = parseFloat(row.totalReqPct.toFixed(1))
            r['TOTAL FILL%']      = parseFloat(row.totalFillPct.toFixed(1))
            r['TOTAL REQ_REM']    = row.totalReqRem
            r['TOTAL HOLD']       = row.totalHold
            r['TOTAL MSA_REM']    = row.totalFnl
            r['TOTAL STK%']       = row.totalStock > 0 ? parseFloat(row.totalPct.toFixed(1)) : 0
            return r
          })
          // Grand total row
          const grand = { '#': '', MAJ_CAT: 'TOTAL' }
          rdcs.forEach(rdc => {
            const s  = rows.reduce((a, row) => a + (row.d[rdc]?.stock_avail || 0), 0)
            const q  = rows.reduce((a, row) => a + (row.d[rdc]?.alloc_qty  || 0), 0)
            const h  = rows.reduce((a, row) => a + (row.d[rdc]?.hold_qty   || 0), 0)
            const rq = rows.reduce((a, row) => a + (row.d[rdc]?.req_qty    || 0), 0)
            const mb = rows.reduce((a, row) => a + (row.d[rdc]?.mbq_qty    || 0), 0)
            const sk = rows.reduce((a, row) => a + (row.d[rdc]?.store_stk  || 0), 0)
            const ex = rows.reduce((a, row) => a + (row.d[rdc]?.excess_stk || 0), 0)
            grand[`${rdc} MBQ`]        = mb
            grand[`${rdc} STOCK`]      = s
            grand[`${rdc} STORE_STK`]  = sk
            grand[`${rdc} EXCESS_STK`] = ex
            grand[`${rdc} REQ`]        = rq
            grand[`${rdc} ALLOC`]      = q
            grand[`${rdc} REQ%`]       = rq > 0 ? parseFloat((q / rq * 100).toFixed(1)) : 0
            grand[`${rdc} FILL%`]      = mb > 0 ? parseFloat(((sk + q) / mb * 100).toFixed(1)) : 0
            grand[`${rdc} REQ_REM`]    = Math.max(0, rq - q)
            grand[`${rdc} HOLD`]       = h
            grand[`${rdc} MSA_REM`]    = Math.max(0, s - q - h)
            grand[`${rdc} STK%`]       = s > 0 ? parseFloat((q / s * 100).toFixed(1)) : 0
          })
          grand['TOTAL MBQ']        = grandMbq
          grand['TOTAL STOCK']      = grandStock
          grand['TOTAL STORE_STK']  = grandStkS
          grand['TOTAL EXCESS_STK'] = grandExcess
          grand['TOTAL REQ']        = grandReq
          grand['TOTAL ALLOC']      = grandAlloc
          grand['TOTAL REQ%']       = parseFloat(grandReqPct.toFixed(1))
          grand['TOTAL FILL%']      = parseFloat(grandFillPct.toFixed(1))
          grand['TOTAL REQ_REM']    = grandReqRem
          grand['TOTAL HOLD']       = grandHold
          grand['TOTAL MSA_REM']    = grandFnl
          grand['TOTAL STK%']       = grandStock > 0 ? parseFloat((grandAlloc / grandStock * 100).toFixed(1)) : 0
          data.push(grand)
          const ws = XLSX.utils.json_to_sheet(data, { header: headers })
          const wb = XLSX.utils.book_new()
          XLSX.utils.book_append_sheet(wb, ws, 'MAJ_CAT Summary')
          XLSX.writeFile(wb, `majcat_summary_${new Date().toISOString().slice(0,10)}.xlsx`)
        }

        const thSort = (col, label, style = {}) => {
          const active = mcSortCol === col
          return (
            <th onClick={() => { if (active) setMcSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setMcSortCol(col); setMcSortDir('desc') } }}
              style={{ cursor: 'pointer', userSelect: 'none', ...style }}>
              {label}{active ? (mcSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
            </th>
          )
        }

        // Per-cell calc popover. metric tells the popover which formula
        // to render; cell carries the values (mbq, stock, alloc, etc.).
        // For STORE_STOCK the popover also fires an async fetch to load
        // the SLOC inventory breakdown.
        const openCalc = (metric, mc, rdc, cell, ev) => {
          ev?.stopPropagation?.()
          const rect = ev?.currentTarget?.getBoundingClientRect?.()
          const anchor = rect ? { x: rect.right, y: rect.bottom } : { x: 0, y: 0 }
          setCalcPopover({ metric, mc, rdc, cell, anchor, sloc: null, slocLoading: false })
          if (metric === 'STORE_STK') {
            setCalcPopover(p => p ? { ...p, slocLoading: true } : p)
            listingAPI.slocBreakdown(mc, rdc || undefined)
              .then(res => setCalcPopover(p => p
                ? { ...p, sloc: res?.data?.data || [], stkTtl: res?.data?.stk_ttl || 0, slocLoading: false }
                : p))
              .catch(() => setCalcPopover(p => p ? { ...p, sloc: [], slocLoading: false } : p))
          }
        }

        // Local alias — pushes to the unified drill stack so the store
        // table renders in the same modal frame instead of opening a
        // separate overlay.
        const openStoreDrill = (mc, rdc) => openStoreDrillNew(mc, rdc)

        // Only render the MAJ_CAT root table when no drill is active.
        // When the user drills (drill.view set), one of the conditional
        // drill modals below renders inside the same frame instead.
        if (drill) return null

        return (
          <div onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
            style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'stretch', justifyContent: 'stretch', zIndex: 1000 }}>
            <div onClick={e => e.stopPropagation()}
              style={{ background: '#fff', width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderBottom: '1px solid #e2e8f0' }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>MAJ_CATs that ran ({rows.length})</div>
                  <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                    Stock {grandStock.toLocaleString()}
                    {grandStkS   > 0 ? ` · STORE_STK ${grandStkS.toLocaleString()}`   : ''}
                    {grandExcess > 0 ? ` · EXCESS ${grandExcess.toLocaleString()}`    : ''}
                    {grandReq    > 0 ? ` · REQ ${grandReq.toLocaleString()}`          : ''}
                    {' '}· ALLOC {grandAlloc.toLocaleString()}
                    {grandReq    > 0 ? ` · REQ% ${grandReqPct.toFixed(1)}%`           : ''}
                    {grandMbq    > 0 ? ` · MBQ ${grandMbq.toLocaleString()} · FILL% ${grandFillPct.toFixed(1)}%` : ''}
                    {grandReqRem > 0 ? ` · REQ_REM ${grandReqRem.toLocaleString()}`   : ''}
                    {grandHold   > 0 ? ` · HOLD ${grandHold.toLocaleString()}`        : ''}
                    {' '}· MSA_REM {grandFnl.toLocaleString()}
                    {grandStock > 0 ? ` · utilisation ${(grandAlloc / grandStock * 100).toFixed(1)}%` : ''}
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <div style={{ position: 'relative' }}>
                    <Search size={12} style={{ position: 'absolute', left: 7, top: '50%', transform: 'translateY(-50%)', color: C.textMuted, pointerEvents: 'none' }} />
                    <input
                      value={mcFilter}
                      onChange={e => setMcFilter(e.target.value)}
                      placeholder="Filter MAJ_CAT…"
                      style={{ paddingLeft: 24, paddingRight: 8, height: 28, fontSize: 11, border: '1px solid #e2e8f0', borderRadius: 6, outline: 'none', width: 160 }}
                    />
                    {mcFilter && <button onClick={() => setMcFilter('')} style={{ position: 'absolute', right: 6, top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: C.textMuted }}><X size={10}/></button>}
                  </div>
                  <button onClick={exportMajCatExcel} title="Export to Excel"
                    style={{ display: 'flex', alignItems: 'center', gap: 4, height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600, background: '#16a34a', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
                    <Download size={12}/> Excel
                  </button>
                  <button onClick={() => setMajCatModalOpen(false)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, color: C.textSub }}><X size={16}/></button>
                </div>
              </div>
              <div style={{ overflow: 'auto', padding: '4px 0' }}>
                {rows.length === 0
                  ? <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>No data available.</div>
                  : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
                      {/* RDC group header */}
                      <tr style={{ background: '#f1f5f9' }}>
                        <th style={{ padding: '4px 10px', textAlign: 'left', width: 28, background: '#f8fafc' }}>#</th>
                        <th style={{ padding: '4px 10px', textAlign: 'left', minWidth: 130, background: '#f8fafc', fontSize: 9, color: C.textSub, letterSpacing: '.04em' }}>MAJ_CAT</th>
                        {rdcs.map(rdc => (
                          <th key={rdc} colSpan={12} style={{ padding: '4px 6px', textAlign: 'center', borderLeft: '1px solid #e2e8f0', fontSize: 9, fontWeight: 700, color: C.text, letterSpacing: '.03em' }}>
                            {rdc}
                          </th>
                        ))}
                        <th colSpan={12} style={{ padding: '4px 6px', textAlign: 'center', borderLeft: '2px solid #cbd5e1', fontSize: 9, color: C.textSub, letterSpacing: '.03em', background: '#f8fafc' }}>TOTAL</th>
                      </tr>
                      {/* Sub-column labels — sortable. Order:
                          MBQ, STOCK, STORE_STK, EXCESS_STK, REQ, ALLOC, REQ%,
                          FILL%, REQ_REM, HOLD, MSA_REM, STK%. */}
                      <tr style={{ background: '#f8fafc', borderBottom: '1px solid #e2e8f0' }}>
                        <th/>
                        {thSort('maj_cat', 'MAJ_CAT', { padding: '3px 10px', textAlign: 'left', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {rdcs.map(rdc => (
                          <React.Fragment key={rdc}>
                            {thSort(`mbq_${rdc}`,    'MBQ',        { padding: '3px 6px', textAlign: 'right', borderLeft: '1px solid #e8edf3', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`stock_${rdc}`,  'STOCK',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`stkS_${rdc}`,   'STORE_STK',  { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`excess_${rdc}`, 'EXCESS_STK', { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`req_${rdc}`,    'REQ',        { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`alloc_${rdc}`,  'ALLOC',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`rpct_${rdc}`,   'REQ%',       { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 46 })}
                            {thSort(`fpct_${rdc}`,   'FILL%',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 48 })}
                            {thSort(`rrem_${rdc}`,   'REQ_REM',    { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            {thSort(`hold_${rdc}`,   'HOLD',       { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                            <th onClick={() => { if (mcSortCol === `fnl_${rdc}`) setMcSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setMcSortCol(`fnl_${rdc}`); setMcSortDir('desc') } }}
                                title="MSA pool remaining after alloc & hold"
                                style={{ cursor: 'pointer', userSelect: 'none', padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 }}>
                              MSA_REM{mcSortCol === `fnl_${rdc}` ? (mcSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                            </th>
                            <th onClick={() => { if (mcSortCol === `pct_${rdc}`) setMcSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setMcSortCol(`pct_${rdc}`); setMcSortDir('desc') } }}
                                title="% of stock consumption (alloc / stock)"
                                style={{ cursor: 'pointer', userSelect: 'none', padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 50 }}>
                              STK%{mcSortCol === `pct_${rdc}` ? (mcSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                            </th>
                          </React.Fragment>
                        ))}
                        {thSort('totalMbq',     'MBQ',        { padding: '3px 6px', textAlign: 'right', borderLeft: '2px solid #cbd5e1', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalStock',   'STOCK',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalStkS',    'STORE_STK',  { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalExcess',  'EXCESS_STK', { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalReq',     'REQ',        { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalAlloc',   'ALLOC',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalReqPct',  'REQ%',       { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 46 })}
                        {thSort('totalFillPct', 'FILL%',      { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 48 })}
                        {thSort('totalReqRem',  'REQ_REM',    { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        {thSort('totalHold',    'HOLD',       { padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 })}
                        <th onClick={() => { if (mcSortCol === 'totalFnl') setMcSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setMcSortCol('totalFnl'); setMcSortDir('desc') } }}
                            title="MSA pool remaining after alloc & hold"
                            style={{ cursor: 'pointer', userSelect: 'none', padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400 }}>
                          MSA_REM{mcSortCol === 'totalFnl' ? (mcSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                        </th>
                        <th onClick={() => { if (mcSortCol === 'totalPct') setMcSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setMcSortCol('totalPct'); setMcSortDir('desc') } }}
                            title="% of stock consumption (alloc / stock)"
                            style={{ cursor: 'pointer', userSelect: 'none', padding: '3px 6px', textAlign: 'right', fontSize: 8, color: C.textMuted, fontWeight: 400, width: 50 }}>
                          STK%{mcSortCol === 'totalPct' ? (mcSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, i) => {
                        // Cells are clickable — open store drill-down filtered to
                        // (MAJ_CAT, RDC) for per-RDC blocks, or (MAJ_CAT, null)
                        // for TOTAL columns / the MAJ_CAT name itself.
                        const drillStyle = { cursor: 'pointer' }
                        return (
                        <tr key={row.maj_cat} style={{ borderTop: '1px solid #f1f5f9' }}>
                          <td style={{ padding: '5px 10px', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{i + 1}</td>
                          <td onClick={() => openStoreDrill(row.maj_cat, null)}
                              title="Click for store-wise breakdown (all RDCs)"
                              style={{ padding: '5px 10px', fontWeight: 600, color: C.primary, whiteSpace: 'nowrap', cursor: 'pointer', textDecoration: 'underline dotted' }}>
                            {row.maj_cat}
                          </td>
                          {rdcs.map(rdc => {
                            const cell = row.d[rdc] || { alloc_qty: 0, stock_avail: 0, hold_qty: 0, req_qty: 0, mbq_qty: 0, store_stk: 0, excess_stk: 0 }
                            const pct   = cell.stock_avail > 0 ? cell.alloc_qty / cell.stock_avail * 100 : 0
                            const rpct  = reqPct(cell)
                            const fpct  = fillPct(cell)
                            const fnl   = residual(cell)
                            const rrem  = reqRem(cell)
                            const dash  = <span style={{ color: '#d1d5db' }}>—</span>
                            // Per-cell click: each metric opens its own calc popover.
                            const onCalc = (metric) => (e) =>
                              openCalc(metric, row.maj_cat, rdc, cell, e)
                            return (
                              <React.Fragment key={rdc}>
                                <td onClick={onCalc('MBQ')} title="Click for MBQ calc (ACS_D + rate × ALC_D)"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', borderLeft: '1px solid #f1f5f9', ...drillStyle }}>
                                  {cell.mbq_qty > 0 ? Math.round(cell.mbq_qty).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('STOCK')} title="MSA pool stock available for this RDC (Σ FNL_Q across sizes)"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.stock_avail > 0 ? cell.stock_avail.toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('STORE_STK')} title="Click for SLOC-wise inventory breakdown"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.store_stk > 0 ? Math.round(cell.store_stk).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('EXCESS')} title="STK_TTL > excess_multiplier × OPT_MBQ"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: cell.excess_stk > 0 ? '#ea580c' : C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.excess_stk > 0 ? Math.round(cell.excess_stk).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('REQ')} title="REQ = max(0, MBQ − STORE_STK)"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.req_qty > 0 ? Math.round(cell.req_qty).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('ALLOC')} title="Click for alloc/hold/fill details"
                                    style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.alloc_qty > 0 ? Math.round(cell.alloc_qty).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: rpct >= 90 ? '#10b981' : rpct >= 60 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {cell.req_qty > 0 ? `${rpct.toFixed(0)}%` : dash}
                                </td>
                                <td onClick={onCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: fpct >= 100 ? '#10b981' : fpct >= 70 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {cell.mbq_qty > 0 ? `${fpct.toFixed(0)}%` : dash}
                                </td>
                                <td onClick={onCalc('REQ_REM')} title="REQ − ALLOC (clamped to 0)"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {rrem > 0 ? rrem.toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('ALLOC')} title="Warehouse hold (TBL buffer / RL-TBC from-hold)"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {cell.hold_qty > 0 ? Math.round(cell.hold_qty).toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('MSA_REM')} title="MSA pool remaining after alloc & hold"
                                    style={{ padding: '5px 6px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums', ...drillStyle }}>
                                  {fnl > 0 ? fnl.toLocaleString() : dash}
                                </td>
                                <td onClick={onCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: pct >= 90 ? '#10b981' : pct >= 60 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {cell.stock_avail > 0 ? `${pct.toFixed(0)}%` : dash}
                                </td>
                              </React.Fragment>
                            )
                          })}
                          {(() => {
                            // Aggregate cell pretending to be a "TOTAL RDC".
                            const totCell = {
                              alloc_qty:   row.totalAlloc,
                              stock_avail: row.totalStock,
                              hold_qty:    row.totalHold,
                              req_qty:     row.totalReq,
                              mbq_qty:     row.totalMbq,
                              store_stk:   row.totalStkS,
                              excess_stk:  row.totalExcess,
                            }
                            const onTotCalc = (metric) => (e) =>
                              openCalc(metric, row.maj_cat, null, totCell, e)
                            const dash = <span style={{ color: '#d1d5db' }}>—</span>
                            return (
                              <React.Fragment>
                                <td onClick={onTotCalc('MBQ')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, borderLeft: '2px solid #e2e8f0', ...drillStyle }}>
                                  {row.totalMbq > 0 ? row.totalMbq.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('STOCK')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalStock > 0 ? row.totalStock.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('STORE_STK')} title="Click for SLOC-wise inventory breakdown"
                                    style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalStkS > 0 ? row.totalStkS.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('EXCESS')} title="Sum of EXCESS_STK from ARS_LISTING"
                                    style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: row.totalExcess > 0 ? '#ea580c' : C.textSub, ...drillStyle }}>
                                  {row.totalExcess > 0 ? row.totalExcess.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('REQ')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalReq > 0 ? row.totalReq.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 600, ...drillStyle }}>
                                  {row.totalAlloc.toLocaleString()}
                                </td>
                                <td onClick={onTotCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: row.totalReqPct >= 90 ? '#10b981' : row.totalReqPct >= 60 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {row.totalReq > 0 ? `${row.totalReqPct.toFixed(0)}%` : dash}
                                </td>
                                <td onClick={onTotCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: row.totalFillPct >= 100 ? '#10b981' : row.totalFillPct >= 70 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {row.totalMbq > 0 ? `${row.totalFillPct.toFixed(0)}%` : dash}
                                </td>
                                <td onClick={onTotCalc('REQ_REM')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalReqRem > 0 ? row.totalReqRem.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalHold > 0 ? row.totalHold.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('MSA_REM')} title="MSA pool remaining after alloc & hold"
                                    style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', color: C.textSub, ...drillStyle }}>
                                  {row.totalFnl > 0 ? row.totalFnl.toLocaleString() : dash}
                                </td>
                                <td onClick={onTotCalc('ALLOC')} style={{ padding: '5px 6px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: row.totalPct >= 90 ? '#10b981' : row.totalPct >= 60 ? '#f59e0b' : C.textMuted, ...drillStyle }}>
                                  {row.totalStock > 0 ? `${row.totalPct.toFixed(0)}%` : '—'}
                                </td>
                              </React.Fragment>
                            )
                          })()}
                        </tr>
                      )})}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
      })()}

      {/* ═══ Store drill-down — per-store breakdown for ONE MAJ_CAT (+ optional RDC) ═══ */}
      {majCatModalOpen && drill?.view === 'store' && (() => {
        const items = [...(storeDrillData || [])]
        const sd = storeDrillSortDir === 'asc' ? 1 : -1
        const reqPctRow  = r => (r.req_qty || 0) > 0 ? (r.alloc_qty || 0) / r.req_qty * 100 : 0
        const fillPctRow = r => (r.mbq_qty || 0) > 0 ? ((r.store_stk || 0) + (r.alloc_qty || 0)) / r.mbq_qty * 100 : 0
        const fnlRow     = r => Math.max(0, (r.store_stk || 0) + (r.alloc_qty || 0) - (r.hold_qty || 0))
        const reqRemRow  = r => Math.max(0, (r.req_qty || 0) - (r.alloc_qty || 0))
        items.sort((a, b) => {
          let av, bv
          if (storeDrillSortCol === 'werks')          { av = a.werks || ''; bv = b.werks || ''; return sd * av.localeCompare(bv) }
          if (storeDrillSortCol === 'req_pct')        { av = reqPctRow(a);  bv = reqPctRow(b) }
          else if (storeDrillSortCol === 'fill_pct')  { av = fillPctRow(a); bv = fillPctRow(b) }
          else if (storeDrillSortCol === 'req_rem')   { av = reqRemRow(a);  bv = reqRemRow(b) }
          else if (storeDrillSortCol === 'fnl_rem')   { av = fnlRow(a);     bv = fnlRow(b) }
          else { av = a[storeDrillSortCol] || 0; bv = b[storeDrillSortCol] || 0 }
          return av === bv ? 0 : sd * (av - bv)
        })
        const tAlloc = items.reduce((s, r) => s + (r.alloc_qty || 0), 0)
        const tHold  = items.reduce((s, r) => s + (r.hold_qty  || 0), 0)
        const tReq   = items.reduce((s, r) => s + (r.req_qty   || 0), 0)
        const tMbq   = items.reduce((s, r) => s + (r.mbq_qty   || 0), 0)
        const tStk   = items.reduce((s, r) => s + (r.store_stk || 0), 0)
        const tReqPct  = tReq > 0 ? tAlloc / tReq * 100 : 0
        const tFillPct = tMbq > 0 ? (tStk + tAlloc) / tMbq * 100 : 0

        const exportDrill = () => {
          // Order matches the parent MAJ_CAT modal:
          // MBQ, STORE_STK, REQ, ALLOC, REQ%, FILL%, REQ_REM, HOLD, FNL_Q_REM.
          const data = items.map((r, i) => ({
            '#': i + 1,
            STORE:        r.werks,
            MBQ:          r.mbq_qty   || 0,
            STORE_STK:    r.store_stk || 0,
            REQ:          r.req_qty   || 0,
            ALLOC:        r.alloc_qty || 0,
            'REQ%':       parseFloat(reqPctRow(r).toFixed(1)),
            'FILL%':      parseFloat(fillPctRow(r).toFixed(1)),
            REQ_REM:      reqRemRow(r),
            HOLD:         r.hold_qty  || 0,
            FNL_Q_REM:    fnlRow(r),
          }))
          data.push({
            '#': '', STORE: 'TOTAL',
            MBQ: tMbq, STORE_STK: tStk, REQ: tReq, ALLOC: tAlloc,
            'REQ%':  parseFloat(tReqPct.toFixed(1)),
            'FILL%': parseFloat(tFillPct.toFixed(1)),
            REQ_REM: Math.max(0, tReq - tAlloc), HOLD: tHold,
            FNL_Q_REM: Math.max(0, tStk + tAlloc - tHold),
          })
          const ws = XLSX.utils.json_to_sheet(data)
          const wb = XLSX.utils.book_new()
          const sn = `${storeDrillMajCat}${storeDrillRdc ? '_' + storeDrillRdc : ''}`.slice(0, 28)
          XLSX.utils.book_append_sheet(wb, ws, sn || 'StoreDrill')
          XLSX.writeFile(wb, `store_drill_${sn}_${new Date().toISOString().slice(0,10)}.xlsx`)
        }

        const sdTh = (col, label, style = {}) => {
          const active = storeDrillSortCol === col
          return (
            <th onClick={() => { if (active) setStoreDrillSortDir(d => d === 'asc' ? 'desc' : 'asc'); else { setStoreDrillSortCol(col); setStoreDrillSortDir('desc') } }}
              style={{ cursor: 'pointer', userSelect: 'none', padding: '4px 8px', fontSize: 9, color: C.textMuted, fontWeight: 500, ...style }}>
              {label}{active ? (storeDrillSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
            </th>
          )
        }

        const dash = <span style={{ color: '#d1d5db' }}>—</span>

        return (
          <div onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
            style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'stretch', justifyContent: 'stretch', zIndex: 1000 }}>
            <div onClick={e => e.stopPropagation()}
              style={{ background: '#fff', width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', borderBottom: '1px solid #e2e8f0' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button onClick={popDrill} title="Back to MAJ_CATs"
                    style={{ display: 'flex', alignItems: 'center', gap: 4,
                             height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600,
                             background: '#f1f5f9', color: C.text,
                             border: '1px solid #e2e8f0', borderRadius: 6,
                             cursor: 'pointer' }}>
                    ← Back
                  </button>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                      MAJ_CATs <span style={{ color: C.textMuted }}>›</span> Stores — {storeDrillMajCat}
                      {storeDrillRdc ? <span style={{ color: C.textSub, fontWeight: 500 }}> · RDC {storeDrillRdc}</span> : <span style={{ color: C.textSub, fontWeight: 500 }}> · all RDCs</span>}
                      {' '}({items.length})
                    </div>
                    <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                      {storeDrillLoading
                        ? 'Loading…'
                        : <>
                            STK {tStk.toLocaleString()} · REQ {tReq.toLocaleString()} · ALLOC {tAlloc.toLocaleString()}
                            {tReq > 0 ? ` · REQ% ${tReqPct.toFixed(1)}%` : ''}
                            {tMbq > 0 ? ` · MBQ ${tMbq.toLocaleString()} · FILL% ${tFillPct.toFixed(1)}%` : ''}
                            {tHold > 0 ? ` · HOLD ${tHold.toLocaleString()}` : ''}
                            {' '}· Click any row for OPT-wise drill.
                          </>}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <button onClick={exportDrill} disabled={!items.length}
                    style={{ display: 'flex', alignItems: 'center', gap: 4, height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600, background: items.length ? '#16a34a' : '#94a3b8', color: '#fff', border: 'none', borderRadius: 6, cursor: items.length ? 'pointer' : 'not-allowed' }}>
                    <Download size={12}/> Excel
                  </button>
                  <button onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
                    title="Close all"
                    style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, color: C.textSub }}>
                    <X size={16}/>
                  </button>
                </div>
              </div>
              <div style={{ overflow: 'auto', padding: '4px 0' }}>
                {storeDrillLoading ? (
                  <div style={{ padding: 32, textAlign: 'center', fontSize: 11, color: C.textMuted }}>Loading store-wise breakdown…</div>
                ) : items.length === 0 ? (
                  <div style={{ padding: 32, textAlign: 'center', fontSize: 11, color: C.textMuted }}>No stores received allocation for this MAJ_CAT{storeDrillRdc ? ` × ${storeDrillRdc}` : ''}.</div>
                ) : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead style={{ position: 'sticky', top: 0, background: '#f8fafc', borderBottom: '1px solid #e2e8f0', zIndex: 1 }}>
                      <tr>
                        <th style={{ padding: '4px 8px', textAlign: 'left', width: 32, fontSize: 9, color: C.textMuted }}>#</th>
                        {sdTh('werks',     'STORE',     { textAlign: 'left'  })}
                        {sdTh('mbq_qty',   'MBQ',       { textAlign: 'right' })}
                        {sdTh('store_stk', 'STORE_STK', { textAlign: 'right' })}
                        {sdTh('req_qty',   'REQ',       { textAlign: 'right' })}
                        {sdTh('alloc_qty', 'ALLOC',     { textAlign: 'right' })}
                        {sdTh('req_pct',   'REQ%',      { textAlign: 'right', width: 60 })}
                        {sdTh('fill_pct',  'FILL%',     { textAlign: 'right', width: 64 })}
                        {sdTh('req_rem',   'REQ_REM',   { textAlign: 'right' })}
                        {sdTh('hold_qty',  'HOLD',      { textAlign: 'right' })}
                        {sdTh('fnl_rem',   'FNL_Q_REM', { textAlign: 'right' })}
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((r, i) => {
                        const rp = reqPctRow(r), fp = fillPctRow(r), rr = reqRemRow(r), fq = fnlRow(r)
                        // Drill down to OPT-wise for this store within the
                        // current (MAJ_CAT, RDC) context.
                        const onRowClick = () =>
                          openOptModal(storeDrillMajCat, storeDrillRdc, r.werks)
                        return (
                          <tr key={r.werks} onClick={onRowClick}
                              title="Click for OPT-wise breakdown at this store"
                              style={{ borderTop: '1px solid #f1f5f9', cursor: 'pointer' }}>
                            <td style={{ padding: '5px 8px', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{i + 1}</td>
                            <td style={{ padding: '5px 8px', fontWeight: 600, color: C.primary, textDecoration: 'underline dotted' }}>{r.werks}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{(r.mbq_qty   || 0) > 0 ? r.mbq_qty.toLocaleString()   : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{(r.store_stk || 0) > 0 ? r.store_stk.toLocaleString() : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{(r.req_qty   || 0) > 0 ? r.req_qty.toLocaleString()   : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>{(r.alloc_qty || 0) > 0 ? r.alloc_qty.toLocaleString() : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: rp >= 90 ? '#10b981' : rp >= 60 ? '#f59e0b' : C.textMuted }}>{(r.req_qty || 0) > 0 ? `${rp.toFixed(0)}%` : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: fp >= 100 ? '#10b981' : fp >= 70 ? '#f59e0b' : C.textMuted }}>{(r.mbq_qty || 0) > 0 ? `${fp.toFixed(0)}%` : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{rr > 0 ? rr.toLocaleString() : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{(r.hold_qty || 0) > 0 ? r.hold_qty.toLocaleString() : dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fq > 0 ? fq.toLocaleString() : dash}</td>
                          </tr>
                        )
                      })}
                      <tr style={{ borderTop: '2px solid #cbd5e1', background: '#f8fafc', fontWeight: 700 }}>
                        <td/>
                        <td style={{ padding: '6px 8px', color: C.text }}>TOTAL</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{tMbq.toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{tStk.toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{tReq.toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{tAlloc.toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: tReqPct >= 90 ? '#10b981' : tReqPct >= 60 ? '#f59e0b' : C.textMuted }}>{tReq > 0 ? `${tReqPct.toFixed(0)}%` : '—'}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontSize: 10, color: tFillPct >= 100 ? '#10b981' : tFillPct >= 70 ? '#f59e0b' : C.textMuted }}>{tMbq > 0 ? `${tFillPct.toFixed(0)}%` : '—'}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{Math.max(0, tReq - tAlloc).toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{tHold.toLocaleString()}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{Math.max(0, tStk + tAlloc - tHold).toLocaleString()}</td>
                      </tr>
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
      })()}

      {/* ═══════════ Per-cell calc popover (Phase 1B) ═══════════
          Anchored near the clicked cell. metric determines which formula
          + values to render. STORE_STK additionally lazy-loads the SLOC
          inventory breakdown via /listing/sloc-breakdown. */}
      {calcPopover && (() => {
        const { metric, mc, rdc, cell, anchor, sloc, slocLoading, stkTtl } = calcPopover
        const reqPctV  = (cell.req_qty || 0) > 0 ? (cell.alloc_qty || 0) / cell.req_qty * 100 : 0
        const fillPctV = (cell.mbq_qty || 0) > 0 ? ((cell.store_stk || 0) + (cell.alloc_qty || 0)) / cell.mbq_qty * 100 : 0
        const msaRem   = Math.max(0, (cell.stock_avail || 0) - (cell.alloc_qty || 0) - (cell.hold_qty || 0))
        const reqRemV  = Math.max(0, (cell.req_qty || 0) - (cell.alloc_qty || 0))
        const convPct  = (cell.req_qty || 0) > 0 ? (cell.alloc_qty || 0) / cell.req_qty * 100 : 0

        // Position: prefer right-below the cell; clamp to viewport.
        const W = 360, H = 280
        const vw = typeof window !== 'undefined' ? window.innerWidth : 1280
        const vh = typeof window !== 'undefined' ? window.innerHeight : 800
        let x = (anchor?.x || 0) - W   // align right edge with cell right
        let y = (anchor?.y || 0) + 4
        if (x < 8) x = 8
        if (x + W > vw - 8) x = vw - W - 8
        if (y + H > vh - 8) y = (anchor?.y || 0) - H - 6
        if (y < 8) y = 8

        const Row = ({ label, val, accent }) => (
          <div style={{ display: 'flex', justifyContent: 'space-between',
                        padding: '3px 0', fontSize: 11, color: C.text }}>
            <span style={{ color: C.textSub }}>{label}</span>
            <span style={{ fontWeight: 600, color: accent || C.text,
                           fontVariantNumeric: 'tabular-nums' }}>{val}</span>
          </div>
        )
        const fmt = (n, d=0) =>
          (n === null || n === undefined || isNaN(n)) ? '—'
          : d === 0 ? Math.round(n).toLocaleString()
          : Number(n).toLocaleString(undefined, { maximumFractionDigits: d })

        const titles = {
          MBQ:       'MBQ — Minimum Buying Quantity',
          STOCK:     'STOCK — MSA Pool Stock',
          STORE_STK: 'STORE_STK — Store on-hand stock',
          EXCESS:    'EXCESS_STK — Surplus over 2× MBQ',
          REQ:       'REQ — Requirement',
          ALLOC:     'ALLOC — Allocation summary',
          REQ_REM:   'REQ_REM — Remaining requirement',
          MSA_REM:   'MSA_REM — Remaining MSA pool',
        }

        let body
        if (metric === 'MBQ') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              OPT_MBQ = round(ACS_D + rate × ALC_D, 0).<br/>
              rate = max(L-7 daily sale, AUTO_GEN_ART_SALE) when established;<br/>
              max(PER_OPT_SALE, L-7, AUTO_GEN_ART_SALE) when AGE &lt; threshold.
            </div>
            <Row label="MBQ (aggregate)" val={fmt(cell.mbq_qty)} accent={C.primary}/>
            <Row label="STORE_STK"       val={fmt(cell.store_stk)}/>
            <Row label="REQ = max(0, MBQ − STORE_STK)" val={fmt(cell.req_qty)}/>
          </>)
        } else if (metric === 'STOCK') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              MSA pool size for this RDC. Σ FNL_Q across (GEN_ART × CLR × VAR_ART × SZ).
              This is the supply pool stage C draws from.
            </div>
            <Row label="STOCK"        val={fmt(cell.stock_avail)} accent={C.primary}/>
            <Row label="ALLOC"        val={fmt(cell.alloc_qty)}/>
            <Row label="HOLD"         val={fmt(cell.hold_qty)}/>
            <Row label="MSA_REM = STOCK − ALLOC − HOLD" val={fmt(msaRem)}/>
            <Row label="STK% = ALLOC / STOCK"
                 val={cell.stock_avail > 0 ? `${(cell.alloc_qty/cell.stock_avail*100).toFixed(1)}%` : '—'}/>
          </>)
        } else if (metric === 'STORE_STK') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              Σ MJ_STK_TTL across stores. Breakdown by SLOC below — every
              SLOC column on ARS_LISTING is summed for this (MAJ_CAT{rdc ? `, ${rdc}` : ''}).
            </div>
            <Row label="STORE_STK (aggregate)" val={fmt(cell.store_stk)} accent={C.primary}/>
            <div style={{ borderTop: '1px solid #f1f5f9', marginTop: 6, paddingTop: 6,
                          maxHeight: 180, overflow: 'auto' }}>
              {slocLoading
                ? <div style={{ fontSize: 10, color: C.textMuted, padding: 4 }}>Loading SLOCs…</div>
                : (!sloc || sloc.length === 0)
                  ? <div style={{ fontSize: 10, color: C.textMuted, padding: 4 }}>No SLOC inventory.</div>
                  : (
                    <>
                      {sloc.map(s => (
                        <Row key={s.sloc} label={s.sloc} val={fmt(s.qty)}/>
                      ))}
                      {stkTtl !== undefined && (
                        <div style={{ borderTop: '1px dashed #e2e8f0', marginTop: 4, paddingTop: 4 }}>
                          <Row label="STK_TTL (Σ all SLOC)" val={fmt(stkTtl)} accent={C.primary}/>
                        </div>
                      )}
                    </>
                  )
              }
            </div>
          </>)
        } else if (metric === 'EXCESS') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              EXCESS_STK = max(0, STK_TTL − excess_multiplier × OPT_MBQ).<br/>
              Computed per-OPT in ARS_LISTING (Part 4d). MIX rows skipped.<br/>
              Default excess_multiplier = 2× (rows with stock above 2× their
              target are flagged excess and don't add to grid REQ).
            </div>
            <Row label="EXCESS_STK"  val={fmt(cell.excess_stk)} accent="#ea580c"/>
            <Row label="STORE_STK"   val={fmt(cell.store_stk)}/>
            <Row label="MBQ"         val={fmt(cell.mbq_qty)}/>
          </>)
        } else if (metric === 'REQ') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              REQ = max(0, MBQ − STORE_STK). Drives Stage C waterfall demand.
            </div>
            <Row label="MBQ"        val={fmt(cell.mbq_qty)}/>
            <Row label="STORE_STK"  val={fmt(cell.store_stk)}/>
            <Row label="REQ"        val={fmt(cell.req_qty)} accent={C.primary}/>
            <Row label="ALLOC"      val={fmt(cell.alloc_qty)}/>
            <Row label="REQ_REM"    val={fmt(reqRemV)}/>
          </>)
        } else if (metric === 'ALLOC') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              ALLOC = Σ SHIP_QTY from Stage C; HOLD = warehouse buffer.<br/>
              REQ% = ALLOC / REQ. FILL% = (STORE_STK + ALLOC) / MBQ.
            </div>
            <Row label="MBQ"         val={fmt(cell.mbq_qty)}/>
            <Row label="REQ"         val={fmt(cell.req_qty)}/>
            <Row label="REQ_REM"     val={fmt(reqRemV)}/>
            <Row label="ALLOC"       val={fmt(cell.alloc_qty)} accent={C.primary}/>
            <Row label="HOLD"        val={fmt(cell.hold_qty)}/>
            <Row label="REQ%"        val={cell.req_qty > 0 ? `${reqPctV.toFixed(1)}%` : '—'}
                 accent={reqPctV >= 90 ? '#10b981' : reqPctV >= 60 ? '#f59e0b' : C.text}/>
            <Row label="FILL%"       val={cell.mbq_qty > 0 ? `${fillPctV.toFixed(1)}%` : '—'}
                 accent={fillPctV >= 100 ? '#10b981' : fillPctV >= 70 ? '#f59e0b' : C.text}/>
          </>)
        } else if (metric === 'REQ_REM') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              REQ_REM = max(0, REQ − ALLOC). What's left unfilled.<br/>
              Conversion = ALLOC / REQ.
            </div>
            <Row label="MBQ"          val={fmt(cell.mbq_qty)}/>
            <Row label="REQ"          val={fmt(cell.req_qty)}/>
            <Row label="ALLOC"        val={fmt(cell.alloc_qty)}/>
            <Row label="REQ_REM"      val={fmt(reqRemV)} accent={C.primary}/>
            <Row label="Conversion %" val={cell.req_qty > 0 ? `${convPct.toFixed(1)}%` : '—'}
                 accent={convPct >= 90 ? '#10b981' : convPct >= 60 ? '#f59e0b' : C.text}/>
          </>)
        } else if (metric === 'MSA_REM') {
          body = (<>
            <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
              MSA_REM = max(0, STOCK − ALLOC − HOLD). What's left in the MSA
              pool at this RDC after the run. Was called FNL_Q_REM.
            </div>
            <Row label="STOCK (Int. WH)" val={fmt(cell.stock_avail)}/>
            <Row label="REQ"             val={fmt(cell.req_qty)}/>
            <Row label="ALLOC"           val={fmt(cell.alloc_qty)}/>
            <Row label="HOLD"            val={fmt(cell.hold_qty)}/>
            <Row label="MSA_REM"         val={fmt(msaRem)} accent={C.primary}/>
          </>)
        } else {
          body = <div style={{ fontSize: 11, color: C.textMuted }}>No details.</div>
        }

        return (
          <>
            <div onClick={() => setCalcPopover(null)}
                 style={{ position: 'fixed', inset: 0, background: 'transparent', zIndex: 1100 }}/>
            <div onClick={e => e.stopPropagation()}
                 style={{ position: 'fixed', left: x, top: y, width: W,
                          background: '#fff', border: '1px solid #e2e8f0',
                          borderRadius: 8, boxShadow: '0 10px 25px rgba(15,23,42,0.18)',
                          zIndex: 1101, padding: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between',
                            alignItems: 'center', marginBottom: 6 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
                  {titles[metric] || metric}
                </div>
                <button onClick={() => setCalcPopover(null)}
                        style={{ background: 'none', border: 'none', cursor: 'pointer',
                                 padding: 2, color: C.textSub }}><X size={12}/></button>
              </div>
              <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 8 }}>
                {mc}{rdc ? ` · ${rdc}` : ' · TOTAL'}
              </div>
              {body}
            </div>
          </>
        )
      })()}

      {/* ═══════════ OPT-wise drill modal (Phase 2) ═══════════
          Opens when the user clicks a MAJ_CAT name or aggregate row.
          Shows one row per (WERKS, GEN_ART_NUMBER, CLR) with OPT-grain
          MBQ/REQ/ALLOC/HOLD/MSA_REM + ALLOC_REMARKS audit trail.
          Click a row → opens VAR-art modal. */}
      {majCatModalOpen && drill?.view === 'opt' && (() => {
        const rows = [...(optModalData || [])]
        // Sort
        rows.sort((a, b) => {
          const av = a[optModalSortCol]
          const bv = b[optModalSortCol]
          const an = (av === null || av === undefined) ? -Infinity : av
          const bn = (bv === null || bv === undefined) ? -Infinity : bv
          if (typeof an === 'string' || typeof bn === 'string') {
            return optModalSortDir === 'asc'
              ? String(an).localeCompare(String(bn))
              : String(bn).localeCompare(String(an))
          }
          return optModalSortDir === 'asc' ? an - bn : bn - an
        })
        const tShip  = rows.reduce((s, r) => s + (r.ALLOC_QTY || 0), 0)
        const tHold  = rows.reduce((s, r) => s + (r.HOLD_QTY  || 0), 0)
        const tReq   = rows.reduce((s, r) => s + (r.OPT_REQ   || 0), 0)
        const tMbq   = rows.reduce((s, r) => s + (r.OPT_MBQ   || 0), 0)
        const tStk   = rows.reduce((s, r) => s + (r.STK_TTL   || 0), 0)
        const tEx    = rows.reduce((s, r) => s + (r.EXCESS_STK|| 0), 0)
        const tMsa   = rows.reduce((s, r) => s + (r.MSA_FNL_Q_REM || 0), 0)

        const th = (col, label, extraStyle={}) => (
          <th onClick={() => {
                if (optModalSortCol === col)
                  setOptModalSortDir(d => d === 'asc' ? 'desc' : 'asc')
                else { setOptModalSortCol(col); setOptModalSortDir('desc') }
              }}
              style={{ cursor: 'pointer', userSelect: 'none', padding: '4px 8px',
                       textAlign: 'right', fontSize: 9, color: C.textMuted,
                       fontWeight: 600, ...extraStyle }}>
            {label}{optModalSortCol === col ? (optModalSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
          </th>
        )
        const dash = <span style={{ color: '#d1d5db' }}>—</span>
        const fmt0 = v => (v === null || v === undefined || v === 0) ? dash : Math.round(v).toLocaleString()

        return (
          <div onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
            style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)',
                     display: 'flex', alignItems: 'stretch', justifyContent: 'stretch',
                     zIndex: 1000 }}>
            <div onClick={e => e.stopPropagation()}
              style={{ background: '#fff', width: '100vw', height: '100vh',
                       display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            padding: '12px 16px', borderBottom: '1px solid #e2e8f0' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button onClick={popDrill} title="Back to Stores"
                    style={{ display: 'flex', alignItems: 'center', gap: 4,
                             height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600,
                             background: '#f1f5f9', color: C.text,
                             border: '1px solid #e2e8f0', borderRadius: 6,
                             cursor: 'pointer' }}>
                    ← Back
                  </button>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                      MAJ_CATs <span style={{ color: C.textMuted }}>›</span> Stores <span style={{ color: C.textMuted }}>›</span> OPTs — {optModalMajCat}
                      {optModalRdc   ? ` · ${optModalRdc}`     : ' · ALL RDCs'}
                      {optModalWerks ? ` · STORE ${optModalWerks}` : ''}
                      {!optModalLoading && ` (${rows.length} OPTs)`}
                    </div>
                    <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                      MBQ {Math.round(tMbq).toLocaleString()} · STK_TTL {Math.round(tStk).toLocaleString()}
                      {tEx > 0  ? ` · EXCESS ${Math.round(tEx).toLocaleString()}`  : ''}
                      {' '}· REQ {Math.round(tReq).toLocaleString()} · ALLOC {Math.round(tShip).toLocaleString()}
                      {tHold > 0 ? ` · HOLD ${Math.round(tHold).toLocaleString()}` : ''}
                      {' '}· MSA_REM {Math.round(tMsa).toLocaleString()}
                      {' '}· Click any row for VAR_ART × SZ drill.
                    </div>
                  </div>
                </div>
                <button onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
                  title="Close all"
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer',
                           padding: 4, color: C.textSub }}><X size={16}/></button>
              </div>
              <div style={{ overflow: 'auto', padding: '4px 0' }}>
                {optModalLoading
                  ? <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>Loading…</div>
                  : rows.length === 0
                    ? <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>No OPTs for this filter.</div>
                    : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead style={{ position: 'sticky', top: 0, zIndex: 1, background: '#f8fafc' }}>
                      <tr style={{ borderBottom: '1px solid #e2e8f0' }}>
                        <th style={{ padding: '4px 8px', textAlign: 'left',  fontSize: 9, color: C.textMuted, fontWeight: 600 }}>#</th>
                        {th('WERKS',             'WERKS',     { textAlign: 'left' })}
                        {th('GEN_ART_NUMBER',    'GEN_ART',   { textAlign: 'left' })}
                        {th('CLR',               'CLR',       { textAlign: 'left' })}
                        {th('OPT_TYPE',          'OPT',       { textAlign: 'left' })}
                        {th('OPT_PRIORITY_RANK', 'RANK')}
                        {th('ACS_D',             'ACS_D')}
                        {th('OPT_MBQ',           'MBQ')}
                        {th('STK_TTL',           'STK_TTL')}
                        {th('EXCESS_STK',        'EXCESS')}
                        {th('OPT_REQ',           'REQ')}
                        {th('ALLOC_QTY',         'ALLOC')}
                        {th('HOLD_QTY',          'HOLD')}
                        {th('MSA_FNL_Q_REM',     'MSA_REM')}
                        {th('ALLOC_STATUS',      'STATUS',    { textAlign: 'left' })}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r, i) => {
                        const onRowClick = () => openVarDrill(
                          optModalMajCat, r.WERKS, r.GEN_ART_NUMBER, r.CLR || '',
                          optModalRdc || null, { ...r }
                        )
                        const reqV  = r.OPT_REQ   || 0
                        const reqRm = Math.max(0, reqV - (r.ALLOC_QTY || 0))
                        const statusColor =
                          r.ALLOC_STATUS === 'ALLOCATED' ? '#10b981'
                          : r.ALLOC_STATUS === 'PARTIAL'   ? '#f59e0b'
                          : r.ALLOC_STATUS === 'SKIPPED'   ? '#ef4444'
                          : C.textSub
                        return (
                          <tr key={i} onClick={onRowClick}
                              title="Click for VAR_ART × SZ drill"
                              style={{ borderTop: '1px solid #f1f5f9', cursor: 'pointer' }}>
                            <td style={{ padding: '5px 8px', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{i+1}</td>
                            <td style={{ padding: '5px 8px', color: C.text, fontWeight: 600 }}>{r.WERKS}</td>
                            <td style={{ padding: '5px 8px', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{r.GEN_ART_NUMBER}</td>
                            <td style={{ padding: '5px 8px', color: C.textSub }}>{r.CLR || dash}</td>
                            <td style={{ padding: '5px 8px', color: C.text }}>{r.OPT_TYPE || dash}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.OPT_PRIORITY_RANK)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.ACS_D)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.OPT_MBQ)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.STK_TTL)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: (r.EXCESS_STK || 0) > 0 ? '#ea580c' : C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.EXCESS_STK)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.OPT_REQ)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.ALLOC_QTY)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.HOLD_QTY)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.MSA_FNL_Q_REM)}</td>
                            <td style={{ padding: '5px 8px', color: statusColor, fontWeight: 600, fontSize: 10 }}>{r.ALLOC_STATUS || dash}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
      })()}

      {/* ═══════════ VAR_ART × SZ drill modal (Phase 2) ═══════════ */}
      {majCatModalOpen && drill?.view === 'var' && (() => {
        const ctx  = varModalCtx || {}
        const rows = [...(varModalData || [])]
        rows.sort((a, b) => {
          const av = a[varModalSortCol]
          const bv = b[varModalSortCol]
          const an = (av === null || av === undefined) ? -Infinity : av
          const bn = (bv === null || bv === undefined) ? -Infinity : bv
          if (typeof an === 'string' || typeof bn === 'string') {
            return varModalSortDir === 'asc'
              ? String(an).localeCompare(String(bn))
              : String(bn).localeCompare(String(an))
          }
          return varModalSortDir === 'asc' ? an - bn : bn - an
        })

        const th = (col, label, extraStyle={}) => (
          <th onClick={() => {
                if (varModalSortCol === col)
                  setVarModalSortDir(d => d === 'asc' ? 'desc' : 'asc')
                else { setVarModalSortCol(col); setVarModalSortDir('desc') }
              }}
              style={{ cursor: 'pointer', userSelect: 'none', padding: '4px 8px',
                       textAlign: 'right', fontSize: 9, color: C.textMuted,
                       fontWeight: 600, ...extraStyle }}>
            {label}{varModalSortCol === col ? (varModalSortDir === 'asc' ? ' ↑' : ' ↓') : ''}
          </th>
        )
        const dash = <span style={{ color: '#d1d5db' }}>—</span>
        const fmt0 = v => (v === null || v === undefined || v === 0) ? dash : Math.round(v).toLocaleString()

        const tShip = rows.reduce((s, r) => s + (r.ALLOC_QTY || r.SHIP_QTY || 0), 0)
        const tHold = rows.reduce((s, r) => s + (r.HOLD_QTY  || 0), 0)
        const tMbq  = rows.reduce((s, r) => s + (r.SZ_MBQ    || 0), 0)
        const tReq  = rows.reduce((s, r) => s + (r.SZ_REQ    || 0), 0)
        const tStk  = rows.reduce((s, r) => s + (r.SZ_STK    || 0), 0)
        const tFnl  = rows.reduce((s, r) => s + (r.FNL_Q     || 0), 0)
        const tRem  = rows.reduce((s, r) => s + (r.FNL_Q_REM || 0), 0)

        return (
          <div onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
            style={{ position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)',
                     display: 'flex', alignItems: 'stretch', justifyContent: 'stretch',
                     zIndex: 1000 }}>
            <div onClick={e => e.stopPropagation()}
              style={{ background: '#fff', width: '100vw', height: '100vh',
                       display: 'flex', flexDirection: 'column' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            padding: '12px 16px', borderBottom: '1px solid #e2e8f0' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <button onClick={popDrill} title="Back to OPTs"
                    style={{ display: 'flex', alignItems: 'center', gap: 4,
                             height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600,
                             background: '#f1f5f9', color: C.text,
                             border: '1px solid #e2e8f0', borderRadius: 6,
                             cursor: 'pointer' }}>
                    ← Back
                  </button>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                      MAJ_CATs <span style={{ color: C.textMuted }}>›</span> Stores <span style={{ color: C.textMuted }}>›</span> OPTs <span style={{ color: C.textMuted }}>›</span> VAR_ART × SZ — {ctx.werks} · {ctx.gen_art}
                      {ctx.clr ? ` · ${ctx.clr}` : ''}
                      {ctx.rdc ? ` · ${ctx.rdc}` : ''}
                      {!varModalLoading && ` (${rows.length} rows)`}
                    </div>
                    <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                      MBQ {Math.round(tMbq).toLocaleString()} · STK {Math.round(tStk).toLocaleString()}
                      {' '}· REQ {Math.round(tReq).toLocaleString()} · ALLOC {Math.round(tShip).toLocaleString()}
                      {tHold > 0 ? ` · HOLD ${Math.round(tHold).toLocaleString()}` : ''}
                      {' '}· FNL_Q {Math.round(tFnl).toLocaleString()} · MSA_REM {Math.round(tRem).toLocaleString()}
                    </div>
                  </div>
                </div>
                <button onClick={() => { setMajCatModalOpen(false); setDrill(null) }}
                  title="Close all"
                  style={{ background: 'transparent', border: 'none', cursor: 'pointer',
                           padding: 4, color: C.textSub }}><X size={16}/></button>
              </div>
              <div style={{ overflow: 'auto', padding: '4px 0' }}>
                {varModalLoading
                  ? <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>Loading…</div>
                  : rows.length === 0
                    ? <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>No allocation rows.</div>
                    : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead style={{ position: 'sticky', top: 0, zIndex: 1, background: '#f8fafc' }}>
                      <tr style={{ borderBottom: '1px solid #e2e8f0' }}>
                        <th style={{ padding: '4px 8px', textAlign: 'left', fontSize: 9, color: C.textMuted, fontWeight: 600 }}>#</th>
                        {th('VAR_ART',     'VAR_ART', { textAlign: 'left' })}
                        {th('SZ',          'SZ',      { textAlign: 'left' })}
                        {th('CONT',        'CONT')}
                        {th('PAK_SZ',      'PAK_SZ')}
                        {th('SZ_MBQ',      'SZ_MBQ')}
                        {th('SZ_STK',      'SZ_STK')}
                        {th('SZ_REQ',      'SZ_REQ')}
                        {th('FNL_Q',       'FNL_Q')}
                        {th('FNL_Q_REM',   'MSA_REM')}
                        {th('SHIP_QTY',    'SHIP')}
                        {th('FROM_HOLD_QTY','from_HOLD')}
                        {th('HOLD_QTY',    'HOLD')}
                        {th('ALLOC_QTY',   'ALLOC')}
                        {th('ALLOC_STATUS','STATUS',  { textAlign: 'left' })}
                        {th('SKIP_REASON', 'REASON',  { textAlign: 'left' })}
                        <th style={{ padding: '4px 8px', textAlign: 'left', fontSize: 9, color: C.textMuted, fontWeight: 600 }}>BAND TRACE</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r, i) => {
                        const statusColor =
                          r.ALLOC_STATUS === 'ALLOCATED' ? '#10b981'
                          : r.ALLOC_STATUS === 'PARTIAL'   ? '#f59e0b'
                          : r.ALLOC_STATUS === 'SKIPPED'   ? '#ef4444'
                          : C.textSub
                        return (
                          <tr key={i} style={{ borderTop: '1px solid #f1f5f9' }}>
                            <td style={{ padding: '5px 8px', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{i+1}</td>
                            <td style={{ padding: '5px 8px', color: C.text, fontVariantNumeric: 'tabular-nums' }}>{r.VAR_ART}</td>
                            <td style={{ padding: '5px 8px', color: C.text, fontWeight: 600 }}>{r.SZ}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>
                              {r.CONT != null ? Number(r.CONT).toFixed(3) : dash}
                            </td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.PAK_SZ)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.SZ_MBQ)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.SZ_STK)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.SZ_REQ)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.FNL_Q)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.FNL_Q_REM)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.SHIP_QTY)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: '#a16207', fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.FROM_HOLD_QTY)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', color: '#b45309', fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.HOLD_QTY)}</td>
                            <td style={{ padding: '5px 8px', textAlign: 'right', fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{fmt0(r.ALLOC_QTY)}</td>
                            <td style={{ padding: '5px 8px', color: statusColor, fontWeight: 600, fontSize: 10 }}>{r.ALLOC_STATUS || dash}</td>
                            <td style={{ padding: '5px 8px', color: C.textSub, fontSize: 10 }}>{r.SKIP_REASON || dash}</td>
                            <td style={{ padding: '5px 8px', color: C.textMuted, fontSize: 9, maxWidth: 360,
                                          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
                                title={r.ALLOC_REMARKS || ''}>
                              {r.ALLOC_REMARKS
                                ? <span style={{ fontFamily: 'monospace' }}>{r.ALLOC_REMARKS}</span>
                                : dash}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
      })()}

      {/* ═══════════ Store modal — per-store allocation breakdown ═══════════ */}
      {storeModalOpen && (() => {
        const items = [...(summary?.by_store || [])]
          .sort((a, b) => (b.alloc_qty || 0) - (a.alloc_qty || 0))
        // REQ/MBQ/ALLOC_QTY/HOLD_QTY are unit counts — round before display/export.
        const totalAlloc = Math.round(items.reduce((s, r) => s + (r.alloc_qty || 0), 0))
        const totalHold  = Math.round(items.reduce((s, r) => s + (r.hold_qty  || 0), 0))
        const totalReq   = Math.round(items.reduce((s, r) => s + (r.mj_req   || 0), 0))
        const hasReq = items.some(r => (r.mj_req || 0) > 0)

        const exportStoreExcel = () => {
          const data = items.map((r, i) => {
            const share  = totalAlloc > 0 ? parseFloat(((r.alloc_qty || 0) / totalAlloc * 100).toFixed(1)) : 0
            const reqPct = (r.mj_req || 0) > 0 ? parseFloat(((r.alloc_qty || 0) / r.mj_req * 100).toFixed(1)) : 0
            const row = {
              '#':         i + 1,
              STORE:       r.werks,
              ALLOC_QTY:   Math.round(r.alloc_qty || 0),
              HOLD_QTY:    Math.round(r.hold_qty  || 0),
            }
            if (hasReq) { row.REQ = Math.round(r.mj_req || 0); row['REQ%'] = reqPct }
            row.ROWS      = r.row_count || 0
            row['SHARE%'] = share
            return row
          })
          // Total row
          const tot = { '#': '', STORE: 'TOTAL', ALLOC_QTY: totalAlloc, HOLD_QTY: totalHold }
          if (hasReq) { tot.REQ = totalReq; tot['REQ%'] = totalReq > 0 ? parseFloat((totalAlloc / totalReq * 100).toFixed(1)) : 0 }
          tot.ROWS      = items.reduce((s, r) => s + (r.row_count || 0), 0)
          tot['SHARE%'] = 100
          data.push(tot)
          const ws = XLSX.utils.json_to_sheet(data)
          const wb = XLSX.utils.book_new()
          XLSX.utils.book_append_sheet(wb, ws, 'Store Allocation')
          XLSX.writeFile(wb, `store_allocation_${new Date().toISOString().slice(0,10)}.xlsx`)
        }

        return (
          <div onClick={() => setStoreModalOpen(false)}
            style={{
              position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              zIndex: 1000,
            }}>
            <div onClick={(e) => e.stopPropagation()}
              style={{
                background: '#fff', borderRadius: 10, width: hasReq ? 760 : 640, maxWidth: '94vw',
                maxHeight: '82vh', display: 'flex', flexDirection: 'column',
                boxShadow: '0 12px 40px rgba(0,0,0,0.18)',
              }}>
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '12px 16px', borderBottom: '1px solid #e2e8f0',
              }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                    Stores that received allocation ({items.length})
                  </div>
                  <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                    Sorted by alloc qty · ALLOC {totalAlloc.toLocaleString()}
                    {totalHold > 0 ? ` · HOLD ${totalHold.toLocaleString()}` : ''}
                    {hasReq && totalReq > 0 ? ` · REQ ${totalReq.toLocaleString()} · fill ${(totalAlloc / totalReq * 100).toFixed(1)}%` : ''}
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <button onClick={exportStoreExcel} title="Export to Excel"
                    style={{ display: 'flex', alignItems: 'center', gap: 4, height: 28, padding: '0 10px', fontSize: 11, fontWeight: 600, background: '#16a34a', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}>
                    <Download size={12}/> Excel
                  </button>
                  <button onClick={() => setStoreModalOpen(false)}
                    style={{ background: 'transparent', border: 'none', cursor: 'pointer', padding: 4, color: C.textSub }}>
                    <X size={16}/>
                  </button>
                </div>
              </div>
              <div style={{ overflow: 'auto', padding: '4px 0' }}>
                {items.length === 0 ? (
                  <div style={{ padding: 24, textAlign: 'center', fontSize: 11, color: C.textMuted }}>
                    No stores have received allocation yet.
                  </div>
                ) : (
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                    <thead style={{ position: 'sticky', top: 0, background: '#f8fafc' }}>
                      <tr style={{ color: C.textSub, fontSize: 9, letterSpacing: '.04em' }}>
                        <th style={{ padding: '6px 14px', textAlign: 'left', width: 36 }}>#</th>
                        <th style={{ padding: '6px 14px', textAlign: 'left' }}>STORE</th>
                        <th style={{ padding: '6px 14px', textAlign: 'right' }}>ALLOC QTY</th>
                        <th style={{ padding: '6px 14px', textAlign: 'right' }}>HOLD QTY</th>
                        {hasReq && <th style={{ padding: '6px 14px', textAlign: 'right' }}>REQ</th>}
                        {hasReq && <th style={{ padding: '6px 14px', textAlign: 'right', width: 70 }}>REQ %</th>}
                        <th style={{ padding: '6px 14px', textAlign: 'right' }}>ROWS</th>
                        <th style={{ padding: '6px 14px', textAlign: 'right', width: 60 }}>SHARE</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((r, i) => {
                        const share  = totalAlloc > 0 ? ((r.alloc_qty || 0) / totalAlloc * 100) : 0
                        const reqPct = (r.mj_req || 0) > 0 ? ((r.alloc_qty || 0) / r.mj_req * 100) : 0
                        return (
                          <tr key={r.werks} style={{ borderTop: '1px solid #f1f5f9' }}>
                            <td style={{ padding: '6px 14px', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{i + 1}</td>
                            <td style={{ padding: '6px 14px', fontWeight: 600, color: C.text }}>{r.werks}</td>
                            <td style={{ padding: '6px 14px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                              {Math.round(r.alloc_qty || 0).toLocaleString()}
                            </td>
                            <td style={{ padding: '6px 14px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>
                              {Math.round(r.hold_qty || 0).toLocaleString()}
                            </td>
                            {hasReq && (
                              <td style={{ padding: '6px 14px', textAlign: 'right', color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>
                                {Math.round(r.mj_req || 0).toLocaleString()}
                              </td>
                            )}
                            {hasReq && (
                              <td style={{ padding: '6px 14px', textAlign: 'right' }}>
                                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 5 }}>
                                  <div style={{ width: 36, height: 4, background: '#e2e8f0', borderRadius: 2, overflow: 'hidden' }}>
                                    <div style={{ width: `${Math.min(100, reqPct)}%`, height: '100%', background: reqPct >= 90 ? '#10b981' : reqPct >= 60 ? '#f59e0b' : C.primary, borderRadius: 2 }}/>
                                  </div>
                                  <span style={{ color: C.textMuted, fontVariantNumeric: 'tabular-nums', minWidth: 34, textAlign: 'right' }}>
                                    {reqPct.toFixed(1)}%
                                  </span>
                                </div>
                              </td>
                            )}
                            <td style={{ padding: '6px 14px', textAlign: 'right', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
                              {(r.rows || 0).toLocaleString()}
                            </td>
                            <td style={{ padding: '6px 14px', textAlign: 'right', color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
                              {share.toFixed(1)}%
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
