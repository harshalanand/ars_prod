import { useEffect, useMemo, useState } from 'react'
import { SlidersHorizontal, Search, History, AlertTriangle, X, Check, Plug, ChevronRight, Trash2, RefreshCw } from 'lucide-react'
import toast from 'react-hot-toast'
import { businessRulesAPI, upcTrackAPI } from '@/services/api'
import { C } from '@/theme/colors'

/*
 * Settings → Business Rules — module-wise behavior switches.
 * Design: module filter chips + searchable rule rows (toggle inline),
 * row click opens a detail drawer with tabs Rule | History, consequence
 * warning and a bounds-checked value editor. "Wiring pending" rules are
 * visible but read-only so the page never lies about what a toggle does.
 * Inactive always means: fall back to the hardcoded default behavior.
 */

function Toggle({ on, disabled, onClick, title }) {
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      style={{
        width: 38, height: 22, borderRadius: 11, border: 'none', padding: 0,
        cursor: disabled ? 'not-allowed' : 'pointer', position: 'relative',
        background: on ? C.green : C.inputBd, opacity: disabled ? 0.45 : 1,
        transition: 'background 0.15s', flexShrink: 0,
      }}>
      <span style={{
        width: 18, height: 18, borderRadius: '50%', background: '#fff',
        position: 'absolute', top: 2, left: on ? 18 : 2, transition: 'left 0.15s',
        boxShadow: '0 1px 2px rgba(0,0,0,0.2)',
      }} />
    </button>
  )
}

function Badge({ children, color = 'gray' }) {
  const map = {
    green: { bg: C.greenBg, bd: C.greenBd, fg: C.green },
    amber: { bg: C.amberBg, bd: C.amberBd, fg: C.amber },
    gray:  { bg: C.rowAlt, bd: C.cardBorder, fg: C.textMuted },
    blue:  { bg: C.primaryLt, bd: C.primaryBd, fg: C.primary },
  }
  const s = map[color] || map.gray
  return (
    <span style={{
      fontSize: 11, padding: '2px 8px', borderRadius: 10, whiteSpace: 'nowrap',
      background: s.bg, border: `1px solid ${s.bd}`, color: s.fg,
    }}>{children}</span>
  )
}

export default function BusinessRulesPage() {
  const [rules, setRules] = useState([])
  const [modules, setModules] = useState([])
  const [moduleFilter, setModuleFilter] = useState('All')
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(null)          // selected rule (drawer)
  const [tab, setTab] = useState('rule')        // 'rule' | 'history'
  const [history, setHistory] = useState([])
  const [editValue, setEditValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [upcResetting, setUpcResetting] = useState(false)

  // Module action attached to the UPC_TRACKING_ENABLED rule (moved here
  // from Settings -> Application, 2026-07-31).
  const resetUpcTracking = async () => {
    const msg = 'Reset UPC Store Tracking?\n\nThis permanently deletes ALL tracked stores and their ' +
      'date / remark / status history. The store master, grid and product view are NOT touched.\n\nContinue?'
    if (!window.confirm(msg)) return
    setUpcResetting(true)
    try {
      const { data } = await upcTrackAPI.reset()
      toast.success(data?.message || 'UPC Store Tracking cleared')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Reset failed')
    } finally {
      setUpcResetting(false)
    }
  }

  const load = async () => {
    setLoading(true)
    try {
      const res = await businessRulesAPI.list()
      const d = res.data?.data || {}
      setRules(d.items || [])
      setModules(d.modules || [])
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load business rules')
    } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const counts = useMemo(() => {
    const m = { All: rules.length }
    rules.forEach(r => { m[r.module] = (m[r.module] || 0) + 1 })
    return m
  }, [rules])

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return rules.filter(r =>
      (moduleFilter === 'All' || r.module === moduleFilter) &&
      (!needle || `${r.rule_key} ${r.rule_name} ${r.description}`.toLowerCase().includes(needle)))
  }, [rules, moduleFilter, q])

  const openRule = async (r) => {
    setSel(r); setTab('rule'); setEditValue(r.rule_value ?? ''); setHistory([])
    try {
      const res = await businessRulesAPI.history(r.rule_key, 50)
      setHistory(res.data?.data?.items || [])
    } catch { /* history is best-effort */ }
  }

  const applyUpdate = async (r, body, okMsg) => {
    setSaving(true)
    try {
      await businessRulesAPI.update(r.rule_key, body)
      toast.success(okMsg)
      await load()
      if (sel?.rule_key === r.rule_key) {
        const res = await businessRulesAPI.history(r.rule_key, 50)
        setHistory(res.data?.data?.items || [])
        setSel(s => s ? { ...s, ...body, rule_value: body.value ?? s.rule_value,
                          is_active: body.is_active ?? s.is_active } : s)
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Update failed')
    } finally { setSaving(false) }
  }

  const toggleRule = (r) => {
    if (!r.is_wired) return
    const goingOff = r.is_active
    if (goingOff && r.consequence &&
        !window.confirm(`Turn OFF "${r.rule_name}"?\n\n${r.consequence}\n\nApplies from the next run.`)) return
    applyUpdate(r, { is_active: !r.is_active },
      `${r.rule_name} ${goingOff ? 'deactivated' : 'activated'} — applies from the next run`)
  }

  const saveValue = () => {
    if (!sel) return
    applyUpdate(sel, { value: String(editValue) }, `${sel.rule_name} value saved`)
  }

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <SlidersHorizontal size={22} color={C.primary} />
          <div>
            <h1 style={{ fontSize: 20, fontWeight: 700, color: C.text, margin: 0 }}>Business rules</h1>
            <p style={{ fontSize: 13, color: C.textSub, margin: '2px 0 0' }}>
              Module-wise switches and values that control run behavior — inactive always means the built-in default
            </p>
          </div>
        </div>
        <div style={{ position: 'relative' }}>
          <Search size={14} color={C.textMuted} style={{ position: 'absolute', left: 10, top: 10 }} />
          <input value={q} onChange={e => setQ(e.target.value)} placeholder={`Search ${rules.length} rules`}
            style={{ padding: '7px 10px 7px 30px', fontSize: 13, border: `1px solid ${C.inputBd}`,
                     borderRadius: 8, background: C.inputBg, color: C.text, width: 200 }} />
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 14, flexWrap: 'wrap' }}>
        {['All', ...modules].map(m => (
          <button key={m} onClick={() => setModuleFilter(m)}
            style={{
              fontSize: 12.5, padding: '6px 14px', borderRadius: 16, cursor: 'pointer',
              border: `1px solid ${moduleFilter === m ? C.primary : C.cardBorder}`,
              background: moduleFilter === m ? C.primary : C.card,
              color: moduleFilter === m ? '#fff' : C.textSub,
            }}>
            {m} · {counts[m] || 0}
          </button>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: sel ? 'minmax(0,1.2fr) minmax(0,1fr)' : '1fr', gap: 14, alignItems: 'start' }}>

        <div style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
          {loading && <div style={{ padding: 20, fontSize: 13, color: C.textMuted }}>Loading…</div>}
          {!loading && visible.length === 0 &&
            <div style={{ padding: 20, fontSize: 13, color: C.textMuted }}>No rules match</div>}
          {visible.map((r, i) => (
            <div key={r.rule_key} onClick={() => openRule(r)}
              style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', cursor: 'pointer',
                borderBottom: i < visible.length - 1 ? `1px solid ${C.cardBorder}` : 'none',
                background: sel?.rule_key === r.rule_key ? C.primaryLt : 'transparent',
              }}>
              <span onClick={e => { e.stopPropagation(); toggleRule(r) }}>
                <Toggle on={r.is_active} disabled={!r.is_wired}
                  title={r.is_wired ? (r.is_active ? 'Deactivate' : 'Activate') : 'Wiring pending — read-only'} />
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, color: r.is_active ? C.text : C.textMuted }}>
                  {r.rule_name}
                </div>
                <div style={{ fontSize: 11.5, color: C.textMuted, fontFamily: 'monospace' }}>
                  {r.rule_key}{r.value_type !== 'flag' && r.rule_value != null && ` = ${r.rule_value}`}
                </div>
              </div>
              <Badge color="blue">{r.module}</Badge>
              {!r.is_wired && <Badge color="amber"><Plug size={10} style={{ verticalAlign: -1 }} /> wiring pending</Badge>}
              <Badge color={r.is_active ? 'green' : 'gray'}>{r.is_active ? 'Active' : 'Inactive'}</Badge>
              <ChevronRight size={15} color={C.textMuted} />
            </div>
          ))}
        </div>

        {sel && (
          <div style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 18, position: 'sticky', top: 16 }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 10 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 15, fontWeight: 700, color: C.text }}>{sel.rule_name}</div>
                <div style={{ fontSize: 11.5, color: C.textMuted, fontFamily: 'monospace', marginTop: 2 }}>
                  {sel.rule_key} · {sel.module}
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Toggle on={sel.is_active} disabled={!sel.is_wired || saving} onClick={() => toggleRule(sel)} />
                <button onClick={() => setSel(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 2 }}>
                  <X size={16} color={C.textMuted} />
                </button>
              </div>
            </div>

            <div style={{ display: 'flex', gap: 4, margin: '14px 0', borderBottom: `1px solid ${C.cardBorder}` }}>
              {[['rule', 'Rule'], ['algo', 'Algorithm'], ['history', `Change history (${history.length})`]].map(([k, label]) => (
                <button key={k} onClick={() => setTab(k)}
                  style={{
                    fontSize: 13, padding: '7px 12px', cursor: 'pointer', background: 'none',
                    border: 'none', borderBottom: `2px solid ${tab === k ? C.primary : 'transparent'}`,
                    color: tab === k ? C.primary : C.textSub, fontWeight: tab === k ? 600 : 400,
                  }}>{label}</button>
              ))}
            </div>

            {tab === 'rule' && (
              <div>
                <p style={{ fontSize: 13, color: C.textSub, lineHeight: 1.6, margin: '0 0 12px' }}>{sel.description}</p>

                {sel.consequence && (
                  <div style={{
                    display: 'flex', gap: 8, alignItems: 'flex-start', padding: '9px 12px',
                    background: C.amberBg, border: `1px solid ${C.amberBd}`, borderRadius: 8, marginBottom: 14,
                  }}>
                    <AlertTriangle size={15} color={C.amber} style={{ flexShrink: 0, marginTop: 1 }} />
                    <p style={{ fontSize: 12.5, color: C.amber, margin: 0, lineHeight: 1.5 }}>
                      <b>If turned off:</b> {sel.consequence}
                    </p>
                  </div>
                )}

                {sel.value_type !== 'flag' && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
                    <span style={{ fontSize: 13, color: C.textSub }}>Value</span>
                    {sel.value_type === 'choice' ? (
                      <select value={editValue} onChange={e => setEditValue(e.target.value)}
                        disabled={!sel.is_wired || saving}
                        style={{ fontSize: 13, padding: '6px 10px', border: `1px solid ${C.inputBd}`,
                                 borderRadius: 8, background: C.inputBg, color: C.text }}>
                        {(sel.choices || '').split(',').map(c => <option key={c} value={c.trim()}>{c.trim()}</option>)}
                      </select>
                    ) : (
                      <input value={editValue} onChange={e => setEditValue(e.target.value)}
                        disabled={!sel.is_wired || saving} type="number"
                        min={sel.value_min ?? undefined} max={sel.value_max ?? undefined} step="any"
                        style={{ width: 90, fontSize: 13, padding: '6px 10px', textAlign: 'center',
                                 border: `1px solid ${C.inputBd}`, borderRadius: 8, background: C.inputBg, color: C.text }} />
                    )}
                    {sel.value_min != null && sel.value_max != null && (
                      <span style={{ fontSize: 11.5, color: C.textMuted }}>allowed {sel.value_min}–{sel.value_max}</span>
                    )}
                    <button onClick={saveValue} disabled={!sel.is_wired || saving || String(editValue) === String(sel.rule_value ?? '')}
                      style={{
                        fontSize: 12.5, padding: '6px 14px', borderRadius: 8, cursor: 'pointer',
                        border: 'none', background: C.primary, color: '#fff',
                        opacity: (!sel.is_wired || saving || String(editValue) === String(sel.rule_value ?? '')) ? 0.5 : 1,
                      }}>
                      <Check size={12} style={{ verticalAlign: -1 }} /> Save
                    </button>
                  </div>
                )}

                {!sel.is_wired && (
                  <div style={{ fontSize: 12.5, color: C.textMuted, background: C.rowAlt, padding: '8px 12px', borderRadius: 8 }}>
                    <Plug size={12} style={{ verticalAlign: -1 }} /> Registered but not yet wired to code —
                    the toggle is read-only until the consuming code reads this rule.
                  </div>
                )}

                {sel.rule_key === 'UPC_TRACKING_ENABLED' && (
                  <div style={{ marginTop: 14, padding: '12px 14px', background: C.redBg,
                                border: `1px solid ${C.redBd}`, borderRadius: 8 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: C.red, display: 'flex', alignItems: 'center', gap: 6 }}>
                      <AlertTriangle size={14} /> Module action — clear all data & reset
                    </div>
                    <p style={{ fontSize: 12, color: C.textSub, margin: '4px 0 10px', lineHeight: 1.5 }}>
                      Permanently deletes all UPC Store Tracking data — tracked stores plus their date,
                      remark and status history (4 module tables) — and resets identity counters to 0.
                      Store master, grid and product view are not touched. Superadmin only.
                    </p>
                    <button onClick={resetUpcTracking} disabled={upcResetting}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5,
                               padding: '7px 14px', borderRadius: 8, cursor: 'pointer', border: 'none',
                               background: C.red, color: '#fff', opacity: upcResetting ? 0.6 : 1 }}>
                      {upcResetting ? <RefreshCw size={13} className="animate-spin" /> : <Trash2 size={13} />}
                      {upcResetting ? 'Clearing…' : 'Clear all data & reset from 0'}
                    </button>
                  </div>
                )}

                <p style={{ fontSize: 11.5, color: C.textMuted, margin: '14px 0 0' }}>
                  Last change: {sel.updated_by || '—'} · {sel.updated_at || '—'} · changes apply from the next run
                </p>
              </div>
            )}

            {tab === 'algo' && (
              <div>
                <pre style={{
                  fontSize: 12, lineHeight: 1.6, color: C.text, whiteSpace: 'pre-wrap',
                  background: C.rowAlt, border: `1px solid ${C.cardBorder}`,
                  borderRadius: 8, padding: '12px 14px', margin: 0,
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                }}>
                  {sel.algorithm || 'No algorithm documented for this rule yet.'}
                </pre>
                <p style={{ fontSize: 11.5, color: C.textMuted, margin: '10px 0 0' }}>
                  Plain-English walk of what the rule does at run time, with a worked example.
                </p>
              </div>
            )}

            {tab === 'history' && (
              <div style={{ borderLeft: `2px solid ${C.cardBorder}`, paddingLeft: 14 }}>
                {history.length === 0 &&
                  <p style={{ fontSize: 13, color: C.textMuted }}>No changes recorded yet</p>}
                {history.map((h, i) => (
                  <div key={i} style={{ marginBottom: 10 }}>
                    <div style={{ fontSize: 12.5, color: C.text }}>
                      {h.old_active !== h.new_active
                        ? <>{h.new_active ? 'Activated' : 'Deactivated'}</>
                        : <>Value {h.old_value ?? '—'} → <b>{h.new_value ?? '—'}</b></>}
                    </div>
                    <div style={{ fontSize: 11.5, color: C.textMuted }}>
                      <History size={10} style={{ verticalAlign: -1 }} /> {h.changed_by} · {h.changed_at}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
