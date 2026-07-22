/**
 * DevSyncManagerPage — one-way table refresh PROD (HOPC866) → DEV (this app's DB).
 *
 * Superadmin tool. Configure the prod source + linked server, activate/deactivate
 * the tables to sync, review tables newly discovered in prod, and run
 * incremental (daily) or full (weekly) refreshes. Prod is only ever READ.
 * See docs/DEV_SYNC_PLAN.md.
 */
import { useState, useEffect, useCallback } from 'react'
import { devSyncAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  RefreshCw, Play, Database, Link2, PlugZap, Search, Trash2,
  CheckCircle2, XCircle, AlertTriangle, Server, Download, Plus,
} from 'lucide-react'
import { C } from '@/theme/colors'

const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString('en-IN'))
const fmtDate = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${String(d.getDate()).padStart(2,'0')} ${d.toLocaleString('en',{month:'short'})}, ` +
         `${d.getHours()%12||12}:${String(d.getMinutes()).padStart(2,'0')}${d.getHours()>=12?'p':'a'}`
}

const Btn = ({ icon:Icon, label, onClick, disabled, tone='primary', small }) => {
  const tones = {
    primary: { bg:C.primary, fg:'#fff', bd:C.primary },
    ghost:   { bg:'#fff', fg:C.textSub, bd:C.cardBorder },
    green:   { bg:C.green, fg:'#fff', bd:C.green },
    danger:  { bg:'#fff', fg:C.red, bd:C.redBd },
  }[tone]
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display:'inline-flex', alignItems:'center', gap:6, cursor:disabled?'not-allowed':'pointer',
      padding: small ? '4px 8px' : '7px 12px', fontSize: small?12:13, fontWeight:600,
      borderRadius:6, border:`1px solid ${tones.bd}`, background:tones.bg, color:tones.fg,
      opacity:disabled?0.55:1,
    }}>{Icon && <Icon size={small?12:14}/>}{label}</button>
  )
}

const Pill = ({ children, tone }) => {
  const t = {
    ok:    { bg:C.greenBg, fg:C.green, bd:C.greenBd },
    error: { bg:C.redBg, fg:C.red, bd:C.redBd },
    skip:  { bg:C.blueBg, fg:C.blue, bd:C.blueBd },
    differ:{ bg:C.amberBg, fg:C.amber, bd:C.amberBd },
    match: { bg:C.greenBg, fg:C.green, bd:C.greenBd },
    idle:  { bg:C.grayBg, fg:C.textMuted, bd:C.grayBd },
    incr:  { bg:C.blueBg, fg:C.blue, bd:C.blueBd },
    full:  { bg:C.amberBg, fg:C.amber, bd:C.amberBd },
  }[tone] || { bg:C.grayBg, fg:C.gray, bd:C.grayBd }
  return <span style={{ fontSize:11, fontWeight:700, padding:'2px 7px', borderRadius:20,
    background:t.bg, color:t.fg, border:`1px solid ${t.bd}` }}>{children}</span>
}

const CAT_TONE = {
  input:'blue', reference:'indigo', config:'green',
  output:'amber', history:'gray', identity:'gray', other:'gray',
}
const CatChip = ({ cat }) => {
  const map = {
    blue:  { bg:C.blueBg,   fg:C.blue,   bd:C.blueBd },
    indigo:{ bg:C.indigoBg, fg:C.indigo, bd:C.indigoBd },
    green: { bg:C.greenBg,  fg:C.green,  bd:C.greenBd },
    amber: { bg:C.amberBg,  fg:C.amber,  bd:C.amberBd },
    gray:  { bg:C.grayBg,   fg:C.gray,   bd:C.grayBd },
  }
  const t = map[CAT_TONE[cat] || 'gray']
  return <span style={{ fontSize:10.5, fontWeight:700, padding:'2px 6px', borderRadius:4,
    background:t.bg, color:t.fg, border:`1px solid ${t.bd}` }}>{cat||'other'}</span>
}

export default function DevSyncManagerPage() {
  const [settings, setSettings] = useState(null)
  const [tables, setTables]     = useState([])
  const [discovery, setDiscovery] = useState(null)
  const [pwd, setPwd]           = useState('')
  const [tpwd, setTpwd]         = useState('')
  const [busy, setBusy]         = useState('')
  const [filter, setFilter]     = useState('')
  const [catFilter, setCatFilter] = useState('')
  const [sel, setSel]           = useState(new Set())
  const [force, setForce]       = useState(false)

  const loadAll = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([devSyncAPI.getSettings(), devSyncAPI.getTables()])
      setSettings(s.data.data || {})
      setTables(t.data.data.items || [])
    } catch (e) { toast.error(e.response?.data?.detail || 'Load failed') }
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const patchSetting = (k, v) => setSettings(s => ({ ...s, [k]: v }))

  const saveSettings = async () => {
    setBusy('save')
    try {
      const body = { ...settings }
      if (pwd)  body.source_pwd = pwd
      if (tpwd) body.target_pwd = tpwd
      const r = await devSyncAPI.saveSettings(body)
      setSettings(r.data.data); setPwd(''); setTpwd('')
      toast.success('Settings saved')
      return true
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed'); return false }
    finally { setBusy('') }
  }

  const testConn = async (which) => {
    setBusy('test'+which)
    try {
      const r = await devSyncAPI.testConnection(which === 'source'
        ? { which, server: settings.source_server, user: settings.source_user, pwd: pwd || undefined }
        : { which, server: settings.target_server, user: settings.target_user, pwd: tpwd || undefined })
      toast.success(r.data.message)
    } catch (e) { toast.error(e.response?.data?.detail || 'Test failed') }
    finally { setBusy('') }
  }

  const setupLink = async () => {
    setBusy('link')
    try {
      const ok = await saveSettings()
      if (!ok) return
      const r = await devSyncAPI.setupLinkServer()
      toast.success(r.data.message)
    } catch (e) { toast.error(e.response?.data?.detail || 'Linked-server setup failed') }
    finally { setBusy('') }
  }

  const runDiscover = async () => {
    setBusy('discover')
    try {
      const r = await devSyncAPI.discover()
      setDiscovery(r.data.data)
      toast.success(r.data.message)
    } catch (e) { toast.error(e.response?.data?.detail || 'Discovery failed') }
    finally { setBusy('') }
  }

  const addDiscovered = async (items) => {
    try {
      const r = await devSyncAPI.addTables(items)
      toast.success(`Added ${r.data.data.added} table(s)`)
      setDiscovery(null); loadAll()
    } catch (e) { toast.error(e.response?.data?.detail || 'Add failed') }
  }

  const toggleActive = async (row) => {
    try {
      await devSyncAPI.updateTable(row.id, { is_active: !row.is_active })
      setTables(ts => ts.map(t => t.id === row.id ? { ...t, is_active: !t.is_active } : t))
    } catch (e) { toast.error('Update failed') }
  }

  const changeMode = async (row, mode) => {
    try {
      await devSyncAPI.updateTable(row.id, { sync_mode: mode })
      setTables(ts => ts.map(t => t.id === row.id ? { ...t, sync_mode: mode } : t))
    } catch (e) { toast.error('Update failed') }
  }

  const removeRow = async (row) => {
    if (!confirm(`Remove ${row.db_name}.${row.table_name} from sync config?`)) return
    try { await devSyncAPI.deleteTable(row.id); loadAll() }
    catch (e) { toast.error('Delete failed') }
  }

  const bulk = async (active) => {
    const ids = [...sel]
    if (!ids.length) return toast.error('Select tables first')
    try { await devSyncAPI.bulkToggle(ids, active); setSel(new Set()); loadAll() }
    catch (e) { toast.error('Bulk update failed') }
  }

  const bulkDelete = async () => {
    const ids = [...sel]
    if (!ids.length) return toast.error('Select tables first')
    if (!confirm(`Remove ${ids.length} selected table(s) from the sync list? (No data is deleted.)`)) return
    try { const r = await devSyncAPI.bulkDelete(ids); toast.success(r.data.message); setSel(new Set()); loadAll() }
    catch (e) { toast.error('Delete failed') }
  }

  const clearAll = async () => {
    if (!tables.length) return toast.error('List is already empty')
    if (!confirm(`Remove ALL ${tables.length} table(s) from the sync list?\nThis only clears the list — no table data is deleted. You can re-add selectively via Discover.`)) return
    try { const r = await devSyncAPI.clearAll(); toast.success(r.data.message); setSel(new Set()); loadAll() }
    catch (e) { toast.error('Clear failed') }
  }

  const run = async (mode) => {
    const active = tables.filter(t => t.is_active)
    if (!active.length) return toast.error('No active tables to sync')
    const forceNote = force ? ' (FORCE — reload even if identical)' : ' (skips identical)'
    if (!confirm(`Run ${mode.toUpperCase()} sync for ${active.length} active table(s)?${forceNote}`)) return
    setBusy('run')
    try {
      const r = await devSyncAPI.run(mode, null, force)
      const d = r.data.data
      toast[d.errors ? 'error' : 'success'](
        `${mode}: ${d.ok} updated, ${d.skipped||0} skipped, ${d.errors} error(s) ` +
        `(${d.source_server} → ${d.target_server})`)
      loadAll()
    } catch (e) { toast.error(e.response?.data?.detail || 'Sync failed') }
    finally { setBusy('') }
  }

  if (!settings) return <div style={{ padding:24, color:C.textMuted }}>Loading…</div>

  const shown = tables.filter(t =>
    (!filter || `${t.db_name}.${t.table_name}`.toLowerCase().includes(filter.toLowerCase())) &&
    (!catFilter || (t.category||'other') === catFilter))
  const activeCount = tables.filter(t => t.is_active).length
  const cats = [...new Set(tables.map(t => t.category||'other'))].sort()

  return (
    <div style={{ padding:20, background:C.pageBg, minHeight:'100%' }}>
      {/* Header */}
      <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:16 }}>
        <Database size={22} color={C.primary}/>
        <div>
          <h1 style={{ margin:0, fontSize:20, fontWeight:800, color:C.text }}>Dev Sync Manager</h1>
          <div style={{ fontSize:12, color:C.textMuted }}>
            One-way refresh · PROD (HOPC866) → DEV · prod is read-only
          </div>
        </div>
      </div>

      {/* Connection card */}
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:10, padding:16, marginBottom:16 }}>
        {settings.source_server && settings.target_server &&
         settings.source_server.toLowerCase() === settings.target_server.toLowerCase() && (
          <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:12, padding:'8px 10px',
            background:C.redBg, border:`1px solid ${C.redBd}`, borderRadius:6, color:C.red, fontSize:12.5, fontWeight:600 }}>
            <AlertTriangle size={15}/> Source and target are the same server — set the target to your DEV server (arsdbpro). Prod must never be the write target.
          </div>
        )}
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:18 }}>
          {/* SOURCE (prod, read) */}
          <div>
            <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10, fontWeight:700, color:C.text }}>
              <Server size={16} color={C.gray}/> Source · PROD (read-only)
            </div>
            <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:10 }}>
              <Field label="Server"><input value={settings.source_server||''} onChange={e=>patchSetting('source_server', e.target.value)} style={inp}/></Field>
              <Field label="Username"><input value={settings.source_user||''} onChange={e=>patchSetting('source_user', e.target.value)} style={inp}/></Field>
              <Field label={`Password ${settings.source_pwd_set?'(saved)':''}`}>
                <input type="password" value={pwd} onChange={e=>setPwd(e.target.value)}
                  placeholder={settings.source_pwd_set?'••••••':'enter'} style={inp}/></Field>
              <Field label="Linked server name"><input value={settings.link_server_name||''} onChange={e=>patchSetting('link_server_name', e.target.value)} style={inp}/></Field>
            </div>
            <div style={{ marginTop:10 }}>
              <Btn small icon={PlugZap} label="Test source" tone="ghost" onClick={()=>testConn('source')} disabled={!!busy}/>
            </div>
          </div>
          {/* TARGET (dev, write) */}
          <div>
            <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10, fontWeight:700, color:C.primary }}>
              <Database size={16} color={C.primary}/> Target · DEV (write)
            </div>
            <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:10 }}>
              <Field label="Server"><input value={settings.target_server||''} onChange={e=>patchSetting('target_server', e.target.value)} style={inp}/></Field>
              <Field label="Username"><input value={settings.target_user||''} onChange={e=>patchSetting('target_user', e.target.value)} style={inp}/></Field>
              <Field label={`Password ${settings.target_pwd_set?'(saved)':''}`}>
                <input type="password" value={tpwd} onChange={e=>setTpwd(e.target.value)}
                  placeholder={settings.target_pwd_set?'••••••':'enter'} style={inp}/></Field>
              <Field label="Data DB"><input value={settings.target_data_db||''} onChange={e=>patchSetting('target_data_db', e.target.value)} style={inp}/></Field>
            </div>
            <div style={{ marginTop:10 }}>
              <Btn small icon={PlugZap} label="Test target" tone="ghost" onClick={()=>testConn('target')} disabled={!!busy}/>
            </div>
          </div>
        </div>
        <div style={{ display:'flex', gap:8, marginTop:14, alignItems:'center' }}>
          <Btn icon={Link2} label="Save & setup linked server" onClick={setupLink} disabled={!!busy}/>
          <Btn icon={CheckCircle2} label="Save settings" tone="ghost" onClick={saveSettings} disabled={!!busy}/>
          <div style={{ flex:1 }}/>
          <span style={{ fontSize:12, color:C.textMuted }}>
            Last incremental: {fmtDate(settings.last_incr_at)} · Last full: {fmtDate(settings.last_full_at)}
          </span>
        </div>
      </div>

      {/* Discovery errors — explains 'unable to discover' */}
      {discovery && discovery.errors?.length > 0 && (
        <div style={{ background:C.redBg, border:`1px solid ${C.redBd}`, borderRadius:10, padding:12, marginBottom:12 }}>
          <div style={{ display:'flex', alignItems:'center', gap:8, fontWeight:700, color:C.red, marginBottom:4 }}>
            <XCircle size={16}/> Discovery reported problems
          </div>
          {discovery.errors.map((e,i)=>(<div key={i} style={{ fontSize:12, color:C.red }}>• {e}</div>))}
          <div style={{ fontSize:12, color:C.textSub, marginTop:6 }}>
            Check the source server / username / password above and click <b>Save & setup linked server</b> (or just Save), then Discover again.
          </div>
        </div>
      )}

      {/* Comparison panel — prod vs dev for every source table */}
      {discovery && (
        <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:10, padding:14, marginBottom:16 }}>
          <div style={{ display:'flex', alignItems:'center', gap:12, marginBottom:10, flexWrap:'wrap' }}>
            <span style={{ fontWeight:700, color:C.text }}>Prod → Dev comparison</span>
            {discovery.summary && (
              <span style={{ display:'flex', gap:6 }}>
                <Pill tone="match">{discovery.summary.matched} identical</Pill>
                <Pill tone="differ">{discovery.summary.differ} differ</Pill>
                <Pill tone="idle">{discovery.summary.new} new</Pill>
                {discovery.summary.missing>0 && <Pill tone="error">{discovery.summary.missing} missing on dev</Pill>}
              </span>
            )}
            <div style={{ flex:1 }}/>
            <Btn small icon={Plus} label="Add recommended (input+ref+config)" tone="green"
              onClick={()=>addDiscovered(discovery.new.filter(n=>n.recommended)
                .map(n=>({db_name:n.db_name,table_name:n.table_name})))}/>
            <Btn small icon={Plus} label="Add all new (inactive)" tone="ghost"
              onClick={()=>addDiscovered(discovery.new.map(n=>({db_name:n.db_name,table_name:n.table_name})))}/>
          </div>
          {discovery.summary?.by_category && (
            <div style={{ display:'flex', flexWrap:'wrap', gap:10, marginBottom:10, fontSize:11.5, color:C.textSub }}>
              {Object.entries(discovery.summary.by_category).filter(([,n])=>n>0).map(([c,n])=>(
                <span key={c} style={{ display:'inline-flex', alignItems:'center', gap:4 }}>
                  <CatChip cat={c}/> {n}
                </span>
              ))}
            </div>
          )}
          <div style={{ maxHeight:320, overflow:'auto', border:`1px solid ${C.grayBd}`, borderRadius:8 }}>
            <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12.5 }}>
              <thead>
                <tr style={{ background:C.headerBg, color:C.textSub, textAlign:'left', position:'sticky', top:0 }}>
                  <th style={th}>DB</th><th style={th}>Table</th><th style={th}>Category</th>
                  <th style={{...th, textAlign:'right'}}>Prod rows</th>
                  <th style={{...th, textAlign:'right'}}>Dev rows</th>
                  <th style={{...th, textAlign:'right'}}>Δ</th>
                  <th style={th}>State</th><th style={th}>Config</th>
                </tr>
              </thead>
              <tbody>
                {(discovery.compare||[]).map((r,i)=>{
                  const delta = r.on_dev ? (r.src_rows - (r.dev_rows||0)) : null
                  const state = !r.on_dev ? 'missing' : (r.match ? 'match' : 'differ')
                  return (
                    <tr key={i} style={{ borderTop:`1px solid ${C.grayBd}`, background:i%2?C.rowAlt:'#fff' }}>
                      <td style={{...td, color:C.textMuted}}>{r.db_name}</td>
                      <td style={{...td, fontWeight:600, color:C.text}}>
                        {r.table_name}{r.recommended && <span title="recommended to sync" style={{color:C.green}}> ★</span>}
                      </td>
                      <td style={td}><CatChip cat={r.category}/></td>
                      <td style={{...td, textAlign:'right'}}>{fmt(r.src_rows)}</td>
                      <td style={{...td, textAlign:'right'}}>{r.on_dev?fmt(r.dev_rows):'—'}</td>
                      <td style={{...td, textAlign:'right', color: delta ? C.amber : C.textMuted, fontWeight: delta?700:400}}>
                        {delta==null?'—':(delta>0?`+${fmt(delta)}`:fmt(delta))}
                      </td>
                      <td style={td}>
                        {state==='match'  && <Pill tone="match">identical</Pill>}
                        {state==='differ' && <Pill tone="differ">differ</Pill>}
                        {state==='missing'&& <Pill tone="error">not on dev</Pill>}
                      </td>
                      <td style={td}>
                        {r.in_config
                          ? <Pill tone={r.is_active?'ok':'idle'}>{r.is_active?'active':'configured'}</Pill>
                          : <button onClick={()=>addDiscovered([{db_name:r.db_name,table_name:r.table_name}])}
                              style={{ fontSize:11, cursor:'pointer', border:`1px solid ${C.cardBorder}`, background:'#fff', borderRadius:5, padding:'2px 8px', color:C.primary, fontWeight:600 }}>+ add</button>}
                      </td>
                    </tr>
                  )
                })}
                {(!discovery.compare || discovery.compare.length===0) && (
                  <tr><td colSpan={8} style={{ padding:18, textAlign:'center', color:C.textMuted }}>
                    No source tables read. See errors above.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div style={{ fontSize:12, color:C.textMuted, marginTop:8 }}>
            <b>identical</b> = same row count on prod &amp; dev → sync will <b>skip</b> it (no update needed).
            <b> differ</b> = counts differ → sync will refresh it.
          </div>
        </div>
      )}

      {/* Toolbar */}
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10 }}>
        <div style={{ position:'relative' }}>
          <Search size={14} color={C.textMuted} style={{ position:'absolute', left:8, top:9 }}/>
          <input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Filter tables…"
            style={{ ...inp, paddingLeft:28, width:220 }}/>
        </div>
        <select value={catFilter} onChange={e=>setCatFilter(e.target.value)} style={{ ...inp, width:150 }}>
          <option value="">All categories</option>
          {cats.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <Btn icon={Download} label="Discover prod tables" tone="ghost" onClick={runDiscover} disabled={!!busy}/>
        <Btn icon={RefreshCw} label="Refresh" tone="ghost" onClick={loadAll} disabled={!!busy}/>
        {tables.length > 0 && (
          <Btn icon={Trash2} label="Clear all" tone="danger" onClick={clearAll} disabled={!!busy}/>
        )}
        {sel.size > 0 && (<>
          <Btn small label={`Activate ${sel.size}`} tone="green" onClick={()=>bulk(true)}/>
          <Btn small label={`Deactivate ${sel.size}`} tone="ghost" onClick={()=>bulk(false)}/>
          <Btn small icon={Trash2} label={`Delete ${sel.size}`} tone="danger" onClick={bulkDelete}/>
        </>)}
        <div style={{ flex:1 }}/>
        <span style={{ fontSize:12, color:C.textSub, fontWeight:600 }}>{activeCount} active / {tables.length} configured</span>
        <label title="Reload even when prod & dev row counts already match" style={{ display:'inline-flex', alignItems:'center', gap:5, fontSize:12, color:C.textSub, cursor:'pointer' }}>
          <input type="checkbox" checked={force} onChange={e=>setForce(e.target.checked)}/> force
        </label>
        <Btn icon={Play} label="Run Incremental" tone="primary" onClick={()=>run('incremental')} disabled={!!busy}/>
        <Btn icon={Play} label="Run Full (weekly)" tone="green" onClick={()=>run('full')} disabled={!!busy}/>
      </div>

      {/* Tables grid */}
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:10, overflow:'hidden' }}>
        <table style={{ width:'100%', borderCollapse:'collapse', fontSize:13 }}>
          <thead>
            <tr style={{ background:C.headerBg, color:C.textSub, textAlign:'left' }}>
              <th style={th}><input type="checkbox"
                checked={sel.size>0 && sel.size===shown.length}
                onChange={e=>setSel(e.target.checked ? new Set(shown.map(t=>t.id)) : new Set())}/></th>
              <th style={th}>Active</th>
              <th style={th}>DB</th>
              <th style={th}>Table</th>
              <th style={th}>Category</th>
              <th style={th}>Mode</th>
              <th style={th}>Incr key</th>
              <th style={{...th, textAlign:'right'}}>Prod rows</th>
              <th style={{...th, textAlign:'right'}}>Dev rows</th>
              <th style={th}>Last sync</th>
              <th style={th}>Status</th>
              <th style={th}></th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 && (
              <tr><td colSpan={12} style={{ padding:24, textAlign:'center', color:C.textMuted }}>
                No tables configured. Click <b>Discover prod tables</b> to find syncable tables.
              </td></tr>
            )}
            {shown.map((t,i)=>(
              <tr key={t.id} style={{ borderTop:`1px solid ${C.grayBd}`, background:i%2?C.rowAlt:'#fff' }}>
                <td style={td}><input type="checkbox" checked={sel.has(t.id)}
                  onChange={e=>{ const n=new Set(sel); e.target.checked?n.add(t.id):n.delete(t.id); setSel(n) }}/></td>
                <td style={td}>
                  <label style={{ display:'inline-flex', cursor:'pointer' }}>
                    <input type="checkbox" checked={t.is_active} onChange={()=>toggleActive(t)}/>
                  </label>
                </td>
                <td style={{...td, color:C.textMuted}}>{t.db_name}</td>
                <td style={{...td, fontWeight:600, color:C.text}}>{t.table_name}</td>
                <td style={td}><CatChip cat={t.category}/></td>
                <td style={td}>
                  <select value={t.sync_mode} onChange={e=>changeMode(t, e.target.value)}
                    style={{ ...inp, padding:'3px 6px', fontSize:12 }}>
                    <option value="incremental">incremental</option>
                    <option value="full">full</option>
                  </select>
                </td>
                <td style={{...td, color:C.textSub, fontFamily:'monospace', fontSize:12}}>{t.incr_key_col||'—'}</td>
                <td style={{...td, textAlign:'right'}}>{fmt(t.src_rows)}</td>
                <td style={{...td, textAlign:'right'}}>{fmt(t.tgt_rows)}</td>
                <td style={{...td, color:C.textMuted, fontSize:12}}>{fmtDate(t.last_sync_at)}</td>
                <td style={td}>
                  {t.last_status==='ok'     && <Pill tone="ok">ok</Pill>}
                  {t.last_status==='skipped'&& <span title={t.last_message}><Pill tone="skip">skipped</Pill></span>}
                  {t.last_status==='error'  && <span title={t.last_message}><Pill tone="error">error</Pill></span>}
                  {!t.last_status           && <Pill tone="idle">—</Pill>}
                </td>
                <td style={td}>
                  <button onClick={()=>removeRow(t)} title="Remove" style={{ border:'none', background:'none', cursor:'pointer', color:C.red }}>
                    <Trash2 size={14}/>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop:12, fontSize:12, color:C.textMuted, lineHeight:1.6 }}>
        <b>Incremental</b> appends rows with key &gt; the max already on dev (cheap, daily).
        <b> Full</b> truncates &amp; reloads inside a transaction (reconciles updates/deletes, weekly).
        Each table syncs in its own transaction — a failure rolls back only that table.
        The run refuses if the source equals this server, so prod is never written.
      </div>
    </div>
  )
}

const Field = ({ label, children }) => (
  <label style={{ display:'flex', flexDirection:'column', gap:4 }}>
    <span style={{ fontSize:11, fontWeight:600, color:C.textSub }}>{label}</span>
    {children}
  </label>
)
const inp = { padding:'6px 9px', fontSize:13, border:`1px solid ${C.inputBd}`, borderRadius:6, background:C.inputBg, color:C.text, outline:'none', width:'100%' }
const th = { padding:'9px 10px', fontSize:11, fontWeight:700, textTransform:'uppercase', letterSpacing:0.3 }
const td = { padding:'8px 10px', verticalAlign:'middle' }
