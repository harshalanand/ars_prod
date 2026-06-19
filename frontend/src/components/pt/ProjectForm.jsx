// Modal form for creating or editing a Project Tracker item.
// `initial` populates the form for edit mode; absence = create mode.
// `parents` is the flat tree (used to populate the parent-picker).
// On submit, calls `onSave(payload)`. Caller MUST resolve with the project_id
// (number) so post-create attachment uploads can target it.
import { useEffect, useRef, useState } from 'react'
import { Paperclip, Trash2, X, Download, Upload } from 'lucide-react'
import toast from 'react-hot-toast'
import { ptAPI } from '@/services/api'

const ATTACH_ALLOWED = ['.xlsx', '.xls', '.csv', '.pdf', '.png', '.jpg', '.jpeg',
                        '.docx', '.doc', '.txt', '.zip']
const ATTACH_MAX_BYTES = 25 * 1024 * 1024

function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function extOf(name) {
  const i = (name || '').lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

const ENUM_FALLBACK = {
  status:   ['DRAFT','NOT_STARTED','IN_PROGRESS','BLOCKED','ON_HOLD','COMPLETED','CANCELLED'],
  priority: ['CRITICAL','HIGH','MEDIUM','LOW'],
  phase:    ['PHASE_1','PHASE_2','PHASE_3','BACKLOG','ICEBOX'],
  category: ['BUG','FEATURE','ENHANCEMENT','RESEARCH','MAINTENANCE','INFRA','OTHER'],
}

const inputStyle = {
  width: '100%', padding: '8px 10px',
  border: '1px solid #d1d5db', borderRadius: 6,
  fontSize: 13, outline: 'none', background: '#fff',
}
const labelStyle = {
  display: 'block', fontSize: 11, fontWeight: 600,
  color: '#374151', marginBottom: 4, textTransform: 'uppercase',
  letterSpacing: 0.4,
}

export default function ProjectForm({ initial, parents = [], enums, onSave, onClose }) {
  const isEdit = !!initial?.PROJECT_ID
  const projectId = initial?.PROJECT_ID || null
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState(() => ({
    parent_id:        initial?.PARENT_ID ?? null,
    name:             initial?.NAME ?? '',
    description:      initial?.DESCRIPTION ?? '',
    status:           initial?.STATUS ?? 'NOT_STARTED',
    priority:         initial?.PRIORITY ?? 'MEDIUM',
    phase:            initial?.PHASE ?? 'BACKLOG',
    category:         initial?.CATEGORY ?? '',
    tags:             initial?.TAGS ?? '',
    owner_username:   initial?.OWNER_USERNAME ?? '',
    assignees:        initial?.ASSIGNEES ?? '',
    start_date:       initial?.START_DATE?.slice?.(0, 10) ?? '',
    due_date:         initial?.DUE_DATE?.slice?.(0, 10)   ?? '',
    estimated_hours:  initial?.ESTIMATED_HOURS ?? '',
    progress_pct:     initial?.PROGRESS_PCT ?? 0,
  }))

  // ── Attachments ──────────────────────────────────────────────────────────
  // Edit mode: existing[] hydrated from server; uploads happen immediately.
  // Create mode: staged[] holds File objects to upload after project is created.
  const [existing, setExisting] = useState([])
  const [staged,   setStaged]   = useState([]) // [{file, error?}]
  const [attBusy,  setAttBusy]  = useState(false)
  const fileInputRef = useRef(null)

  useEffect(() => {
    if (!projectId) return
    ptAPI.attachments.list(projectId)
      .then(r => setExisting(r.data?.data || []))
      .catch(() => {})
  }, [projectId])

  const validateFile = (f) => {
    if (!ATTACH_ALLOWED.includes(extOf(f.name)))
      return `Type not allowed (${extOf(f.name) || 'no ext'})`
    if (f.size > ATTACH_MAX_BYTES)
      return `Too large (${fmtBytes(f.size)} > 25 MB)`
    if (f.size === 0) return 'File is empty'
    return null
  }

  const onPickFiles = async (e) => {
    const files = Array.from(e.target.files || [])
    e.target.value = '' // allow re-selecting the same file later
    if (!files.length) return

    if (!isEdit) {
      // Stage for upload after create
      setStaged(prev => [
        ...prev,
        ...files.map(f => ({ file: f, error: validateFile(f) })),
      ])
      return
    }

    // Edit mode → upload now
    setAttBusy(true)
    try {
      for (const f of files) {
        const err = validateFile(f)
        if (err) { toast.error(`${f.name}: ${err}`); continue }
        try {
          await ptAPI.attachments.upload(projectId, f)
          toast.success(`Uploaded ${f.name}`)
        } catch {
          toast.error(`Failed to upload ${f.name}`)
        }
      }
      const r = await ptAPI.attachments.list(projectId)
      setExisting(r.data?.data || [])
    } finally {
      setAttBusy(false)
    }
  }

  const removeStaged = (idx) => {
    setStaged(prev => prev.filter((_, i) => i !== idx))
  }

  const deleteExisting = async (aid, name) => {
    if (!confirm(`Remove attachment '${name}'?`)) return
    try {
      await ptAPI.attachments.delete(aid)
      setExisting(prev => prev.filter(a => a.ATTACHMENT_ID !== aid))
      toast.success('Removed')
    } catch {
      toast.error('Failed to remove')
    }
  }

  const downloadExisting = async (aid, name) => {
    try {
      const res = await ptAPI.attachments.download(aid)
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a')
      a.href = url; a.download = name
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      toast.error('Failed to download')
    }
  }

  const enumFor = (k) => (enums?.[k] && enums[k].length ? enums[k] : ENUM_FALLBACK[k])
  const setField = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.name.trim()) return
    // Block submit if any staged file has a validation error
    if (staged.some(s => s.error)) {
      toast.error('Fix attachment errors before saving')
      return
    }
    setBusy(true)
    try {
      const payload = { ...form }
      // Coerce empty strings → null on optional fields so backend Pydantic doesn't complain
      const optionalNull = ['phase', 'category', 'tags', 'owner_username', 'assignees',
                            'start_date', 'due_date', 'estimated_hours', 'description']
      optionalNull.forEach(k => { if (payload[k] === '') payload[k] = null })
      if (payload.estimated_hours !== null && payload.estimated_hours !== undefined)
        payload.estimated_hours = parseFloat(payload.estimated_hours) || null
      payload.progress_pct = Math.max(0, Math.min(100, parseInt(payload.progress_pct) || 0))
      if (payload.parent_id === '' || payload.parent_id === undefined) payload.parent_id = null
      if (payload.parent_id !== null) payload.parent_id = parseInt(payload.parent_id, 10)

      const savedId = await onSave(payload)

      // Upload staged files for newly-created project (best-effort)
      if (!isEdit && staged.length && savedId) {
        for (const s of staged) {
          if (s.error) continue
          try { await ptAPI.attachments.upload(savedId, s.file) }
          catch { toast.error(`Failed to upload ${s.file.name}`) }
        }
      }
    } finally {
      setBusy(false)
    }
  }

  // Eligible parents = depth < 2 (so the new node lands at depth ≤ 2 = TASK)
  const eligibleParents = parents.filter(p => {
    if (isEdit && p.PROJECT_ID === initial.PROJECT_ID) return false
    if (p.PROJECT_TYPE === 'TASK') return false
    return true
  })

  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(15,23,42,.55)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 1000,
    }}>
      <form onClick={e => e.stopPropagation()} onSubmit={handleSubmit}
        style={{
          width: 'min(720px, 95vw)', maxHeight: '90vh', overflow: 'auto',
          background: '#fff', borderRadius: 12,
          boxShadow: '0 20px 50px rgba(0,0,0,.25)',
        }}>
        {/* Header */}
        <div style={{
          padding: '16px 20px', borderBottom: '1px solid #e5e7eb',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          background: 'linear-gradient(90deg, #4f46e5 0%, #6366f1 100%)',
          color: '#fff', borderRadius: '12px 12px 0 0',
        }}>
          <div style={{ fontSize: 16, fontWeight: 700 }}>
            {isEdit ? `Edit ${initial.PROJECT_CODE}` : 'New Project'}
          </div>
          <button type="button" onClick={onClose} style={{
            background: 'transparent', border: 'none', color: '#fff',
            cursor: 'pointer', padding: 4, display: 'flex',
          }}><X size={18} /></button>
        </div>

        {/* Body */}
        <div style={{ padding: 20, display: 'grid', gap: 14 }}>
          <div>
            <label style={labelStyle}>Name *</label>
            <input style={inputStyle} required maxLength={255}
              value={form.name} onChange={e => setField('name', e.target.value)}
              placeholder="Concise project title" />
          </div>

          <div>
            <label style={labelStyle}>Description</label>
            <textarea style={{ ...inputStyle, minHeight: 80, fontFamily: 'inherit' }}
              value={form.description ?? ''}
              onChange={e => setField('description', e.target.value)}
              placeholder="What needs to be done? Why? Any relevant context." />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            <div>
              <label style={labelStyle}>Status</label>
              <select style={inputStyle} value={form.status}
                onChange={e => setField('status', e.target.value)}>
                {enumFor('status').map(v => <option key={v}>{v}</option>)}
              </select>
            </div>
            <div>
              <label style={labelStyle}>Priority</label>
              <select style={inputStyle} value={form.priority}
                onChange={e => setField('priority', e.target.value)}>
                {enumFor('priority').map(v => <option key={v}>{v}</option>)}
              </select>
            </div>
            <div>
              <label style={labelStyle}>Phase</label>
              <select style={inputStyle} value={form.phase ?? ''}
                onChange={e => setField('phase', e.target.value || null)}>
                <option value="">—</option>
                {enumFor('phase').map(v => <option key={v}>{v}</option>)}
              </select>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={labelStyle}>Category</label>
              <select style={inputStyle} value={form.category ?? ''}
                onChange={e => setField('category', e.target.value || null)}>
                <option value="">—</option>
                {enumFor('category').map(v => <option key={v}>{v}</option>)}
              </select>
            </div>
            <div>
              <label style={labelStyle}>Parent project</label>
              <select style={inputStyle} value={form.parent_id ?? ''}
                onChange={e => setField('parent_id', e.target.value || null)}>
                <option value="">— (root)</option>
                {eligibleParents.map(p => (
                  <option key={p.PROJECT_ID} value={p.PROJECT_ID}>
                    {p.PROJECT_CODE} • {p.NAME}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={labelStyle}>Owner (username)</label>
              <input style={inputStyle}
                value={form.owner_username ?? ''}
                onChange={e => setField('owner_username', e.target.value)}
                placeholder="Defaults to you" />
            </div>
            <div>
              <label style={labelStyle}>Assignees (CSV)</label>
              <input style={inputStyle}
                value={form.assignees ?? ''}
                onChange={e => setField('assignees', e.target.value)}
                placeholder="alice, bob, carol" />
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12 }}>
            <div>
              <label style={labelStyle}>Start date</label>
              <input type="date" style={inputStyle}
                value={form.start_date ?? ''}
                onChange={e => setField('start_date', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Due date</label>
              <input type="date" style={inputStyle}
                value={form.due_date ?? ''}
                onChange={e => setField('due_date', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Est. hours</label>
              <input type="number" step="0.5" min="0" style={inputStyle}
                value={form.estimated_hours ?? ''}
                onChange={e => setField('estimated_hours', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Progress %</label>
              <input type="number" min="0" max="100" style={inputStyle}
                value={form.progress_pct}
                onChange={e => setField('progress_pct', e.target.value)} />
            </div>
          </div>

          <div>
            <label style={labelStyle}>Tags</label>
            <input style={inputStyle}
              value={form.tags ?? ''}
              onChange={e => setField('tags', e.target.value)}
              placeholder="comma, separated, tags" />
          </div>

          {/* Attachments */}
          <div>
            <label style={labelStyle}>
              <Paperclip size={11} style={{ display: 'inline', marginRight: 4, verticalAlign: -1 }} />
              Attachments — sample / reference data
            </label>
            <div style={{
              border: '1px dashed #d1d5db', borderRadius: 6, padding: 10,
              background: '#f9fafb',
            }}>
              <input ref={fileInputRef} type="file" multiple
                accept={ATTACH_ALLOWED.join(',')}
                onChange={onPickFiles}
                style={{ display: 'none' }} />
              <button type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={attBusy}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '6px 12px', background: '#fff',
                  border: '1px solid #d1d5db', borderRadius: 6,
                  fontSize: 12, fontWeight: 600, cursor: attBusy ? 'wait' : 'pointer',
                  color: '#374151',
                }}>
                <Upload size={13} />
                {attBusy ? 'Uploading…' : 'Add files'}
              </button>
              <span style={{ marginLeft: 10, fontSize: 11, color: '#6b7280' }}>
                xlsx, xls, csv, pdf, docx, doc, txt, png, jpg, zip · max 25 MB each
              </span>

              {/* Existing (edit mode) */}
              {existing.length > 0 && (
                <div style={{ marginTop: 10, display: 'grid', gap: 6 }}>
                  {existing.map(a => (
                    <div key={a.ATTACHMENT_ID} style={attRowStyle}>
                      <Paperclip size={12} color="#6b7280" />
                      <button type="button"
                        onClick={() => downloadExisting(a.ATTACHMENT_ID, a.ORIGINAL_NAME)}
                        style={{ background: 'transparent', border: 'none', padding: 0,
                                 color: '#4f46e5', fontSize: 12, fontWeight: 500,
                                 textAlign: 'left', cursor: 'pointer', flex: 1,
                                 overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                        title={a.ORIGINAL_NAME}>
                        {a.ORIGINAL_NAME}
                      </button>
                      <span style={{ fontSize: 11, color: '#6b7280' }}>{fmtBytes(a.FILE_SIZE)}</span>
                      <span style={{ fontSize: 11, color: '#9ca3af' }}>{a.UPLOADED_BY}</span>
                      <button type="button" title="Download"
                        onClick={() => downloadExisting(a.ATTACHMENT_ID, a.ORIGINAL_NAME)}
                        style={iconLinkStyle}>
                        <Download size={13} />
                      </button>
                      <button type="button" title="Remove"
                        onClick={() => deleteExisting(a.ATTACHMENT_ID, a.ORIGINAL_NAME)}
                        style={{ ...iconLinkStyle, color: '#dc2626' }}>
                        <Trash2 size={13} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* Staged (create mode) */}
              {staged.length > 0 && (
                <div style={{ marginTop: 10, display: 'grid', gap: 6 }}>
                  {staged.map((s, i) => (
                    <div key={i} style={{ ...attRowStyle,
                                          background: s.error ? '#fef2f2' : '#eef2ff',
                                          borderColor: s.error ? '#fecaca' : '#c7d2fe' }}>
                      <Paperclip size={12} color={s.error ? '#dc2626' : '#4f46e5'} />
                      <span style={{ fontSize: 12, fontWeight: 500, flex: 1,
                                     color: s.error ? '#991b1b' : '#374151',
                                     overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                            title={s.file.name}>
                        {s.file.name}
                      </span>
                      <span style={{ fontSize: 11, color: '#6b7280' }}>{fmtBytes(s.file.size)}</span>
                      {s.error && (
                        <span style={{ fontSize: 11, color: '#dc2626' }}>{s.error}</span>
                      )}
                      <button type="button" title="Remove"
                        onClick={() => removeStaged(i)}
                        style={{ ...iconLinkStyle, color: '#dc2626' }}>
                        <X size={13} />
                      </button>
                    </div>
                  ))}
                  {!isEdit && (
                    <div style={{ fontSize: 11, color: '#6b7280', marginTop: 2 }}>
                      These files will upload after the project is created.
                    </div>
                  )}
                </div>
              )}

              {existing.length === 0 && staged.length === 0 && (
                <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 8 }}>
                  No files attached yet.
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div style={{
          padding: '14px 20px', borderTop: '1px solid #e5e7eb',
          display: 'flex', justifyContent: 'flex-end', gap: 10,
        }}>
          <button type="button" onClick={onClose} disabled={busy} style={{
            padding: '8px 18px', borderRadius: 6, border: '1px solid #d1d5db',
            background: '#fff', cursor: 'pointer', fontSize: 13, fontWeight: 600,
          }}>Cancel</button>
          <button type="submit" disabled={busy} style={{
            padding: '8px 18px', borderRadius: 6, border: 'none',
            background: '#4f46e5', color: '#fff',
            cursor: busy ? 'not-allowed' : 'pointer',
            fontSize: 13, fontWeight: 600, opacity: busy ? 0.7 : 1,
          }}>{busy ? 'Saving…' : (isEdit ? 'Save changes' : 'Create')}</button>
        </div>
      </form>
    </div>
  )
}

const attRowStyle = {
  display: 'flex', alignItems: 'center', gap: 8,
  padding: '6px 10px', borderRadius: 6,
  background: '#fff', border: '1px solid #e5e7eb',
}

const iconLinkStyle = {
  background: 'transparent', border: 'none', cursor: 'pointer',
  padding: 4, display: 'inline-flex', alignItems: 'center',
  color: '#6b7280', borderRadius: 4, textDecoration: 'none',
}
