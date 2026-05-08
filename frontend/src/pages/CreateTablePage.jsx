import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { Upload, Check, Plus, Trash2, Key, Columns, Table2, Eye, EyeOff, FileSpreadsheet } from 'lucide-react'
import { tablesAPI, uploadAPI } from '@/services/api'
import toast from 'react-hot-toast'
import SearchableSelect from '@/components/ui/SearchableSelect'

const SQL_TYPES = [
  { value: 'NVARCHAR', label: 'NVARCHAR' },
  { value: 'VARCHAR', label: 'VARCHAR' },
  { value: 'INT', label: 'INT' },
  { value: 'BIGINT', label: 'BIGINT' },
  { value: 'DECIMAL', label: 'DECIMAL' },
  { value: 'FLOAT', label: 'FLOAT' },
  { value: 'BIT', label: 'BIT' },
  { value: 'DATE', label: 'DATE' },
  { value: 'DATETIME2', label: 'DATETIME2' },
]

const TABLE_NAME_REGEX = /^[A-Z_][A-Z0-9_]*$/
const COLUMN_NAME_REGEX = /^[A-Z0-9_][A-Z0-9_]*$/  // Allow columns starting with numbers (SQL Server supports with brackets)

export default function CreateTablePage() {
  const navigate = useNavigate()
  const [mode, setMode] = useState('manual')
  const [file, setFile] = useState(null)
  const [preview, setPreview] = useState(null)
  const [showPreview, setShowPreview] = useState(false)
  const [tableName, setTableName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [module, setModule] = useState('')
  const [columns, setColumns] = useState([{ name: '', type: 'NVARCHAR', maxLength: 255, isPK: false }])
  const [errors, setErrors] = useState({ tableName: '', columnsGeneral: '', pk: '', columns: {} })
  const [creating, setCreating] = useState(false)
  const fileRef = useRef()

  /**
   * Sanitize and auto-correct column/table names for SQL compatibility.
   * Examples:
   *   "10 DIGIT" → "10_DIGIT"
   *   "Product Name!" → "PRODUCT_NAME"
   *   "  spaces  " → "SPACES"
   */
  const sanitizeName = (value) => {
    if (!value) return ''
    
    // Convert to uppercase
    let result = value.toUpperCase()
    
    // Replace spaces and special characters with underscores
    result = result.replace(/[^A-Z0-9_]/g, '_')
    
    // Collapse multiple consecutive underscores to single
    result = result.replace(/_+/g, '_')
    
    // Remove leading and trailing underscores
    result = result.replace(/^_+|_+$/g, '')
    
    // If empty after sanitization, return placeholder
    if (!result) return 'COLUMN'
    
    return result
  }

  const inferType = (dtype) => {
    if (!dtype) return 'NVARCHAR'
    const lowered = String(dtype).toLowerCase()
    if (lowered.includes('int')) return 'INT'
    if (lowered.includes('float') || lowered.includes('double')) return 'FLOAT'
    if (lowered.includes('date')) return 'DATETIME2'
    if (lowered.includes('bool')) return 'BIT'
    if (lowered.includes('decimal') || lowered.includes('numeric')) return 'DECIMAL'
    return 'NVARCHAR'
  }

  const clearValidation = () => {
    setErrors({ tableName: '', columnsGeneral: '', pk: '', columns: {} })
  }

  const handleFileSelect = async (selectedFile) => {
    setFile(selectedFile)
    if (!selectedFile) return

    const formData = new FormData()
    formData.append('file', selectedFile)
    formData.append('rows', '10')

    try {
      const { data } = await uploadAPI.preview(formData)
      setPreview(data.data)
      setShowPreview(false)

      const baseName = sanitizeName(selectedFile.name.replace(/\.[^.]+$/, ''))
      setTableName(baseName)
      setDisplayName(baseName)
      clearValidation()

      const inferredCols = (data.data?.columns || []).map((column, index) => {
        const inferredType = inferType(column.dtype)
        return {
          name: sanitizeName(column.name),
          type: inferredType,
          maxLength: inferredType === 'DECIMAL' ? 18 : 255,
          isPK: index === 0,
        }
      })

      if (inferredCols.length > 0) setColumns(inferredCols)
    } catch {
      toast.error('Failed to preview file')
    }
  }

  const addColumn = () => {
    setColumns([...columns, { name: '', type: 'NVARCHAR', maxLength: 255, isPK: false }])
  }

  const removeColumn = (index) => {
    setColumns(columns.filter((_, idx) => idx !== index))
    setErrors(prev => {
      const nextColumns = { ...prev.columns }
      delete nextColumns[index]
      return { ...prev, columns: nextColumns }
    })
  }

  const updateColumn = (index, field, value) => {
    const updated = [...columns]
    updated[index][field] = value

    if (field === 'type') {
      if (value === 'NVARCHAR' || value === 'VARCHAR') updated[index].maxLength = updated[index].maxLength || 255
      if (value === 'DECIMAL') updated[index].maxLength = updated[index].maxLength || 18
    }

    setColumns(updated)
    setErrors(prev => ({
      ...prev,
      columnsGeneral: '',
      pk: '',
      columns: {
        ...prev.columns,
        [index]: {
          ...(prev.columns[index] || {}),
          ...(field === 'name' ? { name: '' } : {}),
          ...((field === 'maxLength' || field === 'type') ? { maxLength: '' } : {}),
        },
      },
    }))
  }

  const validateForm = () => {
    const nextErrors = { tableName: '', columnsGeneral: '', pk: '', columns: {} }
    let hasError = false

    const normalizedTableName = tableName.trim().toUpperCase()
    if (!normalizedTableName) {
      nextErrors.tableName = 'Table name is required'
      hasError = true
    } else if (!TABLE_NAME_REGEX.test(normalizedTableName)) {
      nextErrors.tableName = 'Use A-Z, 0-9, _. Must start with letter/_'
      hasError = true
    }

    const enteredColumns = columns.filter(column => column.name.trim())
    if (enteredColumns.length === 0) {
      nextErrors.columnsGeneral = 'At least one column is required'
      hasError = true
    }

    const normalizedNames = new Map()
    columns.forEach((column, index) => {
      if (!column.name.trim()) return

      const normalizedName = column.name.trim().toUpperCase()
      if (!COLUMN_NAME_REGEX.test(normalizedName)) {
        nextErrors.columns[index] = { ...(nextErrors.columns[index] || {}), name: 'Invalid name format' }
        hasError = true
      }

      if (normalizedNames.has(normalizedName)) {
        const existingIndex = normalizedNames.get(normalizedName)
        nextErrors.columns[index] = { ...(nextErrors.columns[index] || {}), name: 'Duplicate column name' }
        nextErrors.columns[existingIndex] = { ...(nextErrors.columns[existingIndex] || {}), name: 'Duplicate column name' }
        hasError = true
      } else {
        normalizedNames.set(normalizedName, index)
      }

      if ((column.type === 'NVARCHAR' || column.type === 'VARCHAR' || column.type === 'DECIMAL') && (!column.maxLength || Number(column.maxLength) <= 0)) {
        nextErrors.columns[index] = { ...(nextErrors.columns[index] || {}), maxLength: 'Length/precision must be > 0' }
        hasError = true
      }
    })

    if (enteredColumns.filter(column => column.isPK).length === 0) {
      nextErrors.pk = 'Select at least one primary key column'
      hasError = true
    }

    setErrors(nextErrors)
    return !hasError
  }

  const handleCreate = async () => {
    if (!validateForm()) return

    const normalizedTableName = tableName.trim().toUpperCase()
    const enteredColumns = columns.filter(column => column.name.trim())

    setCreating(true)
    try {
      const payloadColumns = enteredColumns.map(column => {
        const payload = {
          column_name: column.name.trim().toUpperCase(),
          data_type: column.type,
          is_primary_key: column.isPK,
          is_nullable: !column.isPK,
        }

        if (column.type === 'NVARCHAR' || column.type === 'VARCHAR' || column.type === 'DECIMAL') {
          payload.max_length = Number(column.maxLength || (column.type === 'DECIMAL' ? 18 : 255))
        }

        return payload
      })

      await tablesAPI.create({
        table_name: normalizedTableName,
        display_name: (displayName || normalizedTableName).trim(),
        description: description.trim() || null,
        module: (module || 'Data').trim(),
        columns: payloadColumns,
      })

      toast.success('Table created successfully')

      if (mode === 'excel' && file && preview) {
        const primaryKeys = payloadColumns.filter(column => column.is_primary_key).map(column => column.column_name)
        const uploadFormData = new FormData()
        uploadFormData.append('file', file)
        uploadFormData.append('table_name', normalizedTableName)
        uploadFormData.append('primary_key_columns', primaryKeys.join(','))
        uploadFormData.append('mode', 'upsert')

        try {
          await uploadAPI.upload(uploadFormData)
          toast.success('Data uploaded successfully')
        } catch {
          toast.error('Table created, but data upload failed')
        }
      }

      navigate('/tables')
    } catch (error) {
      toast.error(error.response?.data?.detail || 'Failed to create table')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-3 w-full">
      <div className="card p-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-indigo-600 flex items-center justify-center shadow">
            <Table2 size={14} className="text-white" />
          </div>
          <div>
            <h1 className="font-semibold text-sm text-gray-800">Create Table</h1>
            <p className="text-xs text-gray-500">Create table manually or infer schema from file</p>
          </div>
        </div>
        <div className="flex gap-2 shrink-0">
          <button onClick={() => navigate('/tables')} className="btn-secondary h-8 px-3 text-xs">Cancel</button>
          <button onClick={handleCreate} disabled={creating} className="btn-primary h-8 px-3 text-xs">
            <Check size={14} /> {creating ? 'Creating...' : mode === 'excel' && file ? 'Create & Load Data' : 'Create Table'}
          </button>
        </div>
      </div>

      <div className="space-y-3">
        <div className="flex bg-gray-100 rounded-lg p-1 w-fit">
          {[
            { value: 'manual', label: 'Manual Schema' },
            { value: 'excel', label: 'From Excel File' },
          ].map(option => (
            <button
              key={option.value}
              onClick={() => { setMode(option.value); setFile(null); setPreview(null); clearValidation() }}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${mode === option.value ? 'bg-white shadow text-gray-900' : 'text-gray-500'}`}
            >
              {option.label}
            </button>
          ))}
        </div>

        {mode === 'excel' && (
          <div className="card p-3">
            <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3">
              <div className="flex items-center gap-2 text-xs text-gray-600 min-w-0">
                <FileSpreadsheet size={14} className="text-gray-400 shrink-0" />
                <span className="truncate">{file ? file.name : 'No file selected'}</span>
              </div>
              <div className="sm:ml-auto">
                <button
                  type="button"
                  onClick={() => fileRef.current?.click()}
                  className="btn-secondary h-8 px-3 text-xs"
                >
                  <Upload size={13} /> {file ? 'Change File' : 'Select File'}
                </button>
              </div>
            </div>
            <p className="text-[11px] text-gray-400 mt-2">Schema inferred from headers; first column auto-marked as PK</p>
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xls,.csv"
              className="hidden"
              onChange={(event) => handleFileSelect(event.target.files[0])}
            />
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
          <div className="lg:col-span-1 card p-3 space-y-3">
            <h3 className="font-semibold text-gray-900 flex items-center gap-2"><Table2 size={16} /> Table Information</h3>

            <div className="space-y-3">
              <div>
                <label className="label">Table Name*</label>
                <input
                  value={tableName}
                  onChange={(event) => {
                    setTableName(sanitizeName(event.target.value))
                    setErrors(prev => ({ ...prev, tableName: '' }))
                  }}
                  className={`input h-8 text-xs ${errors.tableName ? 'border-red-400 focus:ring-red-200' : ''}`}
                  placeholder="e.g., MASTER_PRODUCTS"
                />
                {errors.tableName && <p className="text-xs text-red-600 mt-1">{errors.tableName}</p>}
              </div>

              <div>
                <label className="label">Display Name</label>
                <input
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  className="input h-8 text-xs"
                  placeholder="e.g., Products Master"
                />
              </div>

              <div>
                <label className="label">Module</label>
                <input
                  value={module}
                  onChange={(event) => setModule(event.target.value)}
                  className="input h-8 text-xs"
                  placeholder="e.g., Inventory"
                />
              </div>
            </div>

            <div>
              <label className="label">Description</label>
              <textarea
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                className="input min-h-[56px] text-xs"
                placeholder="Optional description"
              />
            </div>

            {preview && mode === 'excel' && (
              <div className="rounded-lg border border-blue-100 bg-blue-50 p-3 text-xs text-blue-700">
                <div className="flex items-center justify-between gap-2">
                  <span>File preview loaded: {preview.total_rows || 0} rows, {preview.total_columns || 0} columns</span>
                  <button
                    type="button"
                    onClick={() => setShowPreview(v => !v)}
                    className="text-xs text-blue-700 hover:text-blue-900 flex items-center gap-1"
                  >
                    {showPreview ? <EyeOff size={12} /> : <Eye size={12} />}
                    {showPreview ? 'Hide Preview' : 'Show Preview'}
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="lg:col-span-3 space-y-3">
            <div className="card p-3 space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-gray-900 flex items-center gap-2"><Columns size={16} /> Columns</h3>
                <button onClick={addColumn} className="btn-secondary btn-sm"><Plus size={14} /> Add Column</button>
              </div>

              <div className="space-y-2">
                <div className="grid grid-cols-12 gap-2 text-xs font-medium text-gray-500 px-2">
                  <div className="col-span-4">Column Name</div>
                  <div className="col-span-3">Data Type</div>
                  <div className="col-span-3">Length / Precision</div>
                  <div className="col-span-1 text-center">PK</div>
                  <div className="col-span-1"></div>
                </div>

                {columns.map((column, index) => (
                  <div key={index} className="grid grid-cols-12 gap-2 items-start">
                    <div className="col-span-4">
                      <input
                        value={column.name}
                        onChange={(event) => updateColumn(index, 'name', sanitizeName(event.target.value))}
                        className={`input h-8 text-xs ${errors.columns[index]?.name ? 'border-red-400 focus:ring-red-200' : ''}`}
                        placeholder="COLUMN_NAME"
                      />
                      {errors.columns[index]?.name && <p className="text-[11px] text-red-600 mt-1">{errors.columns[index].name}</p>}
                    </div>

                    <div className="col-span-3">
                      <SearchableSelect
                        value={column.type}
                        onChange={(val) => updateColumn(index, 'type', val)}
                        options={SQL_TYPES}
                        size="sm"
                      />
                    </div>

                    <div className="col-span-3">
                      {(column.type === 'NVARCHAR' || column.type === 'VARCHAR' || column.type === 'DECIMAL') ? (
                        <>
                          <input
                            type="number"
                            min={1}
                            value={column.maxLength || ''}
                            onChange={(event) => updateColumn(index, 'maxLength', Number(event.target.value || 0))}
                            className={`input h-8 text-xs ${errors.columns[index]?.maxLength ? 'border-red-400 focus:ring-red-200' : ''}`}
                            placeholder={column.type === 'DECIMAL' ? '18' : '255'}
                          />
                          {errors.columns[index]?.maxLength && <p className="text-[11px] text-red-600 mt-1">{errors.columns[index].maxLength}</p>}
                        </>
                      ) : (
                        <input className="input h-8 text-xs bg-gray-50" value="-" disabled />
                      )}
                    </div>

                    <div className="col-span-1 flex justify-center pt-0.5">
                      <button
                        onClick={() => updateColumn(index, 'isPK', !column.isPK)}
                        className={`p-1.5 rounded-lg transition-colors ${column.isPK ? 'bg-amber-100 text-amber-600' : 'bg-gray-100 text-gray-400'}`}
                        title="Primary key"
                      >
                        <Key size={12} />
                      </button>
                    </div>

                    <div className="col-span-1 flex justify-end pt-0.5">
                      {columns.length > 1 && (
                        <button onClick={() => removeColumn(index)} className="p-1.5 text-gray-400 hover:text-red-500" title="Remove column">
                          <Trash2 size={13} />
                        </button>
                      )}
                    </div>
                  </div>
                ))}

                {errors.columnsGeneral && <p className="text-xs text-red-600 px-2">{errors.columnsGeneral}</p>}
                {errors.pk && <p className="text-xs text-red-600 px-2">{errors.pk}</p>}
              </div>
            </div>

            {preview && mode === 'excel' && showPreview && (
              <div className="card">
                <div className="card-header flex items-center justify-between">
                  <h3 className="font-semibold">File Preview</h3>
                  <span className="text-xs text-gray-500">{preview.total_rows} rows • {preview.total_columns} columns</span>
                </div>
                <div className="overflow-x-auto max-h-[180px]">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-gray-50">
                      <tr className="border-b">
                        {preview.columns?.map(column => <th key={column.name} className="px-2 py-1.5 text-left font-medium text-gray-600">{column.name}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.data?.slice(0, 6).map((row, rowIndex) => (
                        <tr key={rowIndex} className="border-b hover:bg-gray-50">
                          {preview.columns?.map(column => <td key={column.name} className="px-2 py-1.5 text-gray-700 truncate max-w-[200px]">{row[column.name] ?? ''}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
