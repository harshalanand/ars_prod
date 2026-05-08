import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { Save, Filter, RefreshCw, Download, Trash2, Search, Edit3, X, Loader2, ChevronDown, Play, Database, AlertTriangle, Plus, Check, ArrowLeft } from 'lucide-react'
import { tablesAPI, dataAPI, checklistAPI } from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import toast from 'react-hot-toast'
import useAuthStore from '@/store/authStore'

// =============================================================================
// Multi-Select Filter Dropdown Component
// =============================================================================
function MultiSelectFilterDropdown({ column, selectedValues = [], onChange, tableName, currentFilters, disabled, onRemove }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [options, setOptions] = useState([])
  const [loading, setLoading] = useState(false)
  const dropdownRef = useRef()

  // Close on outside click
  useEffect(() => {
    const handler = (e) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Load options when dropdown opens
  useEffect(() => {
    if (!open || !tableName || !column) return
    
    const loadOptions = async () => {
      setLoading(true)
      try {
        // Build cascade filters (exclude current column)
        const cascadeFilters = {}
        Object.entries(currentFilters || {}).forEach(([col, vals]) => {
          if (col !== column && vals && vals.length > 0) {
            cascadeFilters[col] = vals
          }
        })
        
        const { data } = await tablesAPI.distinct(tableName, column, {
          filters: Object.keys(cascadeFilters).length > 0 ? JSON.stringify(cascadeFilters) : undefined,
          search: search || undefined,
          limit: 500
        })
        setOptions(data.data?.values || [])
      } catch (e) {
        console.error('Failed to load distinct values:', e)
        setOptions([])
      } finally {
        setLoading(false)
      }
    }
    
    const debounce = setTimeout(loadOptions, 250)
    return () => clearTimeout(debounce)
  }, [open, tableName, column, currentFilters, search])

  const handleToggleValue = (value) => {
    const newValues = selectedValues.includes(value)
      ? selectedValues.filter(v => v !== value)
      : [...selectedValues, value]
    onChange(column, newValues)
  }

  const handleSelectAll = () => {
    onChange(column, options.map(o => o.value))
  }

  const handleClearAll = () => {
    onChange(column, [])
  }

  const displayText = selectedValues.length === 0 
    ? 'Select values...'
    : selectedValues.length === 1 
      ? selectedValues[0]
      : `${selectedValues.length} selected`

  return (
    <div ref={dropdownRef} className="relative">
      <div className="flex items-center gap-1 mb-1">
        <label className="text-xs font-semibold text-gray-600 uppercase tracking-wide">{column}</label>
        {onRemove && (
          <button
            onClick={() => onRemove(column)}
            className="text-gray-400 hover:text-red-500 ml-auto"
            title="Remove filter"
          >
            <X size={12} />
          </button>
        )}
      </div>
      <button
        type="button"
        onClick={() => !disabled && setOpen(!open)}
        disabled={disabled}
        className={`w-full min-w-[180px] flex items-center justify-between gap-2 px-3 py-2 text-sm border rounded-lg bg-white transition-all
          ${disabled ? 'opacity-50 cursor-not-allowed' : 'hover:border-blue-400 cursor-pointer'}
          ${selectedValues.length > 0 ? 'border-blue-500 bg-blue-50 ring-1 ring-blue-200' : 'border-gray-300'}
        `}
      >
        <span className={selectedValues.length > 0 ? 'text-blue-700 font-medium truncate' : 'text-gray-400'}>
          {displayText}
        </span>
        <div className="flex items-center gap-1 shrink-0">
          {selectedValues.length > 0 && (
            <X size={14} className="text-gray-400 hover:text-red-500" 
               onClick={(e) => { e.stopPropagation(); onChange(column, []); }} />
          )}
          <ChevronDown size={14} className={`text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} />
        </div>
      </button>
      
      {open && (
        <div className="absolute z-[100] mt-1 min-w-[300px] max-w-[400px] bg-white border rounded-xl shadow-2xl max-h-96 overflow-hidden">
          {/* Search */}
          <div className="p-2 border-b bg-gray-50">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Type to search..."
              className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-200"
              autoFocus
              onClick={(e) => e.stopPropagation()}
            />
          </div>
          
          {/* Select All / Clear All */}
          {options.length > 0 && (
            <div className="flex gap-2 p-2 border-b bg-gray-50 text-xs">
              <button onClick={handleSelectAll} className="text-blue-600 hover:underline">Select All</button>
              <span className="text-gray-300">|</span>
              <button onClick={handleClearAll} className="text-gray-500 hover:underline">Clear</button>
              <span className="ml-auto text-gray-400">{options.length} options</span>
            </div>
          )}
          
          {/* Options List */}
          <div className="max-h-56 overflow-y-auto">
            {loading ? (
              <div className="p-6 text-center">
                <Loader2 size={20} className="animate-spin mx-auto text-blue-500" />
                <p className="text-xs text-gray-400 mt-2">Loading values...</p>
              </div>
            ) : options.length === 0 ? (
              <div className="p-6 text-center text-gray-400">
                <Database size={24} className="mx-auto mb-2 opacity-50" />
                <p className="text-sm">No values found</p>
              </div>
            ) : (
              options.map((opt, idx) => {
                const isSelected = selectedValues.includes(opt.value)
                const displayValue = opt.value || '(empty)'
                return (
                  <label
                    key={idx}
                    className={`flex items-center gap-3 px-3 py-2.5 text-sm cursor-pointer hover:bg-blue-50 transition-colors border-b border-gray-100 last:border-0
                      ${isSelected ? 'bg-blue-100' : ''}
                    `}
                    title={displayValue}
                  >
                    <div className={`w-5 h-5 border-2 rounded flex items-center justify-center shrink-0
                      ${isSelected ? 'bg-blue-600 border-blue-600' : 'border-gray-300 bg-white'}`}
                    >
                      {isSelected && <Check size={14} className="text-white" strokeWidth={3} />}
                    </div>
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => handleToggleValue(opt.value)}
                      className="sr-only"
                    />
                    <span className="flex-1 font-medium text-gray-800" style={{ wordBreak: 'break-word' }}>{displayValue}</span>
                    <span className="text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded-full shrink-0">{opt.count.toLocaleString()}</span>
                  </label>
                )
              })
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// =============================================================================
// Add Filter Column Modal
// =============================================================================
function AddFilterColumnModal({ columns, existingFilters, onAdd, onClose }) {
  const [search, setSearch] = useState('')
  
  const availableColumns = columns.filter(c => 
    !existingFilters.includes(c.column_name) &&
    c.column_name.toLowerCase().includes(search.toLowerCase())
  )
  
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md" onClick={e => e.stopPropagation()}>
        <div className="p-4 border-b">
          <h3 className="font-semibold text-gray-800">Add Filter Column</h3>
          <p className="text-xs text-gray-500 mt-1">Select columns to add as filters</p>
        </div>
        <div className="p-3 border-b">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search columns..."
            className="w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:border-blue-500"
            autoFocus
          />
        </div>
        <div className="max-h-64 overflow-y-auto">
          {availableColumns.length === 0 ? (
            <div className="p-6 text-center text-gray-400">No more columns available</div>
          ) : (
            availableColumns.map(col => (
              <button
                key={col.column_name}
                onClick={() => { onAdd(col.column_name); onClose(); }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-blue-50 flex items-center justify-between"
              >
                <span>{col.column_name}</span>
                <span className="text-xs text-gray-400">{col.data_type}</span>
              </button>
            ))
          )}
        </div>
        <div className="p-3 border-t bg-gray-50">
          <button onClick={onClose} className="btn-secondary btn-sm w-full">Cancel</button>
        </div>
      </div>
    </div>
  )
}

// =============================================================================
// Main Data Editor Component
// =============================================================================
export default function DataEditorPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const fromChecklist = searchParams.get('from') === 'checklist'
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(searchParams.get('table') || '')
  const [tableSearch, setTableSearch] = useState('')
  const [showTableDropdown, setShowTableDropdown] = useState(false)
  const [schema, setSchema] = useState(null)
  const [rowData, setRowData] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(500)
  const [loading, setLoading] = useState(false)
  const [highlightedIndex, setHighlightedIndex] = useState(-1)
  const [saving, setSaving] = useState(false)
  
  // Pre-filters: { columnName: [value1, value2, ...] }
  const [preFilters, setPreFilters] = useState({})
  const [filterColumns, setFilterColumns] = useState([])
  const [showAddColumnModal, setShowAddColumnModal] = useState(false)
  
  // State management
  const [dataLoaded, setDataLoaded] = useState(false)
  const [totalRowCount, setTotalRowCount] = useState(0)  // Total rows BEFORE any filter
  const [showAddRow, setShowAddRow] = useState(false)
  const [newRowData, setNewRowData] = useState({})
  const [filteredRowCount, setFilteredRowCount] = useState(null)  // Rows after filters (null = not calculated)
  
  const { hasPermission } = useAuthStore()
  const gridRef = useRef()
  const tableDropdownRef = useRef()
  
  const pendingChangesRef = useRef(new Map())
  const [pendingCount, setPendingCount] = useState(0)
  const [changesVersion, setChangesVersion] = useState(0)

  // Load tables list (visible tables only)
  useEffect(() => {
    tablesAPI.listAllVisible().then(r => setTables(r.data.data || [])).catch(() => {})
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e) => {
      if (tableDropdownRef.current && !tableDropdownRef.current.contains(e.target)) {
        setShowTableDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // When table changes
  useEffect(() => {
    if (selectedTable) {
      loadSchema()
      loadTotalRowCount()
      setPage(1)
      setPreFilters({})
      setFilterColumns([])
      setDataLoaded(false)
      setRowData([])
      setTotal(0)
      setFilteredRowCount(null)
      pendingChangesRef.current.clear()
      setPendingCount(0)
    } else {
      setSchema(null)
      setRowData([])
      setFilterColumns([])
      setTotalRowCount(0)
      setDataLoaded(false)
    }
  }, [selectedTable])

  const loadTotalRowCount = async () => {
    try {
      const { data } = await tablesAPI.rowCount(selectedTable)
      const count = data.data?.row_count || 0
      setTotalRowCount(count)
      setFilteredRowCount(null)
    } catch (e) {
      console.error('Failed to load row count:', e)
      setTotalRowCount(0)
    }
  }

  const loadSchema = async () => {
    try {
      const { data } = await tablesAPI.schema(selectedTable)
      setSchema(data.data)
    } catch (e) {
      console.error('Failed to load schema:', e)
    }
  }

  // Update filtered row count when filters change
  useEffect(() => {
    const updateFilteredCount = async () => {
      if (!selectedTable) return
      
      const hasFilters = Object.values(preFilters).some(arr => arr && arr.length > 0)
      if (!hasFilters) {
        setFilteredRowCount(null)
        return
      }
      
      try {
        const filterObj = {}
        Object.entries(preFilters).forEach(([col, vals]) => {
          if (vals && vals.length > 0) {
            filterObj[col] = { type: 'in', filter: vals }
          }
        })
        
        const { data } = await tablesAPI.data(selectedTable, {
          page: 1,
          page_size: 1,
          filters: JSON.stringify(filterObj)
        })
        setFilteredRowCount(data.data?.total || 0)
      } catch {
        setFilteredRowCount(null)
      }
    }
    
    const debounce = setTimeout(updateFilteredCount, 400)
    return () => clearTimeout(debounce)
  }, [preFilters, selectedTable])

  const loadData = useCallback(async () => {
    if (!selectedTable || !schema) return
    
    setLoading(true)
    try {
      const filterObj = {}
      Object.entries(preFilters).forEach(([col, vals]) => {
        if (vals && vals.length > 0) {
          filterObj[col] = { type: 'in', filter: vals }
        }
      })
      
      const params = { 
        page, 
        page_size: pageSize,
        filters: Object.keys(filterObj).length > 0 ? JSON.stringify(filterObj) : undefined
      }
      const { data } = await tablesAPI.data(selectedTable, params)
      
      // Debug logging
      console.log('API Response:', data)
      console.log('Row count from API:', data.data?.data?.length)
      console.log('Total from API:', data.data?.total)
      
      setRowData(data.data?.data || [])
      setTotal(data.data?.total || 0)
      setDataLoaded(true)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load data')
    } finally {
      setLoading(false)
    }
  }, [selectedTable, schema, preFilters, page, pageSize])

  // Load data when page or pageSize changes (only if already loaded)
  useEffect(() => {
    if (dataLoaded && selectedTable && schema) {
      loadData()
    }
  }, [page, pageSize])

  const handleLoadData = () => {
    setPage(1)
    setDataLoaded(false)
    setTimeout(() => loadData(), 50)
  }

  const handlePreFilterChange = (column, values) => {
    setPreFilters(prev => ({ ...prev, [column]: values }))
  }

  const handleAddFilterColumn = (column) => {
    if (!filterColumns.includes(column)) {
      setFilterColumns(prev => [...prev, column])
    }
  }

  const handleRemoveFilterColumn = (column) => {
    setFilterColumns(prev => prev.filter(c => c !== column))
    setPreFilters(prev => {
      const next = { ...prev }
      delete next[column]
      return next
    })
  }

  const handleClearAllFilters = () => {
    setPreFilters({})
  }

  const pkColumns = useMemo(() => {
    return schema?.columns?.filter(c => c.is_primary_key).map(c => c.column_name) || []
  }, [schema])

  const getRowKey = useCallback((data) => {
    if (!data || pkColumns.length === 0) return null
    return pkColumns.map(pk => String(data[pk] ?? '')).join('|')
  }, [pkColumns])

  // AG Grid column definitions with floating filters
  const columnDefs = useMemo(() => {
    if (!schema?.columns) return []
    
    const checkboxCol = {
      headerCheckboxSelection: true,
      checkboxSelection: true,
      headerCheckboxSelectionFilteredOnly: true,
      width: 50,
      maxWidth: 50,
      pinned: 'left',
      lockPosition: true,
      suppressMenu: true,
      resizable: false,
    }
    
    const dataCols = schema.columns.map(col => {
      const isPK = col.is_primary_key
      const field = col.column_name
      const canEdit = col.is_editable && hasPermission('DATA_EDIT')
      return {
        field,
        headerName: col.display_name || col.column_name,
        sortable: true,
        resizable: true,
        filter: 'agTextColumnFilter',
        floatingFilter: true,
        editable: (params) => params.data?.__isNew ? true : canEdit,
        cellStyle: (params) => {
          if (isPK) {
            return { fontWeight: 600, background: '#fef3c7', color: '#92400e' }
          }
          const rowKey = getRowKey(params.data)
          if (rowKey && pendingChangesRef.current.has(rowKey)) {
            const entry = pendingChangesRef.current.get(rowKey)
            if (entry.updates && Object.prototype.hasOwnProperty.call(entry.updates, field)) {
              return { background: '#bbf7d0', fontWeight: 500 }
            }
          }
          return null
        },
        minWidth: 100,
        flex: 1,
        filterParams: {
          filterOptions: ['contains', 'equals', 'startsWith', 'endsWith'],
          defaultOption: 'contains',
          debounceMs: 300,
        },
      }
    })
    
    return [checkboxCol, ...dataCols]
  }, [schema, hasPermission, getRowKey, changesVersion])

  const defaultColDef = useMemo(() => ({
    minWidth: 80,
    filter: true,
    floatingFilter: true,
  }), [])

  const onCellValueChanged = useCallback((params) => {
    // For new rows, try auto-save when all PKs are filled
    if (params.data?.__isNew) {
      handleSaveNewRow(params)
      return
    }

    if (pkColumns.length === 0) {
      toast.error('No primary key defined')
      return
    }

    const rowKey = getRowKey(params.data)
    if (!rowKey) return

    const pkValues = {}
    pkColumns.forEach(pk => { pkValues[pk] = params.data[pk] })

    let entry = pendingChangesRef.current.get(rowKey)
    if (!entry) {
      entry = {
        primary_key_columns: [...pkColumns],
        primary_key_values: { ...pkValues },
        updates: {}
      }
    }

    entry.updates[params.colDef.field] = params.newValue
    pendingChangesRef.current.set(rowKey, entry)
    setPendingCount(pendingChangesRef.current.size)
    params.api.refreshCells({ rowNodes: [params.node], force: true })
  }, [pkColumns, getRowKey])

  const handleSaveAll = async () => {
    if (pendingChangesRef.current.size === 0) return toast.info('No changes to save')
    
    setSaving(true)
    setLoading(true)
    let success = 0, failed = 0
    const errors = []

    for (const change of pendingChangesRef.current.values()) {
      try {
        await dataAPI.update({
          table_name: selectedTable,
          primary_key_columns: change.primary_key_columns,
          primary_key_values: change.primary_key_values,
          updates: change.updates,
        })
        success++
      } catch (err) {
        failed++
        const detail = err.response?.data?.detail || 'Unknown error'
        if (!errors.includes(detail)) errors.push(detail)
      }
    }

    if (success > 0) {
      toast.success(`${success} record(s) saved`)
      // Auto-stamp checklist so freshness updates
      checklistAPI.stamp(selectedTable).catch(() => {})
    }
    if (failed > 0) toast.error(`${failed} failed: ${errors[0]}`)

    pendingChangesRef.current.clear()
    setPendingCount(0)
    setChangesVersion(v => v + 1)
    await loadData()
    setSaving(false)
    setLoading(false)
  }

  const handleDiscardChanges = () => {
    pendingChangesRef.current.clear()
    setChangesVersion(v => v + 1)
    setPendingCount(0)
    loadData()
  }

  const handleDelete = async () => {
    if (!gridRef.current?.api) return toast.error('Grid not ready')
    
    const selectedRows = gridRef.current.api.getSelectedRows()
    if (!selectedRows || selectedRows.length === 0) {
      return toast.error('Select rows using checkboxes first')
    }
    
    if (pkColumns.length === 0) return toast.error('No primary key defined')
    if (!confirm(`Delete ${selectedRows.length} row(s)? This cannot be undone.`)) return

    setLoading(true)
    let deleted = 0, failed = 0
    
    try {
      for (const row of selectedRows) {
        try {
          const pkValues = {}
          pkColumns.forEach(pk => { pkValues[pk] = row[pk] })
          await dataAPI.delete({
            table_name: selectedTable,
            primary_key_columns: pkColumns,
            primary_key_values: [pkValues]
          })
          deleted++
        } catch { failed++ }
      }
      
      if (deleted > 0) toast.success(`${deleted} row(s) deleted`)
      if (failed > 0) toast.error(`${failed} row(s) failed`)
      loadData()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Delete failed')
    } finally {
      setLoading(false)
    }
  }

  const exportCSV = () => {
    gridRef.current?.api?.exportDataAsCsv({ fileName: `${selectedTable}.csv` })
  }

  const handleAddRow = () => {
    // Add an empty row at the top of the grid for inline editing
    const emptyRow = {}
    schema?.columns?.forEach(c => { emptyRow[c.column_name] = '' })
    emptyRow.__isNew = true
    setRowData(prev => [emptyRow, ...prev])
    toast.success('New row added — fill values and double-click cells to edit')
    // Focus first cell after render
    setTimeout(() => {
      gridRef.current?.api?.setFocusedCell(0, schema?.columns?.[0]?.column_name)
      gridRef.current?.api?.startEditingCell({ rowIndex: 0, colKey: schema?.columns?.[0]?.column_name })
    }, 100)
  }

  const handleSaveNewRow = async (params) => {
    if (!params.data?.__isNew) return
    const pkCols = schema?.columns?.filter(c => c.is_primary_key).map(c => c.column_name) || []
    if (pkCols.length === 0) return
    const missingPk = pkCols.filter(pk => !params.data[pk] || !String(params.data[pk]).trim())
    if (missingPk.length > 0) return // Not ready yet, still editing
    try {
      const record = { ...params.data }
      delete record.__isNew
      await dataAPI.upsert({
        table_name: selectedTable,
        primary_key_columns: pkCols,
        records: [record],
      })
      toast.success('Row saved')
      params.data.__isNew = false
      handleLoadData()
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to save row') }
  }

  const totalPages = Math.ceil(total / pageSize)
  const activeFilterCount = Object.values(preFilters).filter(arr => arr && arr.length > 0).length

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        {fromChecklist && (
          <button onClick={() => navigate('/data-validation/checklist')}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-indigo-200 bg-indigo-50 text-indigo-600 hover:bg-indigo-100 transition-colors">
            <ArrowLeft size={13}/> Back to Checklist
          </button>
        )}
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Data Editor</h1>
          <p className="text-gray-500 text-sm">View, filter, edit and save data directly in the browser</p>
        </div>
      </div>

      {/* Table Selection */}
      <div className="card p-4">
        <div className="flex items-center gap-4 flex-wrap">
          <div className="flex-1 max-w-md relative" ref={tableDropdownRef}>
            <label className="text-xs font-semibold text-gray-600 mb-1 block uppercase tracking-wide">Select Table</label>
            <div className="relative">
              <input
                type="text"
                value={showTableDropdown ? tableSearch : selectedTable}
                onChange={(e) => { setTableSearch(e.target.value); setShowTableDropdown(true); setHighlightedIndex(0) }}
                onFocus={() => { setShowTableDropdown(true); setHighlightedIndex(0) }}
                onKeyDown={(e) => {
                  const filteredTables = tables.filter(t => (t.table_name || t).toLowerCase().includes(tableSearch.toLowerCase())).slice(0, 100)
                  if (e.key === 'ArrowDown') {
                    e.preventDefault()
                    setHighlightedIndex(prev => Math.min(prev + 1, filteredTables.length - 1))
                  } else if (e.key === 'ArrowUp') {
                    e.preventDefault()
                    setHighlightedIndex(prev => Math.max(prev - 1, 0))
                  } else if (e.key === 'Enter' && highlightedIndex >= 0 && filteredTables[highlightedIndex]) {
                    e.preventDefault()
                    const name = filteredTables[highlightedIndex].table_name || filteredTables[highlightedIndex]
                    setSelectedTable(name)
                    setTableSearch('')
                    setShowTableDropdown(false)
                    setHighlightedIndex(-1)
                  } else if (e.key === 'Escape') {
                    setShowTableDropdown(false)
                    setHighlightedIndex(-1)
                  }
                }}
                placeholder="Search and select a table..."
                className="input pr-10"
              />
              <Search size={16} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400" />
            </div>
            {showTableDropdown && (
              <div className="absolute z-50 w-full mt-1 bg-white border rounded-xl shadow-2xl max-h-72 overflow-y-auto">
                {tables
                  .filter(t => (t.table_name || t).toLowerCase().includes(tableSearch.toLowerCase()))
                  .slice(0, 100)
                  .map((t, index) => {
                    const name = t.table_name || t
                    const isHighlighted = index === highlightedIndex
                    return (
                      <button
                        key={name}
                        onClick={() => {
                          setSelectedTable(name)
                          setTableSearch('')
                          setShowTableDropdown(false)
                          setHighlightedIndex(-1)
                        }}
                        onMouseEnter={() => setHighlightedIndex(index)}
                        className={`w-full text-left px-4 py-2.5 text-sm transition-colors ${
                          isHighlighted ? 'bg-blue-100 text-blue-700' : ''
                        } ${
                          selectedTable === name ? 'font-medium' : ''
                        }`}
                      >
                        {name}
                      </button>
                    )
                  })}
                {tables.filter(t => (t.table_name || t).toLowerCase().includes(tableSearch.toLowerCase())).length === 0 && (
                  <div className="px-4 py-4 text-sm text-gray-400 text-center">No tables found</div>
                )}
              </div>
            )}
          </div>
          
          {selectedTable && dataLoaded && (
            <div className="flex items-center gap-2 pt-5">
              <button onClick={handleLoadData} disabled={loading} className="btn-ghost btn-sm">
                <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh
              </button>
              <button onClick={handleAddRow} className="btn-primary btn-sm" disabled={!schema}>
                <Plus size={14} /> Add Row
              </button>
              <button onClick={exportCSV} className="btn-secondary btn-sm" disabled={rowData.length === 0}>
                <Download size={14} /> Export
              </button>
              {hasPermission('DATA_DELETE') && (
                <button onClick={handleDelete} className="btn-secondary btn-sm text-red-600">
                  <Trash2 size={14} /> Delete Selected
                </button>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Pre-Load Panel */}
      {selectedTable && !dataLoaded && (
        <div className="card p-6">
          {/* Table Info Header */}
          <div className="flex items-start gap-4 mb-6">
            <div className="p-3 bg-gradient-to-br from-blue-500 to-blue-600 rounded-xl shadow-lg">
              <Database size={28} className="text-white" />
            </div>
            <div className="flex-1">
              <h3 className="font-bold text-gray-800 text-xl">{selectedTable}</h3>
              <div className="flex items-center gap-4 mt-2">
                <div className="text-gray-600">
                  <span className="text-sm">Total Rows:</span>
                  <span className="ml-2 text-lg font-bold text-gray-900">{totalRowCount.toLocaleString()}</span>
                </div>
                {filteredRowCount !== null && (
                  <div className="text-blue-600">
                    <span className="text-sm">After Filters:</span>
                    <span className="ml-2 text-lg font-bold">{filteredRowCount.toLocaleString()}</span>
                  </div>
                )}
              </div>
              
              {totalRowCount > 100000 && (
                <div className="flex items-center gap-2 mt-3 text-amber-600 text-sm bg-amber-50 px-3 py-2 rounded-lg w-fit">
                  <AlertTriangle size={16} />
                  Large table - consider applying filters to load faster
                </div>
              )}
            </div>
          </div>

          {/* Filter Section */}
          <div className="border-t pt-4">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Filter size={16} className="text-gray-500" />
                <span className="font-semibold text-gray-700">Pre-load Filters</span>
                <span className="text-xs text-gray-400">(Optional: filter data before loading)</span>
              </div>
              <button 
                onClick={() => setShowAddColumnModal(true)}
                className="btn-secondary btn-sm"
                disabled={!schema}
              >
                <Plus size={14} /> Add Filter Column
              </button>
            </div>
            
            {filterColumns.length === 0 ? (
              <div className="bg-gray-50 rounded-lg p-6 text-center">
                <Filter size={32} className="mx-auto mb-2 text-gray-300" />
                <p className="text-gray-500 text-sm">No filters added. Click "Add Filter Column" to add filters.</p>
                <p className="text-gray-400 text-xs mt-1">You can load data without filters too.</p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex flex-wrap gap-4">
                  {filterColumns.map(col => (
                    <MultiSelectFilterDropdown
                      key={col}
                      column={col}
                      selectedValues={preFilters[col] || []}
                      onChange={handlePreFilterChange}
                      tableName={selectedTable}
                      currentFilters={preFilters}
                      disabled={loading}
                      onRemove={handleRemoveFilterColumn}
                    />
                  ))}
                </div>
                
                {activeFilterCount > 0 && (
                  <button 
                    onClick={handleClearAllFilters} 
                    className="text-sm text-gray-500 hover:text-red-500 flex items-center gap-1"
                  >
                    <X size={14} /> Clear all filters
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Load Button */}
          <div className="mt-6 pt-4 border-t">
            <button
              onClick={handleLoadData}
              disabled={loading}
              className="btn-primary px-8 py-3 text-base flex items-center gap-3"
            >
              {loading ? <Loader2 size={20} className="animate-spin" /> : <Play size={20} />}
              Load Data
              <span className="text-blue-200 text-sm">
                ({(filteredRowCount ?? totalRowCount).toLocaleString()} rows)
              </span>
            </button>
          </div>
        </div>
      )}

      {/* Pending Changes Bar */}
      {pendingCount > 0 && (
        <div className="card p-3 bg-green-50 border-green-200">
          <div className="flex items-center justify-between">
            <span className="text-green-700 font-medium flex items-center gap-2">
              <span className="w-2.5 h-2.5 bg-green-500 rounded-full animate-pulse"></span>
              {pendingCount} unsaved change(s)
            </span>
            <div className="flex gap-2">
              <button onClick={handleDiscardChanges} className="btn-secondary btn-sm">
                <X size={14} /> Discard
              </button>
              <button onClick={handleSaveAll} disabled={saving} className="btn-primary btn-sm">
                {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                Save All
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Data Grid */}
      {selectedTable && dataLoaded && (
        <>
          <div className="card overflow-hidden">
            <div className="flex items-center justify-between p-3 bg-gray-50 border-b">
              <div className="flex items-center gap-4 text-sm">
                <span className="font-semibold text-gray-700">{selectedTable}</span>
                <span className="text-gray-500">{total.toLocaleString()} rows loaded</span>
                {activeFilterCount > 0 && (
                  <span className="px-2 py-0.5 bg-blue-100 text-blue-700 text-xs rounded-full font-medium">
                    {activeFilterCount} pre-filter(s) applied
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3">
                <span className="flex items-center gap-1 text-xs text-amber-700 bg-amber-50 px-2 py-1 rounded">
                  <span className="w-2 h-2 bg-amber-300 rounded"></span> PK
                </span>
                {hasPermission('DATA_EDIT') && (
                  <span className="flex items-center gap-1 text-xs text-gray-500">
                    <Edit3 size={11} /> Double-click to edit
                  </span>
                )}
              </div>
            </div>
            <div className="ag-theme-alpine" style={{ width: '100%', height: 'calc(100vh - 380px)', minHeight: '400px' }}>
              <AgGridReact
                ref={gridRef}
                rowData={rowData}
                columnDefs={columnDefs}
                defaultColDef={defaultColDef}
                onCellValueChanged={onCellValueChanged}
                getRowStyle={(params) => {
                  if (params.data?.__isNew) return { background: '#eef2ff', borderLeft: '3px solid #4f46e5' }
                  const rowKey = getRowKey(params.data)
                  if (rowKey && pendingChangesRef.current.has(rowKey)) return { background: '#f0fdf4' }
                  return undefined
                }}
                animateRows
                undoRedoCellEditing
                undoRedoCellEditingLimit={20}
                enableCellChangeFlash
                pagination={false}
                rowSelection="multiple"
                suppressRowClickSelection
                getRowId={(params) => getRowKey(params.data) || String(Math.random())}
                stopEditingWhenCellsLoseFocus
              />
            </div>
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <div className="text-sm text-gray-500">
                Page {page} of {totalPages || 1} • Showing {((page - 1) * pageSize) + 1}–{Math.min(page * pageSize, total)} of {total.toLocaleString()}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-500">Rows:</span>
                <select
                  value={pageSize}
                  onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1) }}
                  className="text-sm border border-gray-300 rounded px-2 py-1 bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value={100}>100</option>
                  <option value={250}>250</option>
                  <option value={500}>500</option>
                  <option value={1000}>1,000</option>
                  <option value={2000}>2,000</option>
                  <option value={5000}>5,000</option>
                </select>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button disabled={page <= 1 || loading} onClick={() => setPage(1)} className="btn-secondary btn-sm">First</button>
              <button disabled={page <= 1 || loading} onClick={() => setPage(p => p - 1)} className="btn-secondary btn-sm">Prev</button>
              <span className="px-3 py-1.5 bg-gray-100 text-gray-700 text-sm rounded font-medium">{page}</span>
              <button disabled={page >= totalPages || loading} onClick={() => setPage(p => p + 1)} className="btn-secondary btn-sm">Next</button>
              <button disabled={page >= totalPages || loading} onClick={() => setPage(totalPages)} className="btn-secondary btn-sm">Last</button>
            </div>
          </div>
        </>
      )}

      {/* Empty state */}
      {!selectedTable && (
        <div className="card p-16 text-center">
          <Database size={48} className="mx-auto mb-4 text-gray-300" />
          <h3 className="text-lg font-medium text-gray-600 mb-2">Select a Table</h3>
          <p className="text-gray-400">Choose a table from the dropdown above to view and edit data</p>
        </div>
      )}

      {/* Add Filter Column Modal */}
      {showAddColumnModal && schema && (
        <AddFilterColumnModal
          columns={schema.columns || []}
          existingFilters={filterColumns}
          onAdd={handleAddFilterColumn}
          onClose={() => setShowAddColumnModal(false)}
        />
      )}

    </div>
  )
}
