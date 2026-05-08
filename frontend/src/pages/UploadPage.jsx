import { useState, useRef, useEffect } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { Upload, FileSpreadsheet, Eye, ArrowRight, Check, AlertCircle, Download, Trash2, Edit3, Lock, Unlock, Loader2, Clock, RefreshCw, List, X, StopCircle, ChevronDown, ArrowLeft } from 'lucide-react'
import { uploadAPI, tablesAPI, checklistAPI } from '@/services/api'
import toast from 'react-hot-toast'

// Human-friendly duration: 950ms → "950ms", 57 154ms → "57.2s", 90 500ms → "1m 30s"
function fmtDuration(ms) {
  if (ms == null || isNaN(ms)) return '-'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  return `${m}m ${s}s`
}

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
      const dropdownHeight = Math.min(280, filtered.length * 30 + 20)
      
      if (spaceBelow >= dropdownHeight || spaceBelow >= spaceAbove) {
        setDropdownStyle({
          top: rect.bottom + 2,
          left: rect.left,
          width: Math.max(rect.width, 200),
          maxHeight: Math.min(280, spaceBelow - 10)
        })
      } else {
        setDropdownStyle({
          bottom: window.innerHeight - rect.top + 2,
          left: rect.left,
          width: Math.max(rect.width, 200),
          maxHeight: Math.min(280, spaceAbove - 10)
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
        {Icon && <Icon size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-blue-600 pointer-events-none" />}
        <input
          ref={inputRef}
          type="text"
          value={open ? search : value}
          onChange={(e) => { setSearch(e.target.value); setHighlightedIndex(0) }}
          onFocus={() => { setOpen(true); setHighlightedIndex(0); setSearch('') }}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          className={`w-full h-8 ${Icon ? 'pl-7' : 'pl-2.5'} pr-7 bg-white border rounded-lg text-xs hover:border-blue-400 focus:border-blue-400 focus:outline-none`}
          autoComplete="off"
        />
        {value && !open ? (
          <X size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-red-500 cursor-pointer" onClick={handleClear} />
        ) : (
          <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
        )}
      </div>
      {open && (
        <div 
          style={{ position: 'fixed', zIndex: 9999, ...dropdownStyle }}
          className="bg-white border rounded-lg shadow-2xl overflow-hidden"
        >
          <div className="overflow-y-auto" style={{ maxHeight: dropdownStyle.maxHeight || 200 }} ref={listRef}>
            {filtered.length === 0 ? <div className="p-3 text-center text-gray-400 text-xs">No results</div> :
              filtered.map((opt, i) => (
                <div 
                  key={i}
                  onMouseDown={(e) => { e.preventDefault(); onChange(opt); setOpen(false); setSearch('') }}
                  onMouseEnter={() => setHighlightedIndex(i)}
                  className={`w-full text-left px-3 py-1.5 text-xs cursor-pointer flex items-center gap-1.5
                    ${highlightedIndex === i ? 'bg-blue-500 text-white' : 'hover:bg-gray-100'} 
                    ${value === opt && highlightedIndex !== i ? 'bg-blue-50 text-blue-600 font-medium' : ''}`}
                >
                  {highlightedIndex === i && <span className="text-[10px]">▶</span>}
                  <span>{opt}</span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function UploadPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const fromChecklist = searchParams.get('from') === 'checklist'
  const [file, setFile] = useState(null)
  const [tables, setTables] = useState([])
  const [allowedTables, setAllowedTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(searchParams.get('table') || '')
  const [schema, setSchema] = useState(null)
  const [pkColumns, setPkColumns] = useState('')
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [progress, setProgress] = useState(0)
  const [progressInfo, setProgressInfo] = useState({ processed: 0, total: 0 })  // Track row counts
  const [uploading, setUploading] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [step, setStep] = useState(1)
  const [mode, setMode] = useState('upsert')
  const [useAsync, setUseAsync] = useState(false) // Default to sync for better throughput on normal files
  const [jobs, setJobs] = useState([])
  const [showJobs, setShowJobs] = useState(false)
  const [activeJobId, setActiveJobId] = useState(null)
  const [batchReport, setBatchReport] = useState(null)  // For batch report modal
  const fileRef = useRef()
  const pollRef = useRef(null)

  useEffect(() => {
    // Load all tables and allowed tables for upload
    Promise.all([
      tablesAPI.listAll(),
      tablesAPI.allowedTables('upload').catch(() => ({ data: { data: [] } }))
    ]).then(([allRes, allowedRes]) => {
      const all = allRes.data.data || []
      const allowed = allowedRes.data?.data || []
      setTables(all)
      // If permissions exist, filter; otherwise show all
      setAllowedTables(allowed.length > 0 ? all.filter(t => allowed.includes(t.table_name)) : all)
    }).catch(() => {})
    
    // Load recent jobs
    loadJobs()
  }, [])

  // Poll for active job status
  useEffect(() => {
    if (activeJobId) {
      pollRef.current = setInterval(async () => {
        try {
          const { data } = await uploadAPI.getJob(activeJobId)
          const job = data.data
          if (job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled') {
            clearInterval(pollRef.current)
            pollRef.current = null
            setActiveJobId(null)
            setUploading(false)
            if (job.status === 'cancelled') {
              setResult(null)
              setStep(1)
              setProgress(0)
            } else {
              setResult({
                batch_id: job.job_id,
                total_records: job.total_rows,
                inserted: job.inserted_rows,
                updated: job.updated_rows,
                deleted: job.deleted_rows,
                errors: job.error_rows,
                duration_ms: job.duration_ms,
                error_details: job.error_details,
                error_message: job.error_message,
                validation_errors: job.validation_errors,
              })
              setStep(4)
            }
            loadJobs()
            if (job.status === 'completed') {
              toast.success(`${mode === 'delete' ? 'Deletion' : 'Upload'} complete!`)
              checklistAPI.stamp(selectedTable).catch(() => {})
            } else if (job.status === 'cancelled') {
              toast('Job cancelled')
            } else {
              toast.error(job.error_message || 'Job failed')
            }
          } else {
            // Update progress indication
            if (job.total_rows && job.processed_rows) {
              setProgress(Math.round((job.processed_rows / job.total_rows) * 100))
              setProgressInfo({ processed: job.processed_rows, total: job.total_rows })
            } else if (job.total_rows) {
              setProgressInfo({ processed: 0, total: job.total_rows })
            }
          }
        } catch (e) {
          console.error('Failed to poll job status', e)
        }
      }, 1500) // Poll every 1.5 seconds for faster updates
    }
    
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [activeJobId, mode])

  const loadJobs = async () => {
    try {
      const { data } = await uploadAPI.listJobs(20)
      setJobs(data.data || [])
    } catch (e) {}
  }

  const handleCancelJob = async (jobId) => {
    if (!confirm('Are you sure you want to force stop this job?')) return
    try {
      const { data } = await uploadAPI.cancelJob(jobId, true)
      const message = data?.data?.message || data?.message || 'Cancellation requested'

      if (message.toLowerCase().includes('force stop')) {
        window.alert('Force stop requested. Job status is set to cancelled immediately.')
        toast.success(message)
      } else if (message.toLowerCase().includes('requested')) {
        window.alert('Cancellation requested. The job is still processing current work and will stop shortly.')
        toast(message)
      } else {
        toast.success(message)
      }

      loadJobs()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to cancel job')
    }
  }

  const handleDeleteJob = async (jobId) => {
    if (!confirm('Delete this job record? This cannot be undone.')) return
    try {
      await uploadAPI.deleteJob(jobId)
      toast.success('Job deleted')
      loadJobs()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to delete job')
    }
  }

  const viewBatchReport = async (jobId) => {
    try {
      const { data } = await uploadAPI.getJob(jobId)
      setBatchReport(data.data)
    } catch (e) {
      toast.error('Failed to load batch report')
    }
  }

  useEffect(() => {
    if (selectedTable) {
      tablesAPI.schema(selectedTable).then(r => {
        const s = r.data.data
        setSchema(s)
        const pks = s?.columns?.filter(c => c.is_primary_key).map(c => c.column_name) || []
        setPkColumns(pks.join(', '))
      }).catch(() => setSchema(null))
    } else {
      setSchema(null)
      setPkColumns('')
    }
  }, [selectedTable])

  // Filter out system-generated columns (identity, computed, auto-generated)
  const systemColumns = ['id', 'created_at', 'updated_at', 'created_by', 'updated_by', 'modified_at', 'modified_by', 'upload_datetime', 'upload_date', 'upload_time', 'uploaded_at', 'uploaded_by', 'insert_date', 'insert_datetime']
  const isSystemColumn = (col) => {
    const name = col.column_name?.toLowerCase() || ''
    const def_ = (col.default_value || '').toLowerCase()
    const hasAutoDefault = def_.includes('getdate') || def_.includes('current_timestamp') || def_.includes('sysdatetime')
    return col.is_identity || col.is_computed || systemColumns.includes(name) || hasAutoDefault
  }
  
  const editableColumns = schema?.editable_columns || []
  const allColumns = schema?.columns || []
  const nonSystemColumns = allColumns.filter(c => !isSystemColumn(c))
  const pkColumnList = allColumns.filter(c => c.is_primary_key)

  const handleFileSelect = (f) => {
    setFile(f); setPreview(null); setResult(null); setStep(1)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    const f = e.dataTransfer.files[0]
    if (f) handleFileSelect(f)
  }

  const handlePreview = async () => {
    if (!file) return
    setPreviewing(true)
    const fd = new FormData(); fd.append('file', file); fd.append('rows', '20')
    try {
      const { data } = await uploadAPI.preview(fd)
      setPreview(data.data)
      setStep(2)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to preview file')
    } finally {
      setPreviewing(false)
    }
  }

  const handleUpload = async () => {
    if (!file || !selectedTable || !pkColumns.trim()) return toast.error('Select table and enter PK columns')
    setUploading(true); setProgress(0); setProgressInfo({ processed: 0, total: 0 }); setStep(3)
    const fd = new FormData()
    fd.append('file', file)
    fd.append('table_name', selectedTable)
    fd.append('primary_key_columns', pkColumns.trim())
    fd.append('mode', mode)
    
    try {
      if (useAsync) {
        // Async upload - submit job and poll for status
        const { data } = await uploadAPI.uploadAsync(fd, (e) => {
          if (e.total) setProgress(Math.round((e.loaded / e.total) * 50)) // First 50% is file upload
        })
        const job = data.data
        setActiveJobId(job.job_id)
        setProgress(50) // File uploaded, now processing
        toast.success(`Upload job started: ${job.job_id}`)
      } else {
        // Sync upload - wait for completion
        const { data } = await uploadAPI.upload(fd, (e) => {
          if (e.total) setProgress(Math.round((e.loaded / e.total) * 100))
        })
        setResult(data.data)
        setStep(4)
        setUploading(false)
        loadJobs()
        toast.success(mode === 'delete' ? 'Deletion complete!' : 'Upload complete!')
        // Auto-stamp checklist so freshness updates
        checklistAPI.stamp(selectedTable).catch(() => {})
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Upload failed')
      setUploading(false)
    }
  }

  const downloadTemplate = () => {
    if (!schema) return
    
    // Get columns for template - exclude system columns
    let templateColumns
    if (mode === 'delete') {
      templateColumns = pkColumnList.map(c => c.column_name)
    } else {
      // Include PK + editable non-system columns
      const pks = pkColumnList.map(c => c.column_name)
      const editable = editableColumns.filter(c => {
        const col = allColumns.find(ac => ac.column_name === c)
        return col && !isSystemColumn(col) && !pks.includes(c)
      })
      templateColumns = [...pks, ...editable]
    }
    
    const csv = templateColumns.join(',') + '\n'
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${selectedTable}_${mode}_template.csv`
    a.click()
    URL.revokeObjectURL(url)
    toast.success('Template downloaded')
  }

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 mb-3">
        <div className="flex items-center gap-3">
          {fromChecklist && (
            <button
              onClick={() => navigate('/data-validation/checklist')}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold border border-indigo-200 bg-indigo-50 text-indigo-600 hover:bg-indigo-100 transition-colors"
            >
              <ArrowLeft size={13}/> Back to Checklist
            </button>
          )}
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Upload Data</h1>
            <p className="text-gray-500 text-xs mt-0.5">Upload CSV or Excel files to update or delete data in database tables</p>
          </div>
        </div>
      </div>

      {/* Main content - full width */}
      <div className="flex-1 overflow-auto space-y-3">
        {/* Mode Selection */}
        <div className="card p-3">
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-medium text-gray-700">Operation Mode</label>
            <div className="flex items-center gap-3">
              {/* Async toggle */}
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useAsync}
                  onChange={(e) => setUseAsync(e.target.checked)}
                  className="w-3 h-3 rounded text-primary-600"
                />
                <Clock size={12} className="text-gray-400" />
                <span className="text-[11px] text-gray-600">Background</span>
              </label>
              {/* Job history button */}
              <button
                onClick={() => { setShowJobs(!showJobs); loadJobs(); }}
                className="flex items-center gap-1 text-[11px] text-primary-600 hover:text-primary-700"
              >
                <List size={12} />
                {showJobs ? 'Hide Jobs' : 'Job History'} ({jobs.filter(j => j.status === 'running' || j.status === 'pending').length})
              </button>
            </div>
          </div>
          <div className="flex gap-4">
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="radio"
                name="mode"
                value="upsert"
                checked={mode === 'upsert'}
                onChange={() => setMode('upsert')}
                className="w-3 h-3 text-primary-600"
              />
              <Edit3 size={14} className="text-blue-600" />
              <span className="text-xs font-medium">Upsert</span>
              <span className="text-[10px] text-gray-400">Insert/update rows</span>
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input
                type="radio"
                name="mode"
                value="delete"
                checked={mode === 'delete'}
                onChange={() => setMode('delete')}
                className="w-3 h-3 text-red-600"
              />
              <Trash2 size={14} className="text-red-600" />
              <span className="text-xs font-medium">Delete</span>
              <span className="text-[10px] text-gray-400">Remove by PK</span>
            </label>
          </div>
        </div>

        {/* Job History Panel */}
        {showJobs && (
          <div className="card p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="font-medium text-gray-900 flex items-center gap-2">
                <Clock size={16} />
                Recent Upload Jobs
              </h3>
              <button onClick={loadJobs} className="text-xs text-primary-600 hover:text-primary-700 flex items-center gap-1">
                <RefreshCw size={12} />
                Refresh
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-gray-500">
                    <th className="pb-2 font-medium">Job ID</th>
                    <th className="pb-2 font-medium">Table</th>
                    <th className="pb-2 font-medium">File</th>
                    <th className="pb-2 font-medium">Status</th>
                    <th className="pb-2 font-medium text-right">Progress</th>
                    <th className="pb-2 font-medium text-right">Inserted</th>
                    <th className="pb-2 font-medium text-right">Updated</th>
                    <th className="pb-2 font-medium text-right">Errors</th>
                    <th className="pb-2 font-medium text-right">Duration</th>
                    <th className="pb-2 font-medium">Created</th>
                    <th className="pb-2 font-medium text-center">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.length === 0 ? (
                    <tr><td colSpan="11" className="py-4 text-center text-gray-400">No upload jobs yet</td></tr>
                  ) : (
                    jobs.map(j => (
                      <tr key={j.job_id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2 font-mono text-xs">{j.job_id}</td>
                        <td className="py-2">{j.table_name}</td>
                        <td className="py-2 truncate max-w-[150px]" title={j.file_name}>{j.file_name}</td>
                        <td className="py-2">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                            j.status === 'completed' ? 'bg-green-100 text-green-700' :
                            j.status === 'failed' ? 'bg-red-100 text-red-700' :
                            j.status === 'cancelled' ? 'bg-yellow-100 text-yellow-700' :
                            j.status === 'running' ? 'bg-blue-100 text-blue-700' :
                            j.status === 'queued' ? 'bg-purple-100 text-purple-700' :
                            'bg-gray-100 text-gray-600'
                          }`}>
                            {j.status === 'running' && <Loader2 size={10} className="inline mr-1 animate-spin" />}
                            {j.status === 'queued' && <Clock size={10} className="inline mr-1" />}
                            {j.status}
                          </span>
                        </td>
                        <td className="py-2 text-right">
                          {j.status === 'running' && j.processed_rows && j.total_rows ? (
                            <span className="text-xs">
                              <span className="font-medium text-blue-600">{j.processed_rows?.toLocaleString()}</span>
                              <span className="text-gray-400"> / {j.total_rows?.toLocaleString()}</span>
                              <span className="text-gray-500 ml-1">({Math.round((j.processed_rows / j.total_rows) * 100)}%)</span>
                            </span>
                          ) : j.total_rows ? (
                            j.total_rows?.toLocaleString()
                          ) : '-'}
                        </td>
                        <td className="py-2 text-right text-green-600">{j.inserted_rows?.toLocaleString() || 0}</td>
                        <td className="py-2 text-right text-blue-600">{j.updated_rows?.toLocaleString() || 0}</td>
                        <td className="py-2 text-right text-red-600">{j.error_rows?.toLocaleString() || 0}</td>
                        <td className="py-2 text-right text-gray-500">{fmtDuration(j.duration_ms)}</td>
                        <td className="py-2 text-gray-500 text-xs">{j.created_at ? new Date(j.created_at).toLocaleString() : '-'}</td>
                        <td className="py-2 text-center">
                          <div className="flex items-center justify-center gap-1">
                            {j.status === 'completed' && (
                              <button
                                onClick={() => viewBatchReport(j.job_id)}
                                className="text-blue-500 hover:text-blue-700 p-1 rounded hover:bg-blue-50"
                                title="View batch report"
                              >
                                <Eye size={16} />
                              </button>
                            )}
                            {(j.status === 'running' || j.status === 'queued') && (
                              <button
                                onClick={() => handleCancelJob(j.job_id)}
                                className="text-red-500 hover:text-red-700 p-1 rounded hover:bg-red-50"
                                title="Cancel job"
                              >
                                <StopCircle size={16} />
                              </button>
                            )}
                            {(j.status === 'completed' || j.status === 'failed' || j.status === 'cancelled') && (
                              <button
                                onClick={() => handleDeleteJob(j.job_id)}
                                className="text-gray-400 hover:text-red-600 p-1 rounded hover:bg-red-50"
                                title="Delete job"
                              >
                                <Trash2 size={16} />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Steps indicator */}
        <div className="flex items-center gap-1.5 text-xs">
          {[{ n: 1, l: 'File' }, { n: 2, l: 'Preview' }, { n: 3, l: mode === 'delete' ? 'Delete' : 'Upload' }, { n: 4, l: 'Done' }].map((s, i) => (
            <div key={s.n} className="flex items-center gap-1">
              <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-medium ${step >= s.n ? (mode === 'delete' ? 'bg-red-600' : 'bg-primary-600') + ' text-white' : 'bg-gray-100 text-gray-400'}`}>
                {step > s.n ? <Check size={10} /> : s.n}
              </div>
              <span className={`text-[11px] ${step >= s.n ? 'text-gray-900 font-medium' : 'text-gray-400'}`}>{s.l}</span>
              {i < 3 && <ArrowRight size={10} className="text-gray-300 mx-0.5" />}
            </div>
          ))}
        </div>

        {/* Top section: File drop + Config side by side */}
        <div className="grid grid-cols-2 gap-3">
          {/* Drop zone */}
          <div
            className="card border-2 border-dashed border-gray-300 hover:border-primary-400 transition-colors cursor-pointer"
            onClick={() => fileRef.current?.click()}
            onDragOver={e => e.preventDefault()}
            onDrop={handleDrop}
          >
            <div className="p-6 text-center">
              <Upload size={28} className="mx-auto text-gray-300 mb-2" />
              <div className="text-xs text-gray-600 font-medium">{file ? file.name : 'Drag & drop or click to select'}</div>
              <div className="text-[10px] text-gray-400 mt-0.5">CSV, XLSX, XLS • Max 500MB</div>
              {file && <div className="text-[10px] text-primary-600 mt-1">{(file.size / 1024 / 1024).toFixed(1)} MB</div>}
            </div>
            <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" className="hidden" onChange={e => handleFileSelect(e.target.files[0])} />
          </div>

          {/* Config */}
          <div className="card p-3 space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-[11px] font-medium text-gray-700 mb-1 block">Target Table</label>
                <SearchDropdown
                  options={allowedTables.map(t => t.table_name)}
                  value={selectedTable}
                  onChange={setSelectedTable}
                  placeholder="Select table..."
                />
                {allowedTables.length === 0 && tables.length > 0 && (
                  <p className="text-[10px] text-amber-600 mt-0.5">No tables configured. Check Settings.</p>
                )}
              </div>
              <div>
                <label className="text-[11px] font-medium text-gray-700 mb-1 block">Primary Key Columns</label>
                <input value={pkColumns} onChange={e => setPkColumns(e.target.value)} className="w-full h-8 px-2 text-xs border rounded-lg focus:border-primary-400 focus:outline-none" placeholder="store_code, variant_code" />
              </div>
            </div>

            {mode === 'delete' && (
              <div className="bg-red-50 rounded p-2">
                <div className="flex items-start gap-1.5">
                  <AlertCircle size={12} className="text-red-600 shrink-0 mt-0.5" />
                  <div>
                    <div className="text-[11px] font-medium text-red-800">Delete Mode</div>
                    <p className="text-[10px] text-red-600">Rows will be DELETED by primary key.</p>
                  </div>
                </div>
              </div>
            )}

            <div className="flex gap-2">
              <button 
                onClick={handlePreview} 
                disabled={!file || previewing}
                className="btn-secondary btn-sm text-[11px]"
              >
                {previewing ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />}
                {previewing ? 'Loading' : 'Preview'}
              </button>
              {schema && (
                <button onClick={downloadTemplate} className="btn-secondary btn-sm text-[11px]">
                  <Download size={12} /> Template
                </button>
              )}
              <button 
                onClick={handleUpload} 
                disabled={uploading || !selectedTable || !pkColumns || !file} 
                className={`btn-sm text-[11px] ${mode === 'delete' ? 'btn-danger' : 'btn-primary'}`}
              >
                {uploading ? <Loader2 size={12} className="animate-spin" /> : mode === 'delete' ? <Trash2 size={12} /> : <FileSpreadsheet size={12} />}
                {uploading ? 'Processing' : mode === 'delete' ? 'Delete' : 'Upsert'}
              </button>
            </div>
          </div>
        </div>

        {/* Column Permissions */}
        {schema && mode === 'upsert' && (
          <div className="card p-3">
            <div className="text-[11px] font-medium text-gray-700 mb-1.5">Column Permissions</div>
            <div className="flex flex-wrap gap-1">
              {allColumns.map(col => {
                const isPK = col.is_primary_key
                const canEdit = editableColumns.includes(col.column_name)
                const isSys = isSystemColumn(col)
                return (
                  <span
                    key={col.column_name}
                    className={`px-1.5 py-0.5 rounded text-[10px] flex items-center gap-0.5 ${
                      isSys
                        ? 'bg-gray-300 text-gray-600 line-through'
                        : isPK 
                          ? 'bg-amber-100 text-amber-700' 
                          : canEdit 
                            ? 'bg-green-100 text-green-700' 
                            : 'bg-gray-200 text-gray-500'
                    }`}
                    title={isSys ? 'System column (auto-generated)' : ''}
                  >
                    {isPK ? 'PK' : canEdit && !isSys ? <Unlock size={8} /> : <Lock size={8} />}
                    {col.column_name}
                  </span>
                )
              })}
            </div>
            <p className="text-xs text-gray-500 mt-2">
              Green = editable, Yellow = primary key, Gray = read-only, Strikethrough = system (excluded from template)
            </p>
          </div>
        )}

        {/* Preview */}
        {preview && step >= 2 && (
          <div className="card">
            <div className="card-header flex items-center justify-between">
              <h3 className="font-semibold">File Preview ({preview.preview_rows} rows, {preview.total_columns} columns)</h3>
            </div>
            <div className="overflow-x-auto max-h-[400px]">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-white"><tr className="bg-gray-50 border-b">
                  {preview.columns?.map(c => (
                    <th key={c.name} className="px-3 py-2 text-left font-medium text-gray-600 whitespace-nowrap">
                      {c.name}<div className="text-xs text-gray-400 font-normal">{c.dtype}</div>
                    </th>
                  ))}
                </tr></thead>
                <tbody>
                  {preview.data?.map((row, i) => (
                    <tr key={i} className="border-b hover:bg-gray-50">
                      {preview.columns?.map(c => <td key={c.name} className="px-3 py-2 text-gray-700 truncate max-w-[200px]">{row[c.name] ?? ''}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Progress */}
        {uploading && (
          <div className="card p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium flex items-center gap-2">
                <Loader2 size={14} className="animate-spin" />
                {activeJobId ? (
                  <>Processing in background... <span className="text-xs font-mono text-gray-500">({activeJobId})</span></>
                ) : 'Processing...'}
              </span>
              <div className="text-right">
                <span className="text-sm font-semibold text-primary-600">{progress}%</span>
                {progressInfo.total > 0 && (
                  <div className="text-xs text-gray-500">
                    {progressInfo.processed.toLocaleString()} / {progressInfo.total.toLocaleString()} rows
                  </div>
                )}
              </div>
            </div>
            <div className="w-full bg-gray-200 rounded-full h-2">
              <div className={`h-2 rounded-full transition-all ${mode === 'delete' ? 'bg-red-600' : 'bg-primary-600'}`} style={{ width: `${progress}%` }} />
            </div>
            {activeJobId && (
              <p className="text-xs text-gray-500 mt-2">
                You can navigate away from this page - the upload will continue in the background. 
                Check Job History to see progress.
              </p>
            )}
          </div>
        )}

        {/* Result */}
        {result && step === 4 && (
          <div className={`card p-5 ${
            result.errors > 0 && (result.inserted + result.updated) === 0
              ? 'border-red-200 bg-red-50'
              : result.errors > 0 
                ? 'border-amber-200 bg-amber-50'
                : mode === 'delete' 
                  ? 'border-red-200 bg-red-50' 
                  : 'border-emerald-200 bg-emerald-50'
          }`}>
            <div className="flex items-center gap-2 mb-3">
              {result.errors > 0 && (result.inserted + result.updated) === 0 ? (
                <>
                  <AlertCircle size={20} className="text-red-600" />
                  <h3 className="font-semibold text-red-900">Upload Failed - All Records Had Errors</h3>
                </>
              ) : result.errors > 0 ? (
                <>
                  <AlertCircle size={20} className="text-amber-600" />
                  <h3 className="font-semibold text-amber-900">Upload Completed with Errors</h3>
                </>
              ) : (
                <>
                  <Check size={20} className={mode === 'delete' ? 'text-red-600' : 'text-emerald-600'} />
                  <h3 className={`font-semibold ${mode === 'delete' ? 'text-red-900' : 'text-emerald-900'}`}>
                    {mode === 'delete' ? 'Deletion Complete' : 'Upload Complete'}
                  </h3>
                </>
              )}
            </div>
            <div className="grid grid-cols-5 gap-4">
              {mode === 'delete' ? (
                <>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-gray-900">{result.total_records?.toLocaleString() || result.total?.toLocaleString() || 0}</div>
                    <div className="text-xs text-gray-500">Total Records</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-red-600">{result.deleted?.toLocaleString() || 0}</div>
                    <div className="text-xs text-gray-500">Deleted</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-gray-600">{result.not_found?.toLocaleString() || 0}</div>
                    <div className="text-xs text-gray-500">Not Found</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-orange-600">{result.errors?.toLocaleString() || 0}</div>
                    <div className="text-xs text-gray-500">Errors</div>
                  </div>
                </>
              ) : (
                [
                  { l: 'Total Records', v: result.total_records, color: 'text-gray-900' },
                  { l: 'Inserted', v: result.inserted, color: 'text-green-600' },
                  { l: 'Updated', v: result.updated, color: 'text-blue-600' },
                  { l: 'Unchanged', v: result.unchanged, color: 'text-gray-400' },
                  { l: 'Errors', v: result.errors, color: 'text-red-600' },
                ].map(s => (
                  <div key={s.l} className="text-center">
                    <div className={`text-2xl font-bold ${s.color}`}>{s.v?.toLocaleString() || 0}</div>
                    <div className="text-xs text-gray-500">{s.l}</div>
                  </div>
                ))
              )}
            </div>
            <div className="text-xs text-gray-500 mt-3">Batch: {result.batch_id} • Duration: {fmtDuration(result.duration_ms ?? result.total_duration_ms)}</div>
            
            {/* Error Details */}
            {result.error_details && result.error_details.length > 0 && (
              <div className="mt-4 p-3 bg-red-50 rounded border border-red-200 text-left">
                <div className="text-sm font-medium text-red-800 mb-2">Error Details:</div>
                <div className="space-y-1 text-xs text-red-700 max-h-32 overflow-y-auto">
                  {result.error_details.map((err, i) => (
                    <div key={i} className="font-mono">
                      {typeof err === 'string' ? err : `Chunk ${err.chunk} (rows ${err.rows}): ${err.error}`}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Validation Errors — Row-level type mismatch details */}
            {result.validation_errors && result.validation_errors.length > 0 && (
              <div className="mt-4 p-3 bg-amber-50 rounded border border-amber-200 text-left">
                <div className="flex items-center justify-between mb-2">
                  <div className="text-sm font-medium text-amber-800">
                    Data Type Warnings ({result.validation_errors.length} issues found — fix these in your file and re-upload)
                  </div>
                  <button
                    onClick={() => {
                      const rows = result.validation_errors
                      const csv = 'Row,Column,Value,Expected Type,Target Type\n' +
                        rows.map(e => `${e.row},"${(e.column||'').replace(/"/g,'""')}","${(e.value||'').replace(/"/g,'""')}","${e.expected}","${e.target_type}"`).join('\n')
                      const blob = new Blob([csv], { type: 'text/csv' })
                      const url = URL.createObjectURL(blob)
                      const a = document.createElement('a')
                      a.href = url; a.download = 'validation_errors.csv'; a.click()
                      URL.revokeObjectURL(url)
                    }}
                    className="text-xs px-2 py-1 bg-amber-100 hover:bg-amber-200 text-amber-800 rounded border border-amber-300"
                  >
                    Download CSV
                  </button>
                </div>
                <div className="overflow-x-auto">
                  <table className="text-xs w-full border-collapse">
                    <thead>
                      <tr className="bg-amber-100 text-amber-900">
                        <th className="px-2 py-1 border border-amber-200 text-left">Row</th>
                        <th className="px-2 py-1 border border-amber-200 text-left">Column</th>
                        <th className="px-2 py-1 border border-amber-200 text-left">Your Value</th>
                        <th className="px-2 py-1 border border-amber-200 text-left">Expected</th>
                      </tr>
                    </thead>
                    <tbody className="max-h-48 overflow-y-auto">
                      {result.validation_errors.slice(0, 50).map((err, i) => (
                        <tr key={i} className={i % 2 ? 'bg-amber-50/50' : ''}>
                          <td className="px-2 py-1 border border-amber-200 font-mono">{err.row}</td>
                          <td className="px-2 py-1 border border-amber-200 font-semibold">{err.column}</td>
                          <td className="px-2 py-1 border border-amber-200 font-mono text-red-700">{err.value}</td>
                          <td className="px-2 py-1 border border-amber-200">{err.expected}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {result.validation_errors.length > 50 && (
                    <div className="text-xs text-amber-600 mt-1">
                      Showing first 50 of {result.validation_errors.length} issues. Download CSV for full list.
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Batch Report Modal */}
      {batchReport && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b">
              <div>
                <h2 className="text-lg font-semibold">Batch Report: {batchReport.job_id}</h2>
                <p className="text-sm text-gray-500">{batchReport.table_name} - {batchReport.file_name}</p>
              </div>
              <button onClick={() => setBatchReport(null)} className="p-2 hover:bg-gray-100 rounded-lg">
                <X size={18} />
              </button>
            </div>
            <div className="flex-1 overflow-auto p-4 space-y-4">
              {/* Summary Stats */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                <div className="bg-gray-50 p-3 rounded-lg">
                  <div className="text-2xl font-bold">{batchReport.total_rows?.toLocaleString()}</div>
                  <div className="text-xs text-gray-500">Total Rows</div>
                </div>
                <div className="bg-green-50 p-3 rounded-lg">
                  <div className="text-2xl font-bold text-green-600">{batchReport.inserted_rows?.toLocaleString()}</div>
                  <div className="text-xs text-gray-500">Inserted</div>
                </div>
                <div className="bg-blue-50 p-3 rounded-lg">
                  <div className="text-2xl font-bold text-blue-600">{batchReport.updated_rows?.toLocaleString()}</div>
                  <div className="text-xs text-gray-500">Updated</div>
                </div>
                <div className="bg-red-50 p-3 rounded-lg">
                  <div className="text-2xl font-bold text-red-600">{batchReport.error_rows?.toLocaleString() || 0}</div>
                  <div className="text-xs text-gray-500">Errors</div>
                </div>
                <div className="bg-gray-50 p-3 rounded-lg">
                  <div className="text-2xl font-bold">{fmtDuration(batchReport.duration_ms)}</div>
                  <div className="text-xs text-gray-500">Duration</div>
                </div>
              </div>

              {/* Changed Columns Summary */}
              {batchReport.changed_columns_summary && Object.keys(batchReport.changed_columns_summary).length > 0 && (
                <div>
                  <h3 className="font-medium mb-2">Changed Columns</h3>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(batchReport.changed_columns_summary).map(([col, count]) => (
                      <span key={col} className="px-2 py-1 bg-blue-100 text-blue-800 rounded text-sm">
                        {col}: {count.toLocaleString()}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Sample Changes */}
              {batchReport.sample_changes && batchReport.sample_changes.length > 0 && (
                <div>
                  <h3 className="font-medium mb-2">Sample Changes (first {batchReport.sample_changes.length})</h3>
                  <div className="overflow-x-auto border rounded-lg">
                    <table className="w-full text-sm">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-3 py-2 text-left">Action</th>
                          <th className="px-3 py-2 text-left">Primary Key</th>
                          <th className="px-3 py-2 text-left">Changed Columns</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {batchReport.sample_changes.slice(0, 50).map((c, idx) => (
                          <tr key={idx} className="hover:bg-gray-50">
                            <td className="px-3 py-2">
                              <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                                c.action_type === 'INSERT' ? 'bg-green-100 text-green-700' :
                                c.action_type === 'UPDATE' ? 'bg-blue-100 text-blue-700' :
                                'bg-gray-100 text-gray-700'
                              }`}>
                                {c.action_type}
                              </span>
                            </td>
                            <td className="px-3 py-2 font-mono text-xs">{c.pk}</td>
                            <td className="px-3 py-2 text-xs text-gray-600">
                              {c.changed_columns?.join(', ') || '-'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {batchReport.total_rows > 50000 && (
                    <p className="text-xs text-gray-500 mt-2">
                      Note: This is a sample of the first 100 changes. Full detailed audit is only available for uploads under 50,000 rows.
                    </p>
                  )}
                </div>
              )}

              {/* No sample changes message */}
              {(!batchReport.sample_changes || batchReport.sample_changes.length === 0) && (
                <div className="text-center py-8 text-gray-500">
                  <FileSpreadsheet size={40} className="mx-auto mb-3 opacity-50" />
                  <p>No sample changes available</p>
                  <p className="text-sm">This upload may have been all inserts or the sample wasn't captured</p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
