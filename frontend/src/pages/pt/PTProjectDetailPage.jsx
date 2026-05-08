// Project Tracker — Project detail page
// Header summary, breadcrumb, children, activity log.
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Pencil, Archive, Plus, Calendar, User, Tag, Clock } from 'lucide-react'
import toast from 'react-hot-toast'
import { ptAPI } from '@/services/api'
import { StatusBadge, PriorityChip, PhaseChip } from '@/components/pt/StatusBadge'
import StatusPicker from '@/components/pt/StatusPicker'
import ProjectForm from '@/components/pt/ProjectForm'

const fieldStyle = {
  fontSize: 11, fontWeight: 600, color: '#6b7280',
  textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4,
}

function MetaRow({ icon: Icon, label, value }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#374151' }}>
      <Icon size={14} color="#9ca3af" />
      <span style={{ color: '#6b7280', minWidth: 70 }}>{label}</span>
      <span>{value || '—'}</span>
    </div>
  )
}

function ActivityFeed({ items }) {
  if (!items?.length) return <div style={{ color: '#6b7280', fontSize: 12, padding: '8px 0' }}>No activity yet.</div>
  return (
    <div>
      {items.map(a => (
        <div key={a.ACTIVITY_ID} style={{
          padding: '10px 0', borderBottom: '1px solid #f1f5f9',
          display: 'flex', gap: 10,
        }}>
          <div style={{
            width: 8, height: 8, marginTop: 6, borderRadius: 999,
            background: a.ACTIVITY_TYPE === 'CREATED' ? '#16a34a' :
                       a.ACTIVITY_TYPE === 'ARCHIVED' ? '#dc2626' : '#4f46e5',
            flexShrink: 0,
          }} />
          <div style={{ flex: 1, fontSize: 12 }}>
            <div style={{ color: '#111827' }}>
              <strong>{a.ACTOR}</strong>{' '}
              {a.ACTIVITY_TYPE === 'CREATED' && 'created this project'}
              {a.ACTIVITY_TYPE === 'ARCHIVED' && 'archived this project'}
              {a.ACTIVITY_TYPE === 'RESTORED' && 'restored this project'}
              {a.ACTIVITY_TYPE === 'MOVED' && 'moved this project'}
              {a.ACTIVITY_TYPE === 'FIELD_CHANGED' && (
                <> changed <code style={{ background: '#f3f4f6', padding: '1px 5px', borderRadius: 3, fontSize: 11 }}>{a.FIELD_NAME}</code>
                {' from '}<em>{a.OLD_VALUE ?? '—'}</em>{' to '}<em>{a.NEW_VALUE ?? '—'}</em></>
              )}
            </div>
            {a.DETAILS && <div style={{ color: '#6b7280', fontSize: 11, marginTop: 2 }}>{a.DETAILS}</div>}
            <div style={{ color: '#9ca3af', fontSize: 10, marginTop: 2 }}>
              {new Date(a.CREATED_AT).toLocaleString()}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

export default function PTProjectDetailPage() {
  const { id } = useParams()
  const nav = useNavigate()
  const [project, setProject]   = useState(null)
  const [ancestors, setAncestors] = useState([])
  const [children, setChildren] = useState([])
  const [activity, setActivity] = useState([])
  const [enums, setEnums]       = useState(null)
  const [allProjects, setAll]   = useState([])
  const [loading, setLoading]   = useState(true)
  const [editing, setEditing]   = useState(false)
  const [creatingChild, setCreatingChild] = useState(false)

  const load = () => {
    setLoading(true)
    Promise.all([
      ptAPI.get(id),
      ptAPI.activity(id),
    ]).then(([detail, act]) => {
      const d = detail.data?.data
      setProject(d?.project)
      setAncestors(d?.ancestors || [])
      setChildren(d?.children || [])
      setActivity(act.data?.data || [])
    }).finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [id])
  useEffect(() => {
    ptAPI.enums().then(res => setEnums(res.data?.data))
    ptAPI.tree({ archived: false }).then(res => setAll(res.data?.data || []))
  }, [editing, creatingChild])

  if (loading) return <div style={{ padding: 24, color: '#6b7280' }}>Loading…</div>
  if (!project) return <div style={{ padding: 24 }}>Not found.</div>

  const handleSave = async (payload) => {
    try {
      if (creatingChild) {
        payload.parent_id = project.PROJECT_ID
        const res = await ptAPI.create(payload)
        toast.success(`Created ${res.data?.data?.project_code}`)
      } else {
        await ptAPI.update(project.PROJECT_ID, payload)
        toast.success('Updated')
      }
      setEditing(false); setCreatingChild(false)
      load()
    } catch (e) {}
  }
  const handleArchive = async () => {
    if (!confirm(`Archive '${project.NAME}' and all sub-projects?`)) return
    try {
      await ptAPI.archive(project.PROJECT_ID)
      toast.success('Archived')
      nav('/pt/projects')
    } catch (e) {}
  }

  return (
    <div style={{ padding: 20, background: '#f8fafc', minHeight: '100%' }}>
      {/* Breadcrumb */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 12, fontSize: 12, color: '#6b7280' }}>
        <button onClick={() => nav('/pt/projects')} style={{
          background: 'transparent', border: 'none', cursor: 'pointer',
          color: '#4f46e5', display: 'flex', alignItems: 'center', gap: 4, padding: 0,
        }}><ArrowLeft size={14} /> All projects</button>
        {ancestors.map(a => (
          <span key={a.PROJECT_ID}>
            <span style={{ margin: '0 6px' }}>/</span>
            <a onClick={() => nav(`/pt/projects/${a.PROJECT_ID}`)}
              style={{ color: '#4f46e5', cursor: 'pointer' }}>{a.NAME}</a>
          </span>
        ))}
        <span style={{ margin: '0 6px' }}>/</span>
        <span style={{ color: '#111827', fontWeight: 600 }}>{project.NAME}</span>
      </div>

      {/* Header card */}
      <div style={{
        background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb',
        padding: 18, marginBottom: 14,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 14 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: 'monospace', fontSize: 11, color: '#4f46e5', marginBottom: 4 }}>
              {project.PROJECT_CODE} • {project.PROJECT_TYPE}
            </div>
            <div style={{ fontSize: 22, fontWeight: 700, color: '#111827', marginBottom: 6 }}>
              {project.NAME}
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12, alignItems: 'center' }}>
              <StatusPicker value={project.STATUS} onChange={async (next) => {
                await ptAPI.update(project.PROJECT_ID, { status: next })
                toast.success(`Status → ${next}`)
                load()
              }} />
              <PriorityChip value={project.PRIORITY} />
              <PhaseChip value={project.PHASE} />
              {project.CATEGORY && (
                <span style={{
                  display: 'inline-block', padding: '2px 8px', borderRadius: 999,
                  fontSize: 11, fontWeight: 600, background: '#f3f4f6', color: '#374151',
                }}>{project.CATEGORY}</span>
              )}
            </div>
            {project.DESCRIPTION && (
              <div style={{
                fontSize: 13, color: '#374151', whiteSpace: 'pre-wrap',
                lineHeight: 1.5, marginBottom: 12,
              }}>{project.DESCRIPTION}</div>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 10, maxWidth: 600 }}>
              <MetaRow icon={User}     label="Owner"     value={project.OWNER_USERNAME} />
              <MetaRow icon={User}     label="Assignees" value={project.ASSIGNEES} />
              <MetaRow icon={Calendar} label="Start"     value={project.START_DATE?.slice(0,10)} />
              <MetaRow icon={Calendar} label="Due"       value={project.DUE_DATE?.slice(0,10)} />
              <MetaRow icon={Clock}    label="Estimate"  value={project.ESTIMATED_HOURS ? `${project.ESTIMATED_HOURS}h` : null} />
              <MetaRow icon={Clock}    label="Actual"    value={project.ACTUAL_HOURS ? `${project.ACTUAL_HOURS}h` : null} />
              <MetaRow icon={Tag}      label="Tags"      value={project.TAGS} />
              <MetaRow icon={Tag}      label="Created"   value={project.CREATED_AT?.slice(0,16).replace('T',' ')} />
            </div>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <button onClick={() => setEditing(true)} style={btnPrimary}>
              <Pencil size={14} /> Edit
            </button>
            <button onClick={handleArchive} style={btnDanger}>
              <Archive size={14} /> Archive
            </button>
          </div>
        </div>

        {/* Progress */}
        <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: '#6b7280', textTransform: 'uppercase' }}>Progress</span>
          <div style={{ flex: 1, height: 8, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden', maxWidth: 400 }}>
            <div style={{
              height: '100%', width: `${project.PROGRESS_PCT || 0}%`,
              background: project.PROGRESS_PCT >= 100 ? '#16a34a' : '#4f46e5',
            }} />
          </div>
          <span style={{ fontSize: 12, fontWeight: 600, color: '#111827' }}>
            {project.PROGRESS_PCT || 0}%{project.AUTO_PROGRESS ? ' (auto)' : ''}
          </span>
        </div>
      </div>

      {/* Children + Activity grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 14 }}>
        {/* Children */}
        <div style={{
          background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb',
          padding: 14,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              Sub-projects {children.length > 0 && <span style={{ color: '#6b7280' }}>({children.length})</span>}
            </div>
            {project.PROJECT_TYPE !== 'TASK' && (
              <button onClick={() => setCreatingChild(true)} style={{ ...btnPrimary, padding: '5px 10px', fontSize: 11 }}>
                <Plus size={12} /> Add child
              </button>
            )}
          </div>
          {children.length === 0 ? (
            <div style={{ color: '#6b7280', fontSize: 12, padding: '8px 0' }}>
              {project.PROJECT_TYPE === 'TASK' ? 'Tasks have no children.' : 'No sub-projects yet.'}
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <tbody>
                {children.map(c => (
                  <tr key={c.PROJECT_ID} onClick={() => nav(`/pt/projects/${c.PROJECT_ID}`)}
                      style={{ borderBottom: '1px solid #f1f5f9', cursor: 'pointer' }}>
                    <td style={{ padding: '8px 4px', fontFamily: 'monospace', color: '#4f46e5', whiteSpace: 'nowrap' }}>
                      {c.PROJECT_CODE}
                    </td>
                    <td style={{ padding: '8px 4px' }}>{c.NAME}</td>
                    <td style={{ padding: '8px 4px' }} onClick={e => e.stopPropagation()}>
                      <StatusPicker value={c.STATUS} onChange={async (next) => {
                        await ptAPI.update(c.PROJECT_ID, { status: next })
                        toast.success(`${c.PROJECT_CODE} → ${next}`)
                        load()
                      }} />
                    </td>
                    <td style={{ padding: '8px 4px' }}><PriorityChip value={c.PRIORITY} /></td>
                    <td style={{ padding: '8px 4px', color: c.IS_OVERDUE ? '#dc2626' : '#374151' }}>
                      {c.DUE_DATE?.slice(0,10) || '—'}
                    </td>
                    <td style={{ padding: '8px 4px', textAlign: 'right', color: '#374151' }}>
                      {c.PROGRESS_PCT}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Activity */}
        <div style={{
          background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb',
          padding: 14, maxHeight: 600, overflow: 'auto',
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Activity log</div>
          <ActivityFeed items={activity} />
        </div>
      </div>

      {(editing || creatingChild) && (
        <ProjectForm
          initial={editing ? project : null}
          parents={allProjects}
          enums={enums}
          onSave={handleSave}
          onClose={() => { setEditing(false); setCreatingChild(false) }}
        />
      )}
    </div>
  )
}

const btnPrimary = {
  padding: '7px 14px', background: '#4f46e5', color: '#fff', border: 'none',
  borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600,
  display: 'flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap',
}
const btnDanger = {
  ...btnPrimary, background: '#fff', color: '#dc2626', border: '1px solid #fecaca',
}
