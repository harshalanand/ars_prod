/**
 * DropdownFilter — AG Grid Community custom filter that mimics
 * agSetColumnFilter (Enterprise) using a checkbox dropdown.
 *
 * Features:
 *  - All distinct values in the column listed as checkboxes
 *  - Top search box to narrow the list
 *  - "Select all" / "Clear" toggle
 *  - Multi-select; filter passes rows whose value is in the selected set
 *  - Empty selection = "all values" (no filter applied)
 *
 * Usage in a colDef:
 *   { field: 'STATUS', filter: DropdownFilter, floatingFilter: true }
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react'

const DropdownFilter = forwardRef((props, ref) => {
  const { api, colDef, filterChangedCallback } = props
  const filterParams = colDef?.filterParams || {}
  const [selected, setSelected] = useState(() => {
    const seed = filterParams.initialValues
    if (Array.isArray(seed) && seed.length > 0) return new Set(seed.map(String))
    return new Set()
  })
  const [search, setSearch] = useState('')
  const [serverFetching, setServerFetching] = useState(false)
  const allValuesRef = useRef([])
  const isFirstRenderRef = useRef(true)

  // Compute the distinct values for this column from all rows currently in the grid.
  // Used as a fallback when no server-side fetcher is configured.
  const computeDistinct = useCallback(() => {
    const field = colDef?.field
    if (!field || !api) return []
    const seen = new Map()  // value → count
    api.forEachNode((node) => {
      if (!node.data) return
      const v = node.data[field]
      const key = v == null ? '' : String(v)
      seen.set(key, (seen.get(key) || 0) + 1)
    })
    return Array.from(seen.entries())
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => a.value.localeCompare(b.value, undefined, { numeric: true, sensitivity: 'base' }))
  }, [api, colDef])

  const [distinct, setDistinct] = useState([])

  // If filterParams provides a server-side fetcher, prefer it. Otherwise
  // fall back to computing from loaded rows.
  useEffect(() => {
    const fetcher = filterParams.fetchDistinct
    const field = colDef?.field
    if (typeof fetcher === 'function' && field) {
      let cancelled = false
      setServerFetching(true)
      Promise.resolve(fetcher(field, search))
        .then((rows) => {
          if (cancelled) return
          // Expect rows in shape [{ value, count }] or array of strings.
          const normalized = (rows || []).map((r) =>
            typeof r === 'string' ? { value: r, count: 0 } : { value: String(r.value ?? ''), count: r.count ?? 0 }
          )
          setDistinct(normalized)
        })
        .catch(() => { if (!cancelled) setDistinct([]) })
        .finally(() => { if (!cancelled) setServerFetching(false) })
      return () => { cancelled = true }
    }
    // Fallback: compute from loaded rows
    setDistinct(computeDistinct())
  }, [filterParams.fetchDistinct, colDef, search, computeDistinct])

  // When the grid's row model changes AND we're in fallback mode, recompute.
  useEffect(() => {
    if (typeof filterParams.fetchDistinct === 'function') return
    if (!api) return
    const handler = () => setDistinct(computeDistinct())
    api.addEventListener('modelUpdated', handler)
    return () => api.removeEventListener('modelUpdated', handler)
  }, [api, computeDistinct, filterParams.fetchDistinct])

  // Filter list by the search box
  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return distinct
    return distinct.filter((d) => d.value.toLowerCase().includes(q))
  }, [distinct, search])

  // AG Grid filter API
  useImperativeHandle(ref, () => ({
    isFilterActive: () => selected.size > 0,
    doesFilterPass: (params) => {
      if (selected.size === 0) return true
      const v = params.data?.[colDef?.field]
      const key = v == null ? '' : String(v)
      return selected.has(key)
    },
    getModel: () => (selected.size === 0 ? null : { values: Array.from(selected) }),
    setModel: (model) => {
      if (!model || !Array.isArray(model.values)) {
        setSelected(new Set())
      } else {
        setSelected(new Set(model.values.map((x) => String(x))))
      }
    },
  }))

  // Notify the parent (DataEditorPage) directly via filterParams.onSelectionChange,
  // and notify AG Grid via filterChangedCallback. The direct callback is the
  // primary path — it doesn't depend on AG Grid Community v32 wiring up the
  // event chain to a React custom filter correctly.
  useEffect(() => {
    // Skip the initial mount — don't fire onSelectionChange for the seed.
    if (isFirstRenderRef.current) {
      isFirstRenderRef.current = false
      return
    }
    const field = colDef?.field
    const onSel = filterParams.onSelectionChange
    if (typeof onSel === 'function' && field) {
      onSel(field, Array.from(selected))
    }
    if (typeof filterChangedCallback === 'function') {
      filterChangedCallback()
    }
  }, [selected, filterChangedCallback, colDef, filterParams])

  const toggleValue = (v) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(v)) next.delete(v)
      else next.add(v)
      return next
    })
  }

  const selectAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      visible.forEach((d) => next.add(d.value))
      return next
    })
  }

  const clearAll = () => setSelected(new Set())

  return (
    <div className="ag-dropdown-filter">
      <div className="adf-search">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search…"
          className="adf-search-input"
        />
      </div>
      <div className="adf-actions">
        <button type="button" onClick={selectAllVisible} className="adf-btn">Select all</button>
        <button type="button" onClick={clearAll} className="adf-btn adf-btn-clear">Clear</button>
        <span className="adf-count">
          {selected.size > 0 ? `${selected.size} selected` : `${distinct.length} values`}
        </span>
      </div>
      <ul className="adf-list">
        {serverFetching ? (
          <li className="adf-empty">Loading…</li>
        ) : visible.length === 0 ? (
          <li className="adf-empty">No values</li>
        ) : (
          visible.slice(0, 1000).map((d) => (
            <li key={d.value} className="adf-item">
              <label>
                <input
                  type="checkbox"
                  checked={selected.has(d.value)}
                  onChange={() => toggleValue(d.value)}
                />
                <span className="adf-label">{d.value === '' ? '(empty)' : d.value}</span>
                <span className="adf-badge">{d.count.toLocaleString()}</span>
              </label>
            </li>
          ))
        )}
        {visible.length > 1000 && (
          <li className="adf-truncated">Showing first 1,000 of {visible.length.toLocaleString()} — use search to narrow</li>
        )}
      </ul>
      <style>{`
        .ag-dropdown-filter {
          min-width: 220px;
          max-width: 280px;
          font: 12px -apple-system, "Segoe UI", Arial, sans-serif;
          background: white;
          padding: 6px;
        }
        .adf-search { padding: 0 0 6px; border-bottom: 1px solid #e2e8f0; margin-bottom: 6px; }
        .adf-search-input {
          width: 100%;
          padding: 5px 8px;
          font-size: 12px;
          border: 1px solid #cbd5e1;
          border-radius: 4px;
          outline: none;
        }
        .adf-search-input:focus { border-color: #4f46e5; }
        .adf-actions {
          display: flex; align-items: center; gap: 6px;
          padding: 0 0 6px; border-bottom: 1px solid #e2e8f0; margin-bottom: 4px;
        }
        .adf-btn {
          font-size: 11px;
          padding: 3px 8px;
          border: 1px solid #cbd5e1;
          border-radius: 3px;
          background: #f8fafc;
          color: #4f46e5;
          cursor: pointer;
        }
        .adf-btn:hover { background: #eef2ff; }
        .adf-btn-clear { color: #dc2626; }
        .adf-count { font-size: 10px; color: #94a3b8; margin-left: auto; }
        .adf-list { list-style: none; margin: 0; padding: 0; max-height: 260px; overflow-y: auto; }
        .adf-item label {
          display: flex; align-items: center; gap: 6px;
          padding: 4px 6px; cursor: pointer; border-radius: 3px;
          font-size: 12px;
        }
        .adf-item label:hover { background: #f1f5f9; }
        .adf-item input[type="checkbox"] { margin: 0; cursor: pointer; }
        .adf-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #0f172a; }
        .adf-badge {
          font-size: 10px; color: #64748b;
          background: #f1f5f9; padding: 1px 6px; border-radius: 8px;
        }
        .adf-empty, .adf-truncated {
          padding: 8px 4px; color: #94a3b8; font-size: 11px; text-align: center;
        }
      `}</style>
    </div>
  )
})

DropdownFilter.displayName = 'DropdownFilter'
export default DropdownFilter
