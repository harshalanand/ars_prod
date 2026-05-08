import { useState, useEffect, useRef, useCallback } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { Download, Filter, Plus, X, Search, RefreshCw, Settings, ChevronDown, ChevronLeft, ChevronRight, Database, Columns, Eye, List, FileDown, Bell, CheckCircle, Clock, AlertCircle, Trash2, Loader, ArrowLeft } from 'lucide-react'
import { tablesAPI } from '@/services/api'
import toast from 'react-hot-toast'

const previewRowOptions = [50, 100, 200, 500, 1000, 2000, 5000]
const filterOperators = [
  { value: 'equals', label: '= Equals' },
  { value: 'notEqual', label: '≠ Not Equal' },
  { value: 'contains', label: '∋ Contains' },
  { value: 'startsWith', label: 'A.. Starts' },
  { value: 'endsWith', label: '..Z Ends' },
  { value: 'greaterThan', label: '> Greater' },
  { value: 'lessThan', label: '< Less' },
  { value: 'between', label: '↔ Between' },
  { value: 'in', label: '∈ In (Multi)' },
  { value: 'blank', label: '∅ Blank' },
  { value: 'notBlank', label: '✓ Not Blank' },
]

// Dropdown with search and keyboard navigation
// Dropdown with search and keyboard navigation - Using input-based approach like DataEditorPage
function SearchDropdown({ options, value, onChange, placeholder, icon: Icon }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [highlightedIndex, setHighlightedIndex] = useState(0)
  const [dropdownStyle, setDropdownStyle] = useState({})
  const containerRef = useRef()
  const inputRef = useRef()
  const listRef = useRef()
  
  // Close dropdown when clicking outside
  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
        setSearch('')
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])
  
  const filtered = options.filter(o => String(o).toLowerCase().includes(search.toLowerCase())).slice(0, 100)
  
  // Reset highlighted index when filtered list changes
  useEffect(() => {
    setHighlightedIndex(0)
  }, [search])

  // Calculate dropdown position when opening
  useEffect(() => {
    if (open && inputRef.current) {
      const rect = inputRef.current.getBoundingClientRect()
      const spaceBelow = window.innerHeight - rect.bottom
      const spaceAbove = rect.top
      const dropdownHeight = Math.min(260, filtered.length * 26 + 16)
      
      if (spaceBelow >= dropdownHeight || spaceBelow >= spaceAbove) {
        setDropdownStyle({
          top: rect.bottom + 2,
          left: rect.left,
          width: Math.max(rect.width, 180),
          maxHeight: Math.min(260, spaceBelow - 10)
        })
      } else {
        setDropdownStyle({
          bottom: window.innerHeight - rect.top + 2,
          left: rect.left,
          width: Math.max(rect.width, 180),
          maxHeight: Math.min(260, spaceAbove - 10)
        })
      }
    }
  }, [open, filtered.length])
  
  // Scroll highlighted item into view
  useEffect(() => {
    if (open && listRef.current && listRef.current.children[highlightedIndex]) {
      listRef.current.children[highlightedIndex].scrollIntoView({ block: 'nearest' })
    }
  }, [highlightedIndex, open])
  
  const handleKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (!open) setOpen(true)
      else setHighlightedIndex(prev => Math.min(prev + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlightedIndex(prev => Math.max(prev - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (filtered[highlightedIndex]) {
        onChange(filtered[highlightedIndex])
        setOpen(false)
        setSearch('')
      }
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setOpen(false)
      setSearch('')
    } else if (e.key === 'Tab') {
      setOpen(false)
      setSearch('')
    }
  }

  const handleClear = (e) => {
    e.stopPropagation()
    e.preventDefault()
    onChange('')
    setSearch('')
    inputRef.current?.focus()
  }
  
  return (
    <div className="relative" ref={containerRef}>
      <div className="relative">
        {Icon && <Icon size={11} className="absolute left-1.5 top-1/2 -translate-y-1/2 text-blue-600 pointer-events-none" />}
        <input
          ref={inputRef}
          type="text"
          value={open ? search : value}
          onChange={(e) => { setSearch(e.target.value); setHighlightedIndex(0) }}
          onFocus={() => { setOpen(true); setHighlightedIndex(0); setSearch('') }}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className={`h-7 ${Icon ? 'pl-5' : 'pl-2'} pr-6 bg-white border rounded text-[11px] min-w-[120px] max-w-[180px] hover:border-blue-400 focus:border-blue-400 focus:outline-none`}
          autoComplete="off"
        />
        {value && !open ? (
          <X size={10} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-red-500 cursor-pointer" onClick={handleClear} />
        ) : (
          <ChevronDown size={10} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
        )}
      </div>
      {open && (
        <div 
          style={{ position: 'fixed', zIndex: 9999, ...dropdownStyle }}
          className="bg-white border rounded-lg shadow-2xl overflow-hidden"
        >
          <div className="overflow-y-auto" style={{ maxHeight: dropdownStyle.maxHeight || 180 }} ref={listRef}>
            {filtered.length === 0 ? <div className="p-2 text-center text-gray-400 text-[11px]">No results</div> :
              filtered.map((opt, i) => (
                <div 
                  key={i}
                  onMouseDown={(e) => { e.preventDefault(); onChange(opt); setOpen(false); setSearch('') }}
                  onMouseEnter={() => setHighlightedIndex(i)}
                  className={`w-full text-left px-2 py-1 text-[11px] cursor-pointer flex items-center gap-1
                    ${highlightedIndex === i ? 'bg-blue-500 text-white' : 'hover:bg-gray-100'} 
                    ${value === opt && highlightedIndex !== i ? 'bg-blue-50 text-blue-600 font-medium' : ''}`}
                >
                  {highlightedIndex === i && <span className="text-[9px]">▶</span>}
                  <span>{opt}</span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Column selector dropdown
function ColumnSelector({ columns, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const ref = useRef()
  
  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])
  
  const filtered = columns.filter(c => c.toLowerCase().includes(search.toLowerCase()))
  const toggleAll = () => onChange(selected.length === columns.length ? [] : [...columns])
  const toggle = (c) => onChange(selected.includes(c) ? selected.filter(x => x !== c) : [...selected, c])
  
  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)} className="h-8 px-2.5 bg-white border rounded-lg flex items-center gap-1.5 hover:border-blue-400">
        <Columns size={14} className="text-green-600" />
        <span className="text-xs">{selected.length}/{columns.length} Columns</span>
        <ChevronDown size={12} className="text-gray-400" />
      </button>
      {open && (
        <div className="absolute z-50 mt-1 w-56 bg-white border rounded-lg shadow-xl overflow-hidden">
          <div className="p-1.5 border-b flex items-center gap-2">
            <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search..." className="flex-1 h-6 px-2 text-xs border rounded" />
            <button onClick={toggleAll} className="text-[10px] text-blue-600 hover:underline whitespace-nowrap">
              {selected.length === columns.length ? 'None' : 'All'}
            </button>
          </div>
          <div className="max-h-64 overflow-y-auto p-1">
            {filtered.map(col => (
              <label key={col} className="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 rounded cursor-pointer">
                <input type="checkbox" checked={selected.includes(col)} onChange={() => toggle(col)} className="w-3.5 h-3.5" />
                <span className="text-xs truncate">{col}</span>
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Multi-select for filter values
function MultiSelect({ values, selected, onChange, loading, onOpen }) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const ref = useRef()

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleOpen = () => { setOpen(true); if (onOpen) onOpen() }
  const toggle = (v) => onChange(selected.includes(v) ? selected.filter(x => x !== v) : [...selected, v])
  const filtered = values.filter(v => String(v).toLowerCase().includes(search.toLowerCase()))

  return (
    <div className="relative flex-1" ref={ref}>
      <div onClick={handleOpen} className="h-7 px-2 text-xs border rounded bg-white cursor-pointer flex items-center justify-between">
        <span className="truncate">{selected.length ? `${selected.length} selected` : 'Select...'}</span>
        <ChevronDown size={10} />
      </div>
      {open && (
        <div className="absolute z-50 w-48 mt-1 bg-white border rounded-lg shadow-xl max-h-48 overflow-hidden">
          <div className="p-1 border-b">
            <input type="text" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search..." className="w-full h-6 px-2 text-xs border rounded" onClick={e => e.stopPropagation()} />
          </div>
          <div className="max-h-32 overflow-y-auto">
            {loading ? <div className="p-2 text-center text-gray-400 text-xs">Loading...</div> :
             filtered.length === 0 ? <div className="p-2 text-center text-gray-400 text-xs">No values</div> :
             filtered.slice(0, 50).map((v, i) => (
              <div key={i} onClick={() => toggle(v)} className="px-2 py-1 cursor-pointer hover:bg-blue-50 flex items-center gap-1.5 text-xs">
                <input type="checkbox" checked={selected.includes(v)} onChange={() => {}} className="w-3 h-3" />
                <span className="truncate">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// Job status badge
function JobStatusBadge({ status }) {
  const config = {
    pending: { icon: Clock, color: 'text-yellow-600 bg-yellow-50', label: 'Pending' },
    running: { icon: Loader, color: 'text-blue-600 bg-blue-50', label: 'Running' },
    completed: { icon: CheckCircle, color: 'text-green-600 bg-green-50', label: 'Done' },
    failed: { icon: AlertCircle, color: 'text-red-600 bg-red-50', label: 'Failed' },
  }
  const { icon: Icon, color, label } = config[status] || config.pending
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium ${color}`}>
      <Icon size={10} className={status === 'running' ? 'animate-spin' : ''} /> {label}
    </span>
  )
}

// Format file size
function formatSize(bytes) {
  if (!bytes) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Format date
function formatDate(iso) {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

export default function ExportPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const fromChecklist = searchParams.get('from') === 'checklist'
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(searchParams.get('table') || '')
  const [schema, setSchema] = useState(null)
  const [selectedColumns, setSelectedColumns] = useState([])
  const [format, setFormat] = useState('xlsx')
  const [filters, setFilters] = useState([])
  const [distinctValues, setDistinctValues] = useState({})
  const [loadingDistinct, setLoadingDistinct] = useState({})
  const [exporting, setExporting] = useState(false)
  const [preview, setPreview] = useState(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewRows, setPreviewRows] = useState(100)
  const [currentPage, setCurrentPage] = useState(1)
  const [showSettings, setShowSettings] = useState(false)
  const [exportSettings, setExportSettings] = useState({})
  const [showFilters, setShowFilters] = useState(false)
  const [showJobs, setShowJobs] = useState(false)
  const [jobs, setJobs] = useState([])
  const [jobsLoading, setJobsLoading] = useState(false)
  const jobsRef = useRef()

  useEffect(() => {
    // Load tables with export permission
    Promise.all([
      tablesAPI.listAll(),
      tablesAPI.allowedTables('export').catch(() => ({ data: { data: [] } }))
    ]).then(([allRes, allowedRes]) => {
      const all = allRes.data.data || []
      const allowed = allowedRes.data?.data || []
      // If permissions exist, filter; otherwise show all
      setTables(allowed.length > 0 ? all.filter(t => allowed.includes(t.table_name)) : all)
    }).catch(() => {})
    loadExportSettings()
    loadJobs()
  }, [])

  // Poll running jobs every 3 seconds
  useEffect(() => {
    const hasRunning = jobs.some(j => j.status === 'running' || j.status === 'pending')
    if (!hasRunning) return
    const interval = setInterval(loadJobs, 3000)
    return () => clearInterval(interval)
  }, [jobs])

  // Close jobs dropdown on outside click
  useEffect(() => {
    const handler = (e) => { if (jobsRef.current && !jobsRef.current.contains(e.target)) setShowJobs(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const loadExportSettings = async () => {
    try {
      const { data } = await tablesAPI.exportSettings()
      const settings = {}
      data.data?.forEach(s => { settings[s.key] = s.value })
      setExportSettings(settings)
    } catch {}
  }

  const loadJobs = async () => {
    try {
      const { data } = await tablesAPI.listExportJobs(20)
      setJobs(data.data || [])
    } catch {}
  }

  useEffect(() => {
    if (selectedTable) {
      tablesAPI.schema(selectedTable).then(r => {
        const s = r.data.data
        setSchema(s)
        // Exclude system/auto-update columns by default
        const sysCols = ['id', 'created_at', 'updated_at', 'created_by', 'updated_by', 'modified_at', 'modified_by', 'upload_datetime', 'upload_date', 'upload_time', 'uploaded_at', 'uploaded_by', 'insert_date', 'insert_datetime']
        const cols = (s?.columns || []).filter(c => {
          const name = c.column_name?.toLowerCase() || ''
          const def_ = (c.default_value || '').toLowerCase()
          const isAuto = def_.includes('getdate') || def_.includes('current_timestamp') || def_.includes('sysdatetime')
          return !c.is_identity && !c.is_computed && !sysCols.includes(name) && !isAuto
        }).map(c => c.column_name)
        setSelectedColumns(cols)
        setFilters([])
        setDistinctValues({})
        setPreview(null)
        setCurrentPage(1)
      }).catch(() => {})
    } else {
      setSchema(null)
      setSelectedColumns([])
      setFilters([])
      setPreview(null)
    }
  }, [selectedTable])

  const loadDistinctValues = useCallback(async (column) => {
    if (!selectedTable || distinctValues[column]) return
    setLoadingDistinct(prev => ({ ...prev, [column]: true }))
    try {
      const { data } = await tablesAPI.distinct(selectedTable, column, { limit: 300 })
      setDistinctValues(prev => ({ ...prev, [column]: data.data || [] }))
    } catch {
      setDistinctValues(prev => ({ ...prev, [column]: [] }))
    } finally {
      setLoadingDistinct(prev => ({ ...prev, [column]: false }))
    }
  }, [selectedTable, distinctValues])

  const addFilter = () => {
    if (!schema?.columns?.length) return
    const col = schema.columns[0].column_name
    setFilters([...filters, { column: col, operator: 'equals', value: '', values: [], from: '', to: '' }])
    loadDistinctValues(col)
  }

  const updateFilter = (idx, field, value) => {
    const updated = [...filters]
    updated[idx][field] = value
    if (field === 'column') {
      loadDistinctValues(value)
      updated[idx].value = ''
      updated[idx].values = []
    }
    setFilters(updated)
  }

  const removeFilter = (idx) => setFilters(filters.filter((_, i) => i !== idx))

  const buildFilterObject = () => {
    const obj = {}
    filters.forEach(f => {
      if (f.operator === 'blank' || f.operator === 'notBlank') obj[f.column] = { type: f.operator }
      else if (f.operator === 'between' && f.from && f.to) obj[f.column] = { type: 'between', from: f.from, to: f.to }
      else if (f.operator === 'in' && f.values?.length > 0) obj[f.column] = { type: 'in', filter: f.values }
      else if (f.value) obj[f.column] = { type: f.operator, filter: f.value }
    })
    return obj
  }

  const handlePreview = async (page = 1) => {
    if (!selectedTable) return toast.error('Select a table first')
    setPreviewLoading(true)
    setCurrentPage(page)
    try {
      const filterObj = buildFilterObject()
      const { data } = await tablesAPI.data(selectedTable, { 
        page, page_size: previewRows,
        filters: Object.keys(filterObj).length ? JSON.stringify(filterObj) : undefined
      })
      setPreview(data.data)
    } catch { toast.error('Preview failed') }
    finally { setPreviewLoading(false) }
  }

  const handleExport = async () => {
    if (!selectedTable) return toast.error('Select a table')
    if (selectedColumns.length === 0) return toast.error('Select columns')
    setExporting(true)
    try {
      const filterObj = buildFilterObject()
      await tablesAPI.startExportJob({
        table_name: selectedTable,
        format,
        columns: selectedColumns,
        filters: filterObj
      })
      toast.success('Export job started! Check Jobs panel for progress.')
      loadJobs()
      setShowJobs(true)
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to start export') }
    finally { setExporting(false) }
  }

  const handleDownload = async (jobId) => {
    const url = tablesAPI.downloadExportJob(jobId)
    const token = localStorage.getItem('access_token')

    try {
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${token}` },
      })

      if (!res.ok) {
        throw new Error('Download failed')
      }

      const blob = await res.blob()
      const contentDisposition = res.headers.get('content-disposition') || ''
      const match = contentDisposition.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i)
      const filename = match ? decodeURIComponent(match[1]) : `export_${jobId}`

      const objectUrl = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = objectUrl
      a.download = filename
      a.click()
      URL.revokeObjectURL(objectUrl)
    } catch (e) {
      toast.error('Download failed')
    }
  }

  const handleDeleteJob = async (jobId) => {
    try {
      await tablesAPI.deleteExportJob(jobId)
      loadJobs()
      toast.success('Job deleted')
    } catch { toast.error('Failed to delete') }
  }

  const saveExportSettings = async () => {
    try {
      await tablesAPI.updateExportSettings(exportSettings)
      toast.success('Settings saved')
      setShowSettings(false)
    } catch { toast.error('Failed to save') }
  }

  const totalPages = preview ? Math.ceil(preview.total / previewRows) : 0
  const columnList = schema?.columns?.map(c => c.column_name) || []
  const tableNames = tables.map(t => t.table_name || t)
  const runningCount = jobs.filter(j => j.status === 'running' || j.status === 'pending').length

  return (
    <div className="h-[calc(100vh-56px)] flex flex-col -m-6 bg-slate-50">
      {/* Top Toolbar */}
      <div className="bg-white border-b px-3 py-1.5 flex items-center gap-2.5 flex-wrap">
        {/* Back to Checklist */}
        {fromChecklist && (
          <button onClick={() => navigate('/data-validation/checklist')}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-indigo-200 bg-indigo-50 text-indigo-600 hover:bg-indigo-100 transition-colors">
            <ArrowLeft size={13}/> Back to Checklist
          </button>
        )}
        {/* Title */}
        <div className="flex items-center gap-1.5 mr-1.5">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center shadow">
            <FileDown size={14} className="text-white" />
          </div>
          <span className="font-semibold text-[13px] text-gray-800">Export</span>
        </div>

        {/* Table Selector */}
        <SearchDropdown options={tableNames} value={selectedTable} onChange={setSelectedTable} placeholder="Select Table" icon={Database} />
        
        {/* Format */}
        <select value={format} onChange={e => setFormat(e.target.value)} className="h-8 px-2 text-xs border rounded-lg bg-white">
          <option value="xlsx">Excel (.xlsx)</option>
          <option value="csv">CSV (.csv)</option>
        </select>

        {/* Columns */}
        {schema && <ColumnSelector columns={columnList} selected={selectedColumns} onChange={setSelectedColumns} />}
        
        {/* Filters Toggle */}
        <button onClick={() => setShowFilters(!showFilters)} className={`h-8 px-2.5 border rounded-lg flex items-center gap-1.5 text-xs ${showFilters ? 'bg-purple-50 border-purple-300 text-purple-700' : 'bg-white hover:border-purple-300'}`}>
          <Filter size={14} className={showFilters ? 'text-purple-600' : 'text-gray-500'} />
          Filters {filters.length > 0 && <span className="bg-purple-500 text-white text-[10px] px-1.5 rounded-full">{filters.length}</span>}
        </button>

        <div className="flex-1" />

        {/* Actions */}
        <select value={previewRows} onChange={e => setPreviewRows(Number(e.target.value))} className="h-8 px-2 text-xs border rounded-lg bg-white">
          {previewRowOptions.map(n => <option key={n} value={n}>{n.toLocaleString()} rows</option>)}
        </select>
        <button onClick={() => handlePreview(1)} disabled={!selectedTable || previewLoading} className="btn-secondary h-8 px-3 text-xs">
          {previewLoading ? <RefreshCw size={14} className="animate-spin" /> : <Eye size={14} />} Preview
        </button>
        <button onClick={handleExport} disabled={!selectedTable || exporting || !selectedColumns.length} className="btn-primary h-8 px-4 text-xs">
          {exporting ? <RefreshCw size={14} className="animate-spin" /> : <Download size={14} />} Export
        </button>
        
        {/* Jobs Button */}
        <div className="relative" ref={jobsRef}>
          <button onClick={() => { setShowJobs(!showJobs); loadJobs() }} className="h-8 px-2.5 border rounded-lg flex items-center gap-1.5 text-xs bg-white hover:border-blue-300 relative">
            <Bell size={14} className="text-gray-600" />
            Jobs
            {runningCount > 0 && (
              <span className="absolute -top-1 -right-1 w-4 h-4 bg-blue-500 text-white text-[10px] rounded-full flex items-center justify-center animate-pulse">
                {runningCount}
              </span>
            )}
          </button>
          
          {/* Jobs Dropdown */}
          {showJobs && (
            <div className="absolute right-0 mt-1 w-96 bg-white border rounded-xl shadow-xl z-50 overflow-hidden">
              <div className="px-3 py-2 border-b bg-gray-50 flex items-center justify-between">
                <span className="text-sm font-medium text-gray-700">Export Jobs</span>
                <button onClick={loadJobs} className="text-gray-400 hover:text-blue-600">
                  <RefreshCw size={14} className={jobsLoading ? 'animate-spin' : ''} />
                </button>
              </div>
              <div className="max-h-80 overflow-y-auto">
                {jobs.length === 0 ? (
                  <div className="p-6 text-center text-gray-400 text-sm">No export jobs</div>
                ) : (
                  jobs.map(job => (
                    <div key={job.job_id} className="px-3 py-2 border-b hover:bg-gray-50">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-xs font-medium text-gray-800 truncate max-w-[150px]">{job.table_name}</span>
                        <JobStatusBadge status={job.status} />
                      </div>
                      <div className="flex items-center justify-between text-[10px] text-gray-500">
                        <span>{formatDate(job.created_at)}</span>
                        <span>{job.total_rows?.toLocaleString() || '?'} rows • {formatSize(job.file_size)}</span>
                      </div>
                      {job.status === 'running' && job.total_rows > 0 && (
                        <div className="mt-1.5">
                          <div className="h-1 bg-gray-200 rounded-full overflow-hidden">
                            <div className="h-full bg-blue-500 transition-all" style={{ width: `${(job.processed_rows / job.total_rows * 100).toFixed(0)}%` }} />
                          </div>
                          <div className="text-[9px] text-gray-400 mt-0.5">{job.processed_rows?.toLocaleString()} / {job.total_rows?.toLocaleString()}</div>
                        </div>
                      )}
                      {job.status === 'failed' && job.error_message && (
                        <div className="mt-1 text-[10px] text-red-600 truncate">{job.error_message}</div>
                      )}
                      <div className="flex items-center gap-2 mt-1.5">
                        {job.status === 'completed' && (
                          <button onClick={() => handleDownload(job.job_id)} className="text-[10px] text-blue-600 hover:underline flex items-center gap-0.5">
                            <Download size={10} /> Download
                          </button>
                        )}
                        {job.status !== 'running' && (
                          <button onClick={() => handleDeleteJob(job.job_id)} className="text-[10px] text-red-500 hover:underline flex items-center gap-0.5">
                            <Trash2 size={10} /> Delete
                          </button>
                        )}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
        
        <button onClick={() => setShowSettings(true)} className="h-8 px-2 text-gray-500 hover:text-blue-600">
          <Settings size={16} />
        </button>
      </div>

      {/* Filters Panel */}
      {showFilters && (
        <div className="bg-purple-50/50 border-b px-4 py-2 flex items-center gap-2 flex-wrap">
          {filters.map((f, idx) => (
            <div key={idx} className="flex items-center gap-1 bg-white border rounded-lg px-2 py-1 shadow-sm">
              <select value={f.column} onChange={e => updateFilter(idx, 'column', e.target.value)} className="h-6 text-xs border-0 bg-transparent focus:ring-0 pr-1">
                {columnList.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
              <select value={f.operator} onChange={e => updateFilter(idx, 'operator', e.target.value)} className="h-6 text-xs border-0 bg-transparent focus:ring-0 px-1 w-24">
                {filterOperators.map(op => <option key={op.value} value={op.value}>{op.label}</option>)}
              </select>
              {f.operator === 'between' ? (
                <>
                  <input type="text" value={f.from || ''} onChange={e => updateFilter(idx, 'from', e.target.value)} placeholder="From" className="h-6 w-16 px-1 text-xs border rounded" />
                  <span className="text-xs text-gray-400">-</span>
                  <input type="text" value={f.to || ''} onChange={e => updateFilter(idx, 'to', e.target.value)} placeholder="To" className="h-6 w-16 px-1 text-xs border rounded" />
                </>
              ) : f.operator === 'in' ? (
                <MultiSelect values={distinctValues[f.column] || []} selected={f.values || []} onChange={vals => updateFilter(idx, 'values', vals)} loading={loadingDistinct[f.column]} onOpen={() => loadDistinctValues(f.column)} />
              ) : !['blank', 'notBlank'].includes(f.operator) && (
                <input type="text" value={f.value || ''} onChange={e => updateFilter(idx, 'value', e.target.value)} onFocus={() => loadDistinctValues(f.column)} placeholder="Value..." className="h-6 w-24 px-1 text-xs border rounded" list={`dl-${idx}`} />
              )}
              {distinctValues[f.column]?.length > 0 && <datalist id={`dl-${idx}`}>{distinctValues[f.column].slice(0, 50).map((v, i) => <option key={i} value={String(v)} />)}</datalist>}
              <button onClick={() => removeFilter(idx)} className="p-0.5 hover:bg-red-100 rounded"><X size={12} className="text-red-500" /></button>
            </div>
          ))}
          <button onClick={addFilter} disabled={!schema} className="h-7 px-2 text-xs text-purple-700 border border-purple-300 rounded-lg hover:bg-purple-100 flex items-center gap-1 disabled:opacity-50">
            <Plus size={12} /> Add Filter
          </button>
          {filters.length > 0 && <button onClick={() => setFilters([])} className="text-xs text-red-600 hover:underline ml-2">Clear All</button>}
        </div>
      )}

      {/* Preview Table - Full Page */}
      <div className="flex-1 overflow-hidden p-4">
        <div className="h-full bg-white rounded-xl shadow-sm border flex flex-col overflow-hidden">
          {/* Table Header */}
          <div className="px-4 py-2 border-b bg-gray-50 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Eye size={14} className="text-blue-600" />
              <span className="text-sm font-medium text-gray-700">
                Preview {preview && <span className="text-gray-500 font-normal">— {preview.total?.toLocaleString()} total rows</span>}
              </span>
            </div>
            {preview && totalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => handlePreview(currentPage - 1)} disabled={currentPage <= 1 || previewLoading} className="p-1 hover:bg-gray-200 rounded disabled:opacity-40">
                  <ChevronLeft size={16} />
                </button>
                <span className="text-xs text-gray-600 min-w-[80px] text-center">Page {currentPage} of {totalPages.toLocaleString()}</span>
                <button onClick={() => handlePreview(currentPage + 1)} disabled={currentPage >= totalPages || previewLoading} className="p-1 hover:bg-gray-200 rounded disabled:opacity-40">
                  <ChevronRight size={16} />
                </button>
              </div>
            )}
          </div>

          {/* Table Content */}
          <div className="flex-1 overflow-auto">
            {!selectedTable ? (
              <div className="h-full flex items-center justify-center text-gray-400">
                <div className="text-center">
                  <Database size={48} className="mx-auto mb-2 text-gray-300" />
                  <p className="text-sm">Select a table to begin</p>
                </div>
              </div>
            ) : !preview ? (
              <div className="h-full flex items-center justify-center text-gray-400">
                <div className="text-center">
                  <Eye size={48} className="mx-auto mb-2 text-gray-300" />
                  <p className="text-sm">Click Preview to see data</p>
                </div>
              </div>
            ) : previewLoading ? (
              <div className="h-full flex items-center justify-center">
                <div className="text-center">
                  <RefreshCw size={32} className="animate-spin text-blue-500 mx-auto mb-2" />
                  <p className="text-sm text-gray-500">Loading preview...</p>
                </div>
              </div>
            ) : (
              <table className="w-full text-xs">
                <thead className="bg-gray-100 sticky top-0">
                  <tr>
                    <th className="px-2 py-2 text-left font-semibold text-gray-500 border-b w-12">#</th>
                    {selectedColumns.map(col => (
                      <th key={col} className="px-3 py-2 text-left font-semibold text-gray-600 border-b whitespace-nowrap">{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.data?.map((row, i) => (
                    <tr key={i} className="border-b border-gray-100 hover:bg-blue-50/30">
                      <td className="px-2 py-1.5 text-gray-400 border-r border-gray-100">{(currentPage - 1) * previewRows + i + 1}</td>
                      {selectedColumns.map(col => (
                        <td key={col} className="px-3 py-1.5 truncate max-w-[200px]" title={row[col] != null ? String(row[col]) : ''}>
                          {row[col] != null ? String(row[col]) : <span className="text-gray-300">null</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Table Footer */}
          {preview && (
            <div className="px-4 py-2 border-t bg-gray-50 text-xs text-gray-500 flex items-center justify-between">
              <span>Showing {((currentPage - 1) * previewRows + 1).toLocaleString()}-{Math.min(currentPage * previewRows, preview.total).toLocaleString()} of {preview.total?.toLocaleString()} rows</span>
              <span>{selectedColumns.length} columns selected</span>
            </div>
          )}
        </div>
      </div>

      {/* Settings Modal */}
      {showSettings && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-2xl w-[500px] overflow-hidden">
            <div className="px-4 py-3 bg-gradient-to-r from-blue-600 to-blue-700 text-white font-semibold flex items-center gap-2">
              <Settings size={16} /> Export Settings
            </div>
            <div className="p-4 space-y-3 text-sm">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">Max Rows Per File</label>
                  <input type="number" value={exportSettings.max_rows_per_file || '100000'} onChange={e => setExportSettings(p => ({...p, max_rows_per_file: e.target.value}))} className="input" />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">Auto Split Large Files</label>
                  <select value={exportSettings.enable_auto_split || 'true'} onChange={e => setExportSettings(p => ({...p, enable_auto_split: e.target.value}))} className="input">
                    <option value="true">Enabled</option>
                    <option value="false">Disabled</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-xs text-gray-600 mb-1">Split Method</label>
                <select value={exportSettings.split_method || 'product'} onChange={e => setExportSettings(p => ({...p, split_method: e.target.value}))} className="input">
                  <option value="product">Product (SEG → DIV → SUB_DIV → MAJ_CAT)</option>
                  <option value="store">Store (ZONE → REG → STORE)</option>
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">Product Hierarchy (JSON)</label>
                  <input type="text" value={exportSettings.product_hierarchy || '["SEG","DIV","SUB_DIV","MAJ_CAT"]'} onChange={e => setExportSettings(p => ({...p, product_hierarchy: e.target.value}))} className="input font-mono text-xs" />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">Store Hierarchy (JSON)</label>
                  <input type="text" value={exportSettings.store_hierarchy || '["ZONE","REG","STORE"]'} onChange={e => setExportSettings(p => ({...p, store_hierarchy: e.target.value}))} className="input font-mono text-xs" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">GM Field</label>
                  <input type="text" value={exportSettings.product_gm_field || 'SEG'} onChange={e => setExportSettings(p => ({...p, product_gm_field: e.target.value}))} className="input" />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">GM Value</label>
                  <input type="text" value={exportSettings.product_gm_value || 'GM'} onChange={e => setExportSettings(p => ({...p, product_gm_value: e.target.value}))} className="input" />
                </div>
              </div>
            </div>
            <div className="px-4 py-3 bg-gray-50 flex justify-end gap-2">
              <button onClick={() => setShowSettings(false)} className="btn-secondary text-sm">Cancel</button>
              <button onClick={saveExportSettings} className="btn-primary text-sm">Save Settings</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
