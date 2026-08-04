import { useState, useEffect } from 'react'
import { Search, RefreshCw, Play, Database, Info, Compass, Columns, Boxes } from 'lucide-react'
import { sapAPI } from '@/services/api'
import toast from 'react-hot-toast'
import WhereBuilder from '@/components/sap/WhereBuilder'
import { cleanConds } from '@/utils/sapWhere'
import useSapUiStore from '@/store/sapUiStore'

const esc = (s) => String(s || '').replace(/'/g, "''")

// Selected fields as removable chips (shown per the global name/label/both mode)
// plus an add box with autocomplete suggestions. Backed by the comma list.
function FieldPicker({ fields, fieldLabels, displayMode, onToggle, suggestions = [] }) {
  const [q, setQ] = useState('')
  const chipText = (f) => {
    const lbl = fieldLabels[f]
    if (displayMode === 'label') return lbl || f
    if (displayMode === 'both') return `${f}${lbl ? ' · ' + lbl : ''}`
    return f
  }
  const add = () => {
    const parts = q.split(/[\s,;]+/).map(s => s.trim().toUpperCase()).filter(Boolean)
    parts.forEach(p => { if (!fields.some(f => f.toUpperCase() === p)) onToggle(p) })
    setQ('')
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5 min-h-[2.25rem] p-2 border rounded bg-white">
        {fields.length === 0 && <span className="text-xs text-gray-400">No fields selected — add below. Wide tables need specific fields.</span>}
        {fields.map(f => (
          <span key={f} className="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] font-mono rounded bg-primary-50 text-primary-700 border border-primary-200">
            {chipText(f)}
            <button type="button" onClick={() => onToggle(f)} className="hover:text-red-600 font-bold leading-none" title="Remove">×</button>
          </span>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <input className="input flex-1 font-mono text-xs" list="explorer-fieldpick-list"
          value={q} onChange={e => setQ(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add() } }}
          placeholder="type a field + Enter (e.g. LABST), or paste MATNR,WERKS,…" />
        <button type="button" onClick={add} className="btn-secondary py-1 text-xs shrink-0">Add</button>
        <datalist id="explorer-fieldpick-list">
          {(suggestions || []).map(f => <option key={f} value={f} label={fieldLabels[f] || ''} />)}
        </datalist>
      </div>
    </div>
  )
}

// Ad-hoc read + discovery surface. Lets you find out WHAT to pull (table names,
// field names, OData services) and SEE SAP rows before turning a request into a
// scheduled pull. Everything here is read-only (GET) — it never writes to SQL.
export default function SapExplorerPage() {
  const [door, setDoor] = useState('rfc_table')
  const [form, setForm] = useState({
    sap_table: 'LQUA',
    fields: 'LGNUM,MATNR,WERKS,LGTYP,LGPLA,GESME,VERME,MEINS',
    odata_service: '', odata_entity: '', odata_filter: '', odata_select: '',
    sf_query: 'SELECT LGNUM,MATNR,WERKS,LGTYP,LGPLA,GESME,VERME\nFROM V2RETAIL.BRONZE.SAP_LQUA', sf_database: 'V2RETAIL', sf_schema: 'BRONZE',
    env: '', limit: 50,
  })
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [whereConds, setWhereConds] = useState([])
  const [discoveredFields, setDiscoveredFields] = useState([])
  const displayMode = useSapUiStore(s => s.displayMode)       // 'name' | 'label' | 'both' — global (set in Connection)
  const setDisplayMode = useSapUiStore(s => s.setDisplayMode)
  const [fieldLabels, setFieldLabels] = useState({})    // FIELDNAME -> SAP label (shared)

  // Sync the global display preference from the Connection config on mount, so
  // every user gets the same setting even without opening the Connection page.
  useEffect(() => {
    sapAPI.getConnection()
      .then(({ data }) => { if (data?.data?.display_mode) setDisplayMode(data.data.display_mode) })
      .catch(() => {})
  }, [setDisplayMode])

  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))
  const mergeLabels = (m) => setFieldLabels(prev => ({ ...prev, ...m }))

  // Resolve SAP labels (DD03L -> data element -> DD04T) for any columns we do
  // not have yet, so the result-table double header can show them.
  const loadLabelsFor = async (table, columns) => {
    const missing = (columns || []).filter(c => !fieldLabels[c])
    if (!table || !missing.length) return
    try {
      const inF = missing.map(f => `'${esc(f).toUpperCase()}'`).join(',')
      const { data: d1 } = await sapAPI.preview({
        door: 'rfc_table', sap_table: 'DD03L', fields: 'FIELDNAME,ROLLNAME',
        where_clause: `TABNAME = '${esc(table).toUpperCase()}' AND FIELDNAME IN (${inF})`,
        env: form.env || undefined, limit: 300,
      })
      const rollByField = {}, rolls = new Set()
      ;(d1?.data?.rows || []).forEach(r => { rollByField[r.FIELDNAME] = r.ROLLNAME; if (r.ROLLNAME) rolls.add(r.ROLLNAME) })
      const labelByRoll = {}, rl = [...rolls]
      for (let i = 0; i < rl.length; i += 30) {
        const inR = rl.slice(i, i + 30).map(x => `'${esc(x)}'`).join(',')
        try {
          const { data: d2 } = await sapAPI.preview({
            door: 'rfc_table', sap_table: 'DD04T', fields: 'ROLLNAME,SCRTEXT_M,DDTEXT',
            where_clause: `DDLANGUAGE = 'E' AND ROLLNAME IN (${inR})`,
            env: form.env || undefined, limit: 200,
          })
          ;(d2?.data?.rows || []).forEach(r => { labelByRoll[r.ROLLNAME] = r.SCRTEXT_M || r.DDTEXT || '' })
        } catch { /* best effort */ }
      }
      const add = {}
      missing.forEach(f => { const roll = rollByField[f.toUpperCase()]; if (roll && labelByRoll[roll]) add[f] = labelByRoll[roll] })
      if (Object.keys(add).length) mergeLabels(add)
    } catch { /* labels are best-effort */ }
  }

  const run = async () => {
    setLoading(true); setResult(null)
    try {
      const body = { door, limit: Number(form.limit) || 50 }
      if (door === 'odata') {
        // OData services are published in dev (not prod yet) — default env to dev.
        body.env = form.env || 'dev'
        Object.assign(body, {
          odata_service: form.odata_service, odata_entity: form.odata_entity,
          odata_filter: form.odata_filter || undefined, odata_select: form.odata_select || undefined,
        })
      } else if (door === 'snowflake') {
        Object.assign(body, {
          sf_query: form.sf_query,
          sf_database: form.sf_database || undefined,
          sf_schema: form.sf_schema || undefined,
        })
      } else {
        body.env = form.env || undefined
        Object.assign(body, {
          sap_table: form.sap_table,
          fields: form.fields || undefined,
          where_json: cleanConds(whereConds),
        })
      }
      const { data } = await sapAPI.preview(body)
      const res = data?.data || { columns: [], rows: [] }
      setResult(res)
      toast.success(data?.message || 'Done')
      if (door === 'rfc_table' && form.sap_table && res.columns?.length) {
        loadLabelsFor(form.sap_table, res.columns)   // fill the double header
      }
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Preview failed')
    } finally { setLoading(false) }
  }

  // Fill the RFC form from a discovered table; toggle a field in/out of Fields.
  const useTable = (t) => { setDoor('rfc_table'); set('sap_table', t); set('fields', '') }
  const toggleField = (f) => {
    const up = (f || '').toUpperCase()
    const arr = (form.fields || '').split(',').map(s => s.trim()).filter(Boolean)
    const idx = arr.findIndex(x => x.toUpperCase() === up)
    if (idx >= 0) arr.splice(idx, 1); else arr.push(up)
    set('fields', arr.join(','))
  }
  const useService = (svc, ent) => { setDoor('odata'); set('odata_service', svc); set('odata_entity', ent || '') }
  const selectedFields = (form.fields || '').split(',').map(s => s.trim()).filter(Boolean)
  const selectAllFields = (names) => {
    const cur = (form.fields || '').split(',').map(s => s.trim()).filter(Boolean)
    const seen = new Set(cur.map(s => s.toUpperCase()))
    ;(names || []).forEach(n => { const up = (n || '').toUpperCase(); if (up && !seen.has(up)) { cur.push(up); seen.add(up) } })
    set('fields', cur.join(','))
  }
  const clearFields = () => set('fields', '')

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><Search size={24} /> SAP Explorer</h1>
        <p className="text-gray-500 text-sm mt-0.5">
          Discover what to pull, then read a few rows to confirm — before you create a pull. Read-only; never writes to SQL.
        </p>
      </div>

      {/* ── Discovery ──────────────────────────────────────────────────────── */}
      <Discovery env={form.env}
        onUseTable={useTable} onToggleField={toggleField} onUseService={useService}
        onFields={setDiscoveredFields} selectedFields={selectedFields}
        displayMode={displayMode} onFieldLabels={mergeLabels}
        onSelectAll={selectAllFields} onClear={clearFields} />

      {/* ── Read ───────────────────────────────────────────────────────────── */}
      <div className="card p-6 space-y-4">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-gray-700">Read rows</span>
          <div className="flex gap-2 ml-2">
            {[['rfc_table', 'RFC table'], ['odata', 'OData service'], ['snowflake', 'Snowflake']].map(([d, lbl]) => (
              <button key={d} onClick={() => setDoor(d)}
                className={`px-3 py-1.5 text-xs font-semibold rounded border ${door === d ? 'bg-primary-50 text-primary-700 border-primary-400' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'}`}>
                {lbl}
              </button>
            ))}
          </div>
        </div>

        {door === 'rfc_table' && (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">SAP table</label>
              <input className="input font-mono" value={form.sap_table} onChange={e => set('sap_table', e.target.value)} placeholder="LQUA" />
            </div>
            <div>
              <label className="label">Environment (blank = default)</label>
              <input className="input" value={form.env} onChange={e => set('env', e.target.value)} placeholder="prod / qa / dev" />
            </div>
            <div className="col-span-2">
              <label className="label">Fields (required for wide tables)</label>
              <FieldPicker fields={selectedFields} fieldLabels={fieldLabels} displayMode={displayMode}
                onToggle={toggleField} suggestions={discoveredFields} />
            </div>
            <div className="col-span-2">
              <label className="label">Conditions (optional)</label>
              <WhereBuilder conds={whereConds} onChange={setWhereConds} fieldSuggestions={discoveredFields} listId="explorer-field-list" />
            </div>
          </div>
        )}
        {door === 'odata' && (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">OData service</label>
              <input className="input font-mono" value={form.odata_service} onChange={e => set('odata_service', e.target.value)} placeholder="Z_SB_ARTICLE" />
            </div>
            <div>
              <label className="label">Entity</label>
              <input className="input font-mono" value={form.odata_entity} onChange={e => set('odata_entity', e.target.value)} placeholder="ArtMin" />
            </div>
            <div>
              <label className="label">$filter (optional)</label>
              <input className="input font-mono" value={form.odata_filter} onChange={e => set('odata_filter', e.target.value)} placeholder="mtart eq 'FERT'" />
            </div>
            <div>
              <label className="label">$select (optional)</label>
              <input className="input font-mono" value={form.odata_select} onChange={e => set('odata_select', e.target.value)} placeholder="matnr,mtart" />
            </div>
            <div>
              <label className="label">Environment</label>
              <input className="input" value={form.env} onChange={e => set('env', e.target.value)} placeholder="dev (services are published in dev)" />
            </div>
            <div className="col-span-2 text-[11px] text-amber-600">
              OData services are activated in <b>dev</b> (catalog shows <code>not_shipped</code> = not yet in prod). Leave Environment blank or set <code>dev</code> to get data.
            </div>
          </div>
        )}
        {door === 'snowflake' && (
          <div className="grid grid-cols-2 gap-4">
            <div className="col-span-2">
              <label className="label">SELECT query</label>
              <textarea className="input font-mono h-28" value={form.sf_query} onChange={e => set('sf_query', e.target.value)}
                placeholder="SELECT LGNUM,MATNR,WERKS,GESME,VERME FROM V2RETAIL.BRONZE.SAP_LQUA" />
            </div>
            <div>
              <label className="label">Database (USE, optional)</label>
              <input className="input font-mono" value={form.sf_database} onChange={e => set('sf_database', e.target.value)} placeholder="V2RETAIL" />
            </div>
            <div>
              <label className="label">Schema (USE, optional)</label>
              <input className="input font-mono" value={form.sf_schema} onChange={e => set('sf_schema', e.target.value)} placeholder="BRONZE" />
            </div>
            <div className="col-span-2 text-[11px] text-blue-600">
              Reads directly from Snowflake with your key-pair (read-only SELECT/WITH only) — no gateway relay, no RFC buffer limit.
            </div>
          </div>
        )}

        <div className="flex items-end gap-3 pt-2 border-t">
          <div className="w-32">
            <label className="label">Rows (max 500)</label>
            <input type="number" className="input" value={form.limit} onChange={e => set('limit', e.target.value)} />
          </div>
          <button onClick={run} disabled={loading} className="btn-primary">
            {loading ? <RefreshCw size={16} className="animate-spin" /> : <Play size={16} />} Read from SAP
          </button>
        </div>
        <div className="text-xs text-gray-500 bg-amber-50 border border-amber-200 rounded p-2 flex gap-2">
          <Info size={14} className="mt-0.5 shrink-0" />
          Wide SAP tables (like LQUA) fail with <code>DATA_BUFFER_EXCEEDED</code> if you fetch all
          columns — always list specific fields.
        </div>
      </div>

      {result && (
        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-2 bg-gray-50 border-b text-sm text-gray-600 flex items-center justify-between gap-2">
            <span className="flex items-center gap-2"><Database size={14} /> {result.rows?.length || 0} row(s) · {result.columns?.length || 0} column(s)</span>
            <span className="text-[11px] text-gray-400">Field display: <span className="font-medium text-gray-600">{displayMode}</span> · change in SAP → Connection</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="bg-gray-100 text-gray-600">
                <tr>{(result.columns || []).map(c => {
                  const label = fieldLabels[c]
                  let top = c, bottom = ''
                  if (displayMode === 'label') top = label || c
                  else if (displayMode === 'both') bottom = label || ''
                  return (
                    <th key={c} className="px-3 py-2 text-left whitespace-nowrap align-bottom border-b border-gray-200">
                      <div className="font-semibold">{top}</div>
                      {bottom && <div className="text-[10px] text-gray-400 font-normal font-mono">{bottom}</div>}
                    </th>
                  )
                })}</tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(result.rows || []).map((r, i) => (
                  <tr key={i} className="hover:bg-gray-50">
                    {(result.columns || []).map(c => <td key={c} className="px-3 py-1.5 font-mono whitespace-nowrap">{String(r[c] ?? '')}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(!result.rows || result.rows.length === 0) && <div className="p-6 text-center text-gray-400 text-sm">No rows returned.</div>}
        </div>
      )}
    </div>
  )
}

// ── Discovery panel — find table names, field names, OData services ─────────
function Discovery({ env, onUseTable, onToggleField, onUseService, onFields, selectedFields = [],
                     displayMode = 'name', onFieldLabels, onSelectAll, onClear }) {
  const [mode, setMode] = useState('table')   // table | fields | odata
  const [kw, setKw] = useState('STOCK')
  const [tbl, setTbl] = useState('MARD')
  const [busy, setBusy] = useState(false)
  const [rows, setRows] = useState(null)       // for table/fields
  const [services, setServices] = useState(null)
  const [fieldQ, setFieldQ] = useState('')     // filter the field list
  const selSet = new Set((selectedFields || []).map(s => String(s).toUpperCase()))

  const findTables = async () => {
    setBusy(true); setRows(null)
    try {
      const { data } = await sapAPI.preview({
        door: 'rfc_table', sap_table: 'DD02T',
        fields: 'TABNAME,DDTEXT',
        where_clause: `DDLANGUAGE = 'E' AND DDTEXT LIKE '%${esc(kw).toUpperCase()}%'`,
        env: env || undefined, limit: 60,
      })
      setRows(data?.data?.rows || [])
    } catch (e) { toast.error(e.response?.data?.detail || 'Search failed') }
    finally { setBusy(false) }
  }

  const listFields = async () => {
    setBusy(true); setRows(null)
    try {
      const { data } = await sapAPI.preview({
        door: 'rfc_table', sap_table: 'DD03L',
        fields: 'FIELDNAME,ROLLNAME',
        where_clause: `TABNAME = '${esc(tbl).toUpperCase()}'`,
        env: env || undefined, limit: 300,
      })
      // Drop structural pseudo-fields like .INCLUDE / DUMMY.
      let clean = (data?.data?.rows || []).filter(r => r.FIELDNAME && !r.FIELDNAME.startsWith('.'))
      // Resolve SAP labels (DD04T) so the Name/Label toggle can show either.
      const rolls = [...new Set(clean.map(r => r.ROLLNAME).filter(Boolean))]
      const labelByRoll = {}
      for (let i = 0; i < rolls.length; i += 30) {
        const inR = rolls.slice(i, i + 30).map(r => `'${esc(r)}'`).join(',')
        try {
          const { data: ld } = await sapAPI.preview({
            door: 'rfc_table', sap_table: 'DD04T', fields: 'ROLLNAME,SCRTEXT_M,DDTEXT',
            where_clause: `DDLANGUAGE = 'E' AND ROLLNAME IN (${inR})`,
            env: env || undefined, limit: 200,
          })
          ;(ld?.data?.rows || []).forEach(r => { labelByRoll[r.ROLLNAME] = r.SCRTEXT_M || r.DDTEXT || '' })
        } catch { /* best effort */ }
      }
      clean = clean.map(r => ({ ...r, LABEL: labelByRoll[r.ROLLNAME] || '' }))
      setRows(clean)
      onFields?.(clean.map(r => r.FIELDNAME))   // WHERE-builder autocomplete
      const lm = {}; clean.forEach(r => { if (r.LABEL) lm[r.FIELDNAME] = r.LABEL })
      onFieldLabels?.(lm)                        // share labels with the result header
    } catch (e) { toast.error(e.response?.data?.detail || 'Field lookup failed') }
    finally { setBusy(false) }
  }

  const listServices = async () => {
    setBusy(true); setServices(null)
    try {
      const { data } = await sapAPI.odataServices(env || undefined)
      const cat = data?.data?.catalog || data?.data || []
      setServices(Array.isArray(cat) ? cat : [])
    } catch (e) { toast.error(e.response?.data?.detail || 'Service discovery failed') }
    finally { setBusy(false) }
  }

  return (
    <div className="card p-6 space-y-4 border-primary-100">
      <div className="flex items-center gap-2">
        <Compass size={18} className="text-primary-600" />
        <span className="font-semibold text-gray-800">Discover — what to pull</span>
      </div>
      <div className="flex flex-wrap gap-2">
        {[['table', 'Find a table', Search], ['fields', 'List a table’s fields', Columns], ['odata', 'OData services', Boxes]].map(([m, label, Icon]) => (
          <button key={m} onClick={() => { setMode(m); setRows(null); setServices(null) }}
            className={`px-3 py-1.5 text-xs font-semibold rounded border inline-flex items-center gap-1.5 ${mode === m ? 'bg-primary-50 text-primary-700 border-primary-400' : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'}`}>
            <Icon size={13} /> {label}
          </button>
        ))}
      </div>

      {mode === 'table' && (
        <div className="flex items-end gap-2">
          <div className="flex-1 max-w-xs">
            <label className="label">Keyword in table description</label>
            <input className="input" value={kw} onChange={e => setKw(e.target.value)} placeholder="STOCK / ARTICLE / SALES" onKeyDown={e => e.key === 'Enter' && findTables()} />
          </div>
          <button onClick={findTables} disabled={busy} className="btn-secondary">{busy ? <RefreshCw size={15} className="animate-spin" /> : <Search size={15} />} Search</button>
        </div>
      )}
      {mode === 'fields' && (
        <div className="flex items-end gap-2">
          <div className="flex-1 max-w-xs">
            <label className="label">SAP table name</label>
            <input className="input font-mono" value={tbl} onChange={e => setTbl(e.target.value)} placeholder="MARD" onKeyDown={e => e.key === 'Enter' && listFields()} />
          </div>
          <button onClick={listFields} disabled={busy} className="btn-secondary">{busy ? <RefreshCw size={15} className="animate-spin" /> : <Columns size={15} />} List fields</button>
        </div>
      )}
      {mode === 'odata' && (
        <button onClick={listServices} disabled={busy} className="btn-secondary">{busy ? <RefreshCw size={15} className="animate-spin" /> : <Boxes size={15} />} Load catalog</button>
      )}

      {/* Results */}
      {rows && mode === 'table' && (
        <div className="max-h-72 overflow-y-auto border rounded divide-y divide-gray-100">
          {rows.length === 0 && <div className="p-3 text-sm text-gray-400">No tables matched.</div>}
          {rows.map((r, i) => (
            <div key={i} className="flex items-center justify-between px-3 py-1.5 text-xs hover:bg-gray-50">
              <span><span className="font-mono font-semibold text-gray-800">{r.TABNAME}</span> <span className="text-gray-500">— {r.DDTEXT}</span></span>
              <button onClick={() => onUseTable(r.TABNAME)} className="text-primary-600 hover:underline shrink-0 ml-2">Use →</button>
            </div>
          ))}
        </div>
      )}
      {rows && mode === 'fields' && (
        rows.length === 0 ? (
          <div className="p-1 text-sm text-gray-400">No fields found — check the table name.</div>
        ) : (
          (() => {
            const ql = fieldQ.trim().toUpperCase()
            const shown = ql ? rows.filter(r => String(r.FIELDNAME).toUpperCase().includes(ql) || String(r.LABEL || '').toUpperCase().includes(ql)) : rows
            return (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <span className="text-xs text-gray-500">
                    {shown.length}{ql ? ` / ${rows.length}` : ''} field(s) — click to add/remove; <span className="text-primary-700 font-medium">highlighted = selected</span>
                  </span>
                  <div className="flex items-center gap-2 text-[11px]">
                    <button onClick={() => onSelectAll?.(shown.map(r => r.FIELDNAME))} className="text-primary-600 hover:underline font-medium">
                      Select all{ql ? ' (shown)' : ''}
                    </button>
                    <span className="text-gray-300">·</span>
                    <button onClick={() => onClear?.()} className="text-gray-500 hover:underline">Clear</button>
                    <span className="text-gray-300">·</span>
                    <span className="text-gray-400">display: <span className="font-medium text-gray-600">{displayMode}</span></span>
                  </div>
                </div>
                <div className="relative">
                  <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
                  <input className="input text-xs pl-7" value={fieldQ} onChange={e => setFieldQ(e.target.value)}
                    placeholder="search fields by name or label…" />
                </div>
                <div className="max-h-72 overflow-y-auto border rounded p-2 flex flex-wrap gap-1.5">
                  {shown.length === 0 && <span className="text-xs text-gray-400">No field matches “{fieldQ}”.</span>}
                  {shown.map((r, i) => {
                    const on = selSet.has(String(r.FIELDNAME).toUpperCase())
                    const text = displayMode === 'label' ? (r.LABEL || r.FIELDNAME)
                      : displayMode === 'both' ? `${r.FIELDNAME}${r.LABEL ? ' · ' + r.LABEL : ''}`
                      : r.FIELDNAME
                    return (
                      <button key={i} onClick={() => onToggleField(r.FIELDNAME)}
                        title={`${r.FIELDNAME}${r.LABEL ? ' — ' + r.LABEL : ''} · click to ${on ? 'remove' : 'add'}`}
                        className={`px-2 py-0.5 text-[11px] font-mono rounded border transition-colors ${on ? 'bg-primary-600 text-white border-primary-600' : 'bg-gray-100 text-gray-700 border-transparent hover:bg-primary-100'}`}>
                        {on ? '✓ ' : ''}{text}
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          })()
        )
      )}
      {services && mode === 'odata' && (
        <div className="max-h-80 overflow-y-auto space-y-3">
          {services.length === 0 && <div className="p-1 text-sm text-gray-400">No services in the catalog.</div>}
          {services.map((s, i) => (
            <div key={i} className="border rounded p-3">
              <div className="flex items-center justify-between">
                <span className="font-mono font-semibold text-gray-800 text-sm">{s.service}</span>
                {s.status && <span className={`text-[10px] px-1.5 py-0.5 rounded ${s.status === 'shipped' ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'}`}>{s.status}</span>}
              </div>
              {s.description && <div className="text-xs text-gray-500 mt-0.5">{s.description}</div>}
              <div className="mt-2 flex flex-wrap gap-1.5">
                {(s.entities || []).map((e, j) => (
                  <button key={j} onClick={() => onUseService(s.service, e.name)} title={e.description || ''}
                    className="px-2 py-0.5 text-[11px] font-mono rounded bg-gray-100 hover:bg-primary-100 text-gray-700">
                    {e.name} →
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
