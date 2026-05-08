import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useParams, Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Download, Columns, RefreshCw, Copy, ClipboardList, FilterX } from 'lucide-react'
import { tablesAPI, dataAPI, checklistAPI, rlsAPI } from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import toast from 'react-hot-toast'
import useAuthStore from '@/store/authStore'

export default function TableDataPage() {
  const { tableName } = useParams()
  const [searchParams] = useSearchParams()
  const fromParam = searchParams.get('from')
  const fromChecklist = fromParam === 'checklist'
  const fromGridBuilder = fromParam === 'grid-builder'
  const [schema, setSchema] = useState(null)
  const [rowData, setRowData] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(100)
  const [loading, setLoading] = useState(true)
  const [serverFilters, setServerFilters] = useState(null)
  const { hasPermission, isSuperAdmin } = useAuthStore()
  const [colRestrictions, setColRestrictions] = useState([]) // [{column_name, is_visible, is_masked}]
  const filterTimer = useRef(null)

  const loadSchema = async () => {
    try {
      const { data } = await tablesAPI.schema(tableName)
      setSchema(data.data)
    } catch {}
  }

  const loadData = async (p = page, filters = serverFilters) => {
    setLoading(true)
    try {
      const params = { page: p, page_size: pageSize }
      if (filters && Object.keys(filters).length > 0) {
        params.filters = JSON.stringify(filters)
      }
      const { data } = await tablesAPI.data(tableName, params)
      setRowData(data.data?.data || [])
      setTotal(data.data?.total || 0)
    } finally { setLoading(false) }
  }

  useEffect(() => {
    loadSchema(); loadData(1, null)
    if (fromChecklist) checklistAPI.stamp(tableName).catch(() => {})
    if (!isSuperAdmin()) {
      rlsAPI.myColumnRestrictions(tableName)
        .then(r => setColRestrictions(r.data.data || []))
        .catch(() => {})
    }
  }, [tableName])
  useEffect(() => { loadData(page) }, [page, pageSize])

  // Convert ag-grid filter model to server filter format
  const onFilterChanged = useCallback((params) => {
    const model = params.api.getFilterModel()
    if (!model || Object.keys(model).length === 0) {
      setServerFilters(null)
      setPage(1)
      clearTimeout(filterTimer.current)
      filterTimer.current = setTimeout(() => loadData(1, null), 300)
      return
    }
    const filters = {}
    Object.entries(model).forEach(([col, filterDef]) => {
      if (filterDef.filterType === 'text') {
        const type = filterDef.type || 'contains'
        filters[col] = { type, filter: filterDef.filter }
      }
    })
    setServerFilters(filters)
    setPage(1)
    clearTimeout(filterTimer.current)
    filterTimer.current = setTimeout(() => loadData(1, filters), 400)
  }, [tableName, pageSize])

  const HIDDEN_COLS = new Set(['UPLOAD_DATETIME'])

  const columnDefs = useMemo(() => {
    if (!schema?.columns) return []
    const isNumType = (t) => ['int','bigint','smallint','tinyint','float','real','decimal','numeric','money'].includes((t||'').toLowerCase())
    const isTextType = (t) => ['nvarchar','varchar','nchar','char','ntext','text'].includes((t||'').toLowerCase())
    const isDateType = (t) => ['date','datetime','datetime2','smalldatetime','time'].includes((t||'').toLowerCase())

    const restrictionMap = Object.fromEntries(colRestrictions.map(r => [r.column_name, r]))

    return schema.columns.filter(col => {
      if (HIDDEN_COLS.has((col.column_name || '').toUpperCase())) return false
      const r = restrictionMap[col.column_name]
      if (r && (!r.is_visible || r.is_masked)) return false // hide masked/invisible columns
      return true
    }).map(col => {
      const dt = col.data_type || ''
      const isNum = isNumType(dt)
      const isText = isTextType(dt)
      const isDate = isDateType(dt)
      const header = col.display_name || col.column_name || ''
      // Width = enough for the header text (avg 7.5px/char + 28px for sort+filter icons + padding)
      // floored to a per-type minimum so 1-char numeric headers like "0001" still get a usable width.
      const headerW = Math.ceil(header.length * 7.5) + 30
      const baseMin = isNum ? 75 : isDate ? 110 : isText ? 120 : 90
      const width = Math.max(headerW, baseMin)
      const restriction = restrictionMap[col.column_name]
      const rlsReadOnly = restriction && restriction.is_visible && !restriction.can_edit
      return {
        field: col.column_name,
        headerName: header,
        sortable: true,
        filter: true,
        resizable: true,
        editable: hasPermission('DATA_EDIT') && !col.is_primary_key && !rlsReadOnly,
        cellClass: col.is_primary_key ? 'ag-cell-pk' : rlsReadOnly ? 'ag-cell-rls-readonly' : '',
        headerClass: rlsReadOnly ? 'ag-header-rls-readonly' : '',
        width,
        minWidth: 70,
      }
    })
  }, [schema, hasPermission, colRestrictions])

  const defaultColDef = useMemo(() => ({
    filter: 'agTextColumnFilter',
    floatingFilter: true,
    resizable: true,
    enableCellChangeFlash: true,
    suppressSizeToFit: false,
    valueFormatter: (p) => {
      if (p.value == null || p.value === '') return ''
      const n = Number(p.value)
      if (isNaN(n)) return p.value
      const col = (p.colDef?.field || '').toUpperCase()
      // CONT columns → 4 decimals
      if (col.includes('CONT')) return n.toFixed(4)
      // SALE columns → 2 decimals
      if (col.includes('SAL') || col.includes('SALE')) return n.toFixed(2)
      // Everything else → integer
      return String(Math.round(n))
    },
  }), [])

  const onCellValueChanged = useCallback(async (params) => {
    const pkCols = schema?.columns?.filter(c => c.is_primary_key).map(c => c.column_name) || []
    if (pkCols.length === 0) return toast.error('No PK defined — cannot save edit')
    const pkValues = {}
    pkCols.forEach(pk => { pkValues[pk] = params.data[pk] })
    try {
      await dataAPI.update({
        table_name: tableName,
        primary_key_columns: pkCols,
        primary_key_values: pkValues,
        updates: { [params.colDef.field]: params.newValue },
      })
      toast.success('Cell updated')
      if (fromChecklist) checklistAPI.stamp(tableName).catch(() => {})
    } catch { params.api.undoCellEditing() }
  }, [schema, tableName, fromChecklist])

  const gridRef = useRef()

  // ── Cell selection state (refs to avoid stale closures) ──
  const selRef = useRef({ anchor: null, start: null, end: null, cols: [] })
  const [selCount, setSelCount] = useState(0)

  const getAllDataCols = () => {
    const api = gridRef.current?.api
    if (!api) return []
    return api.getAllDisplayedColumns()
      .map(c => c.getColId())
      .filter(c => c && c !== 'ag-Grid-SelectionColumn' && c !== '0')
  }

  const refreshCells = (cells) => {
    // cells = [{row, colId}, ...] — refresh only these specific cells
    const api = gridRef.current?.api
    if (!api || !cells.length) return
    const byRow = {}
    cells.forEach(({ row, colId }) => {
      if (!byRow[row]) byRow[row] = []
      byRow[row].push(colId)
    })
    const rowNodes = []
    const columns = new Set()
    Object.entries(byRow).forEach(([r, colIds]) => {
      const node = api.getDisplayedRowAtIndex(Number(r))
      if (node) rowNodes.push(node)
      colIds.forEach(c => columns.add(c))
    })
    const colObjs = api.getAllDisplayedColumns().filter(c => columns.has(c.getColId()))
    if (rowNodes.length && colObjs.length) {
      api.refreshCells({ rowNodes, columns: colObjs, force: true })
    }
  }

  // Collect all cells in a selection range
  const getCellsInRange = (sel) => {
    if (!sel.start) return []
    const cells = []
    for (let r = sel.start.row; r <= sel.end.row; r++) {
      for (const c of sel.cols) cells.push({ row: r, colId: c })
    }
    return cells
  }

  const setSelection = (anchor, endCell) => {
    const allCols = getAllDataCols()
    const ai = allCols.indexOf(anchor.colId)
    const ei = allCols.indexOf(endCell.colId)
    if (ai < 0 || ei < 0) return

    const prev = selRef.current
    const prevCells = getCellsInRange(prev)

    selRef.current = {
      anchor,
      start: { row: Math.min(anchor.row, endCell.row), col: Math.min(ai, ei) },
      end:   { row: Math.max(anchor.row, endCell.row), col: Math.max(ai, ei) },
      cols:  allCols.slice(Math.min(ai, ei), Math.max(ai, ei) + 1),
    }
    const newCells = getCellsInRange(selRef.current)
    setSelCount(newCells.length)

    // Only refresh old + new cells (deduplicated by Set in refreshCells)
    const allCellsToRefresh = [...prevCells, ...newCells]
    refreshCells(allCellsToRefresh)
  }

  const clearSel = () => {
    const prevCells = getCellsInRange(selRef.current)
    selRef.current = { anchor: null, start: null, end: null, cols: [] }
    setSelCount(0)
    refreshCells(prevCells)
  }

  const isCellSelected = (rowIndex, colId) => {
    const s = selRef.current
    if (!s.start) return false
    return rowIndex >= s.start.row && rowIndex <= s.end.row && s.cols.includes(colId)
  }

  const getSelectedText = (withHeaders) => {
    const api = gridRef.current?.api
    const s = selRef.current
    if (!api || !s.start) return null
    const lines = []
    for (let r = s.start.row; r <= s.end.row; r++) {
      const node = api.getDisplayedRowAtIndex(r)
      if (node) lines.push(s.cols.map(c => String(node.data[c] ?? '')).join('\t'))
    }
    if (withHeaders) return [s.cols.join('\t'), ...lines].join('\n')
    return lines.length > 0 ? lines.join('\n') : null
  }

  // ── Mouse handlers: click, shift+click, drag ──
  const getCellInfo = (ev) => {
    let el = ev.target
    while (el && !el.getAttribute?.('col-id')) el = el.parentElement
    if (!el) return null
    const colId = el.getAttribute('col-id')
    if (!colId || colId === 'ag-Grid-SelectionColumn' || colId === '0') return null
    let rowEl = el
    while (rowEl && !rowEl.getAttribute?.('row-index')) rowEl = rowEl.parentElement
    if (!rowEl) return null
    const row = parseInt(rowEl.getAttribute('row-index'), 10)
    return isNaN(row) ? null : { row, colId }
  }

  const draggingRef = useRef(false)
  const gridWrapRef = useRef(null)

  useEffect(() => {
    const el = gridWrapRef.current
    if (!el) return

    const onDown = (ev) => {
      if (ev.button !== 0) return
      if (ev.target.closest('.ag-selection-checkbox, .ag-header, .ag-floating-filter')) return
      const cell = getCellInfo(ev)
      if (!cell) return

      if (ev.shiftKey && selRef.current.anchor) {
        ev.preventDefault()
        setSelection(selRef.current.anchor, cell)
      } else {
        setSelection(cell, cell)
      }
      draggingRef.current = true
    }

    const onMove = (ev) => {
      if (!draggingRef.current || !selRef.current.anchor) return
      const cell = getCellInfo(ev)
      if (cell) setSelection(selRef.current.anchor, cell)
    }

    const onUp = () => { draggingRef.current = false }

    el.addEventListener('mousedown', onDown)
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      el.removeEventListener('mousedown', onDown)
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
  })

  // ── Keyboard: Ctrl+C, Ctrl+Shift+C, Escape ──
  useEffect(() => {
    const onKey = (ev) => {
      if (ev.key === 'Escape') { clearSel(); return }
      if (!(ev.ctrlKey || ev.metaKey) || ev.key.toLowerCase() !== 'c') return

      const api = gridRef.current?.api
      if (!api) return

      // Priority 1: cell selection from mouse (single or range)
      const text = getSelectedText(ev.shiftKey)
      if (text !== null) {
        ev.preventDefault()
        navigator.clipboard.writeText(text)
        const s = selRef.current
        const count = (s.end.row - s.start.row + 1) * s.cols.length
        toast.success(`Copied ${count} cell(s)${ev.shiftKey ? ' with headers' : ''}`)
        return
      }

      // Priority 2: checkbox-selected rows
      const selRows = api.getSelectedRows()
      if (selRows.length > 0) {
        ev.preventDefault()
        const cols = api.getColumnDefs().map(c => c.field).filter(Boolean)
        const lines = selRows.map(r => cols.map(c => r[c] ?? '').join('\t'))
        const rowText = ev.shiftKey ? [cols.join('\t'), ...lines].join('\n') : lines.join('\n')
        navigator.clipboard.writeText(rowText)
        toast.success(`Copied ${selRows.length} row(s)${ev.shiftKey ? ' with headers' : ''}`)
        return
      }

    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const exportCSV = () => {
    const restrictionMap = Object.fromEntries(colRestrictions.map(r => [r.column_name, r]))
    const headers = (schema?.columns?.map(c => c.column_name) || []).filter(col => {
      const r = restrictionMap[col]
      return !r || (r.is_visible && !r.is_masked)
    })
    const csv = [headers.join(','), ...rowData.map(r => headers.map(h => `"${r[h] ?? ''}"`).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${tableName}.csv`; a.click()
  }

  const copyToClipboard = (withHeaders) => {
    const headers = schema?.columns?.map(c => c.column_name) || []
    const rows = rowData.map(r => headers.map(h => r[h] ?? '').join('\t'))
    const text = withHeaders ? [headers.join('\t'), ...rows].join('\n') : rows.join('\n')
    navigator.clipboard.writeText(text).then(
      () => toast.success(`Copied ${rowData.length} rows${withHeaders ? ' with headers' : ''}`),
      () => toast.error('Copy failed')
    )
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Link to={fromChecklist ? "/data-validation/checklist" : fromGridBuilder ? "/data-prep/store-stock" : "/tables"} className="p-1.5 hover:bg-gray-100 rounded-md"><ArrowLeft size={14} /></Link>
          <div>
            <h1 className="text-[13px] font-bold text-gray-900">{schema?.display_name || tableName}</h1>
            <div className="flex items-center gap-2 text-[10px] text-gray-500">
              <span>{total.toLocaleString()} rows</span>
              <span>{schema?.columns?.length || 0} columns</span>
              {schema?.module && <span className="badge-gray">{schema.module}</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(1) }}
            className="input w-auto" title="Rows per page">
            {[100, 200, 500, 1000, 2000, 5000, 10000].map(n => <option key={n} value={n}>{n.toLocaleString()} rows</option>)}
          </select>
          {serverFilters && Object.keys(serverFilters).length > 0 && (
            <button onClick={() => { gridRef.current?.api?.setFilterModel(null); setServerFilters(null); setPage(1); loadData(1, null) }}
              className="btn-ghost btn-sm text-red-500" title="Clear all filters"><FilterX size={12} /> Clear</button>
          )}
          <button onClick={() => loadData(page)} className="btn-ghost btn-sm"><RefreshCw size={12} /> Refresh</button>
          <button onClick={() => copyToClipboard(true)} className="btn-ghost btn-sm" title="Copy with headers"><ClipboardList size={12} /></button>
          <button onClick={() => copyToClipboard(false)} className="btn-ghost btn-sm" title="Copy values only"><Copy size={12} /></button>
          <button onClick={exportCSV} className="btn-secondary btn-sm"><Download size={12} /> CSV</button>
        </div>
      </div>

      <div ref={gridWrapRef} className="ag-theme-alpine ag-compact" style={{ width: '100%', height: 'calc(100vh - 170px)' }}>
        <AgGridReact
          ref={gridRef}
          rowData={rowData}
          columnDefs={columnDefs}
          defaultColDef={defaultColDef}
          onCellValueChanged={onCellValueChanged}
          onFilterChanged={onFilterChanged}
          undoRedoCellEditing
          ensureDomOrder
          pagination={false}
          rowHeight={22}
          headerHeight={26}
          floatingFiltersHeight={24}
          rowSelection={{ mode: 'multiRow', enableClickSelection: false }}
          onCellFocused={(e) => {
            // Skip if mouse drag is active (mouse handlers manage selection)
            if (draggingRef.current) return
            if (e.rowIndex == null || !e.column) return
            const colId = e.column.getColId()
            if (!colId || colId === 'ag-Grid-SelectionColumn' || colId === '0') return
            const cell = { row: e.rowIndex, colId }
            setSelection(cell, cell)
          }}
          cellStyle={(params) => {
            const colId = params.column?.getColId()
            if (colId && isCellSelected(params.rowIndex, colId)) {
              return { backgroundColor: '#bfdbfe', color: '#1e3a5f', fontWeight: 500 }
            }
            const r = colRestrictions.find(x => x.column_name === colId)
            if (r && r.is_visible && !r.can_edit) {
              return { backgroundColor: '#f3f4f6', color: '#6b7280' }
            }
            return null
          }}
          loading={loading}
        />
        <div className="text-[8px] text-gray-400 mt-1 px-1" style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
          <span>
            Click cell · <b>Drag</b> select range · <b>Shift+Click</b> extend · Checkbox rows · <b>Ctrl+C</b> copy · <b>Ctrl+Shift+C</b> with headers
          </span>
          {selCount > 0 && (
            <span style={{ display:'flex', alignItems:'center', gap:6 }}>
              <span style={{ fontSize:9, fontWeight:700, color:'#3b82f6', background:'#eff6ff',
                padding:'1px 7px', borderRadius:8, border:'1px solid #bfdbfe' }}>
                {selCount} cell{selCount > 1 ? 's' : ''} selected
              </span>
              <button onClick={clearSel} style={{ fontSize:9, color:'#ef4444', cursor:'pointer',
                background:'none', border:'none', textDecoration:'underline' }}>Clear</button>
            </span>
          )}
        </div>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between">
        <div className="text-[10px] text-gray-500">
          Showing {((page - 1) * pageSize) + 1}–{Math.min(page * pageSize, total)} of {total.toLocaleString()}
        </div>
        <div className="flex items-center gap-1.5">
          <button disabled={page <= 1} onClick={() => setPage(p => p - 1)} className="btn-secondary btn-sm">Previous</button>
          <span className="text-[10px] text-gray-600">Page {page} of {totalPages}</span>
          <button disabled={page >= totalPages} onClick={() => setPage(p => p + 1)} className="btn-secondary btn-sm">Next</button>
        </div>
      </div>
    </div>
  )
}
