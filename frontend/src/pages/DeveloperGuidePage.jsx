import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import {
  Code2, RefreshCw, Search, FileCode2, Database, Server, Layers, GitCommit,
  AlertCircle, FileText, ChevronRight, ChevronDown, ExternalLink, Terminal,
  BookOpen, X, ClipboardCopy, Check, Folder, FolderOpen, Maximize2, Minimize2,
  Hash, Link as LinkIcon
} from 'lucide-react'
import clsx from 'clsx'
import api from '@/services/api'
import toast from 'react-hot-toast'

/* =========================================================================
   Developer Guide — auto-introspecting view of the live ARS app.

   No hand-maintained content here. Every section pulls from
   /api/v1/dev-guide/index, which inspects:
     - the running FastAPI app for routes
     - app/services/*.py for service classes (via ast)
     - frontend/src/pages/*.jsx for the page list
     - INFORMATION_SCHEMA for table list
     - git log for recent activity
     - app/docs/dev_guide/*.md for optional developer-authored notes

   Add/change code → reload → page reflects the change. No SOP rot.
   ========================================================================= */

const TABS = [
  { id: 'overview',   label: 'Overview',         icon: BookOpen },
  { id: 'processes',  label: 'Processes (Notes)', icon: FileText },
  { id: 'routes',     label: 'API Routes',        icon: Server },
  { id: 'services',   label: 'Services',          icon: Layers },
  { id: 'pages',      label: 'Frontend Pages',    icon: FileCode2 },
  { id: 'tables',     label: 'Database Tables',   icon: Database },
  { id: 'recent',     label: 'Recent Changes',    icon: GitCommit },
]

const HTTP_COLORS = {
  GET:    'bg-emerald-100 text-emerald-700',
  POST:   'bg-blue-100 text-blue-700',
  PUT:    'bg-amber-100 text-amber-700',
  PATCH:  'bg-amber-100 text-amber-700',
  DELETE: 'bg-rose-100 text-rose-700',
}

/* ────────────────────────── Markdown renderer (small subset) ────────────── */
function renderInline(text, kp = '') {
  const parts = []
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)]+\))/g
  let last = 0, m, i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) {
      parts.push(<code key={`${kp}-c-${i++}`} className="px-1.5 py-0.5 rounded bg-gray-100 text-[12px] text-rose-700 font-mono">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('**')) {
      parts.push(<strong key={`${kp}-b-${i++}`}>{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('*')) {
      parts.push(<em key={`${kp}-i-${i++}`}>{tok.slice(1, -1)}</em>)
    } else if (tok.startsWith('[')) {
      const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok)
      if (mm) parts.push(<a key={`${kp}-l-${i++}`} href={mm[2]} className="text-indigo-600 hover:underline" target="_blank" rel="noreferrer">{mm[1]}</a>)
      else parts.push(tok)
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function renderMarkdown(md) {
  if (!md) return null
  const lines = md.split('\n')
  const blocks = []
  let i = 0, k = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }
    if (line.startsWith('```')) {
      const buf = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) { buf.push(lines[i]); i++ }
      i++
      blocks.push(<pre key={`b-${k++}`} className="my-3 p-3 rounded-lg bg-gray-900 text-gray-100 text-[12px] font-mono overflow-x-auto whitespace-pre">{buf.join('\n')}</pre>)
      continue
    }
    if (/^#{1,4}\s+/.test(line)) {
      const level = (line.match(/^#+/)[0]).length
      const text = line.replace(/^#+\s+/, '')
      const Tag = `h${Math.min(level + 1, 6)}`
      const sizes = { 1: 'text-2xl', 2: 'text-xl', 3: 'text-lg', 4: 'text-base' }
      blocks.push(<Tag key={`b-${k++}`} className={`${sizes[level] || 'text-base'} font-bold text-gray-900 mt-5 mb-2`}>{renderInline(text, `h-${k}`)}</Tag>)
      i++; continue
    }
    if (line.startsWith('|') && lines[i+1] && /^\|[\s|:-]+\|/.test(lines[i+1])) {
      const rows = []
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(lines[i]); i++ }
      const strip = (l) => l.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())
      const header = strip(rows[0])
      const body = rows.slice(2).map(strip)
      blocks.push(
        <div key={`b-${k++}`} className="my-3 overflow-x-auto rounded-lg border border-gray-200">
          <table className="min-w-full text-[12px]">
            <thead className="bg-gray-50 text-gray-700">
              <tr>{header.map((h, j) => <th key={j} className="px-3 py-2 text-left font-semibold border-b">{renderInline(h, `th-${j}`)}</th>)}</tr>
            </thead>
            <tbody>
              {body.map((r, ri) => <tr key={ri} className={ri % 2 ? 'bg-gray-50/50' : 'bg-white'}>
                {r.map((c, ci) => <td key={ci} className="px-3 py-1.5 align-top border-b text-gray-800">{renderInline(c, `td-${ri}-${ci}`)}</td>)}
              </tr>)}
            </tbody>
          </table>
        </div>
      )
      continue
    }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      const items = []
      while (i < lines.length && (lines[i].startsWith('- ') || lines[i].startsWith('* '))) {
        items.push(lines[i].slice(2)); i++
      }
      blocks.push(<ul key={`b-${k++}`} className="my-2 ml-5 list-disc space-y-1 text-[13px] text-gray-800">
        {items.map((it, j) => <li key={j}>{renderInline(it, `li-${j}`)}</li>)}
      </ul>)
      continue
    }
    if (/^\d+\.\s/.test(line)) {
      const items = []
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s/, '')); i++
      }
      blocks.push(<ol key={`b-${k++}`} className="my-2 ml-5 list-decimal space-y-1 text-[13px] text-gray-800">
        {items.map((it, j) => <li key={j}>{renderInline(it, `oli-${j}`)}</li>)}
      </ol>)
      continue
    }
    blocks.push(<p key={`b-${k++}`} className="my-2 text-[13px] text-gray-800 leading-6">{renderInline(line, `p-${k}`)}</p>)
    i++
  }
  return blocks
}

/* ────────────────────────── File viewer modal ──────────────────────────── */
function FileModal({ file, onClose }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!file) return
    setLoading(true); setErr('')
    api.get('/dev-guide/file', { params: { path: file.path } })
      .then(res => setData(res.data))
      .catch(e => setErr(e.response?.data?.detail || e.message))
      .finally(() => setLoading(false))
  }, [file])

  if (!file) return null

  const focusLine = file.line || 0

  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-lg w-full max-w-5xl max-h-[90vh] flex flex-col overflow-hidden" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-4 py-2.5 border-b bg-gray-50">
          <div className="flex items-center gap-2 min-w-0">
            <FileCode2 size={16} className="text-indigo-600 shrink-0" />
            <code className="text-[12px] font-mono text-gray-700 truncate">{file.path}</code>
            {focusLine > 0 && <span className="text-[11px] text-gray-500 shrink-0">:{focusLine}</span>}
            {data && <span className="text-[10px] text-gray-400 shrink-0">{data.lines} lines · {(data.size_bytes/1024).toFixed(1)} KB</span>}
          </div>
          <div className="flex items-center gap-2">
            {data && (
              <button
                onClick={() => { navigator.clipboard.writeText(data.content); setCopied(true); setTimeout(() => setCopied(false), 1500) }}
                className="text-[11px] text-gray-600 hover:text-indigo-600 flex items-center gap-1 px-2 py-1 rounded hover:bg-gray-100"
              >
                {copied ? <><Check size={12}/> Copied</> : <><ClipboardCopy size={12}/> Copy</>}
              </button>
            )}
            <button onClick={onClose} className="text-gray-500 hover:text-gray-900"><X size={18}/></button>
          </div>
        </div>
        <div className="flex-1 overflow-auto bg-gray-900">
          {loading && <div className="p-6 text-gray-300 text-[13px]">Loading…</div>}
          {err && <div className="p-6 text-rose-300 text-[13px]">{err}</div>}
          {data && (
            <pre className="text-[12px] font-mono leading-5 text-gray-100 p-0 m-0">
              {data.content.split('\n').map((line, idx) => {
                const ln = idx + 1
                const isFocus = ln === focusLine
                return (
                  <div key={ln} className={clsx('flex', isFocus && 'bg-amber-900/40')}>
                    <span className="select-none w-12 text-right pr-3 text-gray-500 border-r border-gray-700 shrink-0">{ln}</span>
                    <span className="pl-3 whitespace-pre">{line || ' '}</span>
                  </div>
                )
              })}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

/* ────────────────────────── Note viewer modal ─────────────────────────── */
/* ─────────── Note content viewer (used inline, no modal) ─────────────── */
function NoteContent({ slug, onPickSlug }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (!slug) return
    setLoading(true); setErr('')
    setData(null)
    api.get(`/dev-guide/note/${encodeURIComponent(slug)}`)
      .then(res => setData(res.data))
      .catch(e => setErr(e.response?.data?.detail || e.message))
      .finally(() => setLoading(false))
  }, [slug])

  if (!slug) {
    return (
      <div className="h-full flex items-center justify-center text-gray-400 text-[13px] p-8">
        <div className="text-center max-w-md">
          <FileText size={36} className="mx-auto mb-3 opacity-40" />
          <p className="font-medium text-gray-600 mb-1">Select a topic from the tree</p>
          <p className="text-[12px]">
            Sections expand into individual pages. Each page covers one
            module: what it does, how it works, examples, and how to extend.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Breadcrumb header */}
      <div className="px-5 py-2.5 border-b bg-gray-50 flex items-center justify-between sticky top-0 z-10">
        <div className="flex items-center gap-1.5 text-[12px] text-gray-700 min-w-0 flex-wrap">
          <FileText size={14} className="text-indigo-600 shrink-0" />
          {slug.split('/').map((part, idx, arr) => {
            const partial = arr.slice(0, idx + 1).join('/')
            const isLast = idx === arr.length - 1
            return (
              <span key={partial} className="flex items-center gap-1.5">
                <button
                  onClick={() => onPickSlug && onPickSlug(partial)}
                  className={clsx(
                    'hover:text-indigo-600 truncate max-w-xs',
                    isLast ? 'font-semibold text-gray-900' : 'text-gray-500'
                  )}
                  title={partial}
                >
                  {part.replace(/^\d+[_\-]/, '').replace(/_/g, ' ')}
                </button>
                {!isLast && <ChevronRight size={11} className="text-gray-400 shrink-0" />}
              </span>
            )
          })}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {data?.modified_at && (
            <span className="text-[10px] text-gray-400">edited {data.modified_at}</span>
          )}
          <button
            onClick={() => {
              const url = `${window.location.origin}${window.location.pathname}#/${slug}`
              navigator.clipboard.writeText(url)
              toast.success('Link copied')
            }}
            className="text-gray-400 hover:text-indigo-600"
            title="Copy direct link"
          >
            <LinkIcon size={13} />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto px-6 py-5">
        {loading && <div className="text-gray-500 text-[13px]">Loading…</div>}
        {err && (
          <div className="rounded bg-rose-50 border border-rose-200 p-3 text-rose-800 text-[13px]">
            <div className="font-semibold mb-1">Could not load note</div>
            <div>{err}</div>
            <p className="mt-2 text-[12px]">
              If this is a folder without an <code>_index.md</code>, pick a
              specific child page from the tree.
            </p>
          </div>
        )}
        {data && (
          <article className="max-w-4xl mx-auto prose-sm">
            {renderMarkdown(data.content)}
          </article>
        )}
      </div>
    </div>
  )
}


/* ─────────── Recursive tree node (folder + leaf notes) ──────────────── */
function TreeNode({ node, level, openFolders, onToggle, onPickSlug, selectedSlug, filterText }) {
  const isFolder = node.type === 'folder'
  const isOpen = openFolders[node.slug] ?? (level === 0)  // root folders open by default
  const matchesFilter = !filterText
    || (node.title || '').toLowerCase().includes(filterText)
    || (node.slug || '').toLowerCase().includes(filterText)

  // For folders: show if itself matches OR any descendant matches (handled by caller).
  if (isFolder) {
    return (
      <div>
        <div className="flex items-center">
          <button
            onClick={() => onToggle(node.slug)}
            className="p-0.5 text-gray-400 hover:text-gray-700 shrink-0"
          >
            {isOpen ? <ChevronDown size={13}/> : <ChevronRight size={13}/>}
          </button>
          <button
            onClick={() => node.has_index && onPickSlug(node.slug)}
            className={clsx(
              'flex items-center gap-1.5 px-1.5 py-1 rounded text-[12px] flex-1 text-left min-w-0',
              node.has_index && selectedSlug === node.slug
                ? 'bg-indigo-100 text-indigo-700 font-medium'
                : node.has_index ? 'text-gray-800 hover:bg-gray-100' : 'text-gray-700 cursor-default',
            )}
            style={{ paddingLeft: `${level * 4}px` }}
            disabled={!node.has_index}
            title={node.has_index ? 'Open section overview' : 'No overview — pick a child'}
          >
            {isOpen ? <FolderOpen size={13} className="text-amber-600 shrink-0"/> : <Folder size={13} className="text-amber-500 shrink-0"/>}
            <span className="truncate">{node.title}</span>
            {node.children?.length > 0 && (
              <span className="ml-auto text-[9px] text-gray-400 shrink-0">{node.children.length}</span>
            )}
          </button>
        </div>
        {isOpen && node.children?.length > 0 && (
          <div className="ml-3 border-l border-gray-200 pl-1.5 space-y-0.5 mt-0.5">
            {node.children
              .filter(child => {
                if (!filterText) return true
                // Folder visible if it has a matching descendant
                const hay = JSON.stringify(child).toLowerCase()
                return hay.includes(filterText)
              })
              .map(child => (
                <TreeNode
                  key={child.slug}
                  node={child}
                  level={level + 1}
                  openFolders={openFolders}
                  onToggle={onToggle}
                  onPickSlug={onPickSlug}
                  selectedSlug={selectedSlug}
                  filterText={filterText}
                />
              ))}
          </div>
        )}
      </div>
    )
  }

  // Leaf note
  if (filterText && !matchesFilter) return null
  return (
    <button
      onClick={() => onPickSlug(node.slug)}
      className={clsx(
        'w-full flex items-center gap-1.5 px-1.5 py-1 rounded text-[12px] text-left',
        selectedSlug === node.slug
          ? 'bg-indigo-100 text-indigo-700 font-medium'
          : 'text-gray-700 hover:bg-gray-100'
      )}
      style={{ paddingLeft: `${level * 4 + 14}px` }}
    >
      <FileText size={11} className="text-gray-400 shrink-0"/>
      <span className="truncate">{node.title}</span>
    </button>
  )
}

/* ────────────────────────── Main page ─────────────────────────────────── */
export default function DeveloperGuidePage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState(() => {
    // Deep-link: if there's a #/<slug> hash, jump to the Processes tab
    if (typeof window !== 'undefined' && window.location.hash.startsWith('#/')) return 'processes'
    return 'overview'
  })
  const [search, setSearch] = useState('')
  const [openFile, setOpenFile] = useState(null)

  const fetchIndex = useCallback(() => {
    setLoading(true); setErr('')
    api.get('/dev-guide/index')
      .then(res => setData(res.data))
      .catch(e => setErr(e.response?.data?.detail || e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { fetchIndex() }, [fetchIndex])

  const refresh = useCallback(async () => {
    try {
      await api.post('/dev-guide/refresh')
      toast.success('Cache cleared — re-introspecting…')
      fetchIndex()
    } catch (e) {
      toast.error('Refresh failed')
    }
  }, [fetchIndex])

  const filterText = search.trim().toLowerCase()
  const matches = useCallback((s) => !filterText || String(s || '').toLowerCase().includes(filterText), [filterText])

  /* ── Filtered slices ─────────────────────────────────────────────────── */
  const routes = useMemo(() => {
    if (!data) return []
    return data.routes.filter(r =>
      matches(r.path) || matches(r.method) || matches(r.summary)
      || matches(r.file) || matches(r.function) || (r.tags || []).some(matches)
    )
  }, [data, matches])

  const services = useMemo(() => {
    if (!data) return []
    return data.services.filter(s =>
      matches(s.name) || matches(s.module_doc)
      || (s.classes || []).some(c => matches(c.name) || matches(c.doc))
    )
  }, [data, matches])

  const pages = useMemo(() => {
    if (!data) return []
    return data.pages.filter(p => matches(p.component) || matches(p.route) || matches(p.hint))
  }, [data, matches])

  const tables = useMemo(() => {
    if (!data) return []
    return data.tables.filter(t => matches(t.table))
  }, [data, matches])

  const recent = useMemo(() => {
    if (!data) return []
    return data.git_recent.filter(c =>
      matches(c.subject) || matches(c.author) || (c.files || []).some(matches)
    )
  }, [data, matches])

  /* ── Render ──────────────────────────────────────────────────────────── */
  if (loading && !data) {
    return <div className="p-8 text-gray-500 text-[13px]">Loading developer guide…</div>
  }
  if (err && !data) {
    return (
      <div className="p-8">
        <div className="rounded-lg bg-rose-50 border border-rose-200 p-4 text-rose-800 text-[13px] flex items-start gap-2">
          <AlertCircle size={16} className="shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold mb-1">Could not load developer guide</div>
            <div>{err}</div>
            <button onClick={fetchIndex} className="mt-3 px-3 py-1 bg-rose-600 text-white rounded text-[12px]">Retry</button>
          </div>
        </div>
      </div>
    )
  }

  const stats = data?.stats || {}

  return (
    <div className="p-4 max-w-[1400px] mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shrink-0">
            <Code2 size={20} className="text-white" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-900 leading-tight">Developer Guide</h1>
            <p className="text-[12px] text-gray-600 mt-0.5">
              Live introspection of the running app. Updates itself automatically — no manual SOP maintenance.
            </p>
            <div className="text-[10px] text-gray-400 mt-1">
              Generated {data?.generated_at} · {stats.route_count} routes · {stats.service_count} services
              · {stats.page_count} pages · {stats.table_count} tables · {stats.commit_count} recent commits
            </div>
          </div>
        </div>
        <button
          onClick={refresh}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-indigo-600 text-white text-[12px] font-medium hover:bg-indigo-700 disabled:opacity-50"
          disabled={loading}
        >
          <RefreshCw size={13} className={clsx(loading && 'animate-spin')} />
          Re-introspect
        </button>
      </div>

      {/* Search */}
      <div className="relative mb-3">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search across routes, services, pages, tables, commits…"
          className="w-full pl-9 pr-9 py-2 border border-gray-300 rounded-md text-[13px] focus:outline-none focus:ring-2 focus:ring-indigo-400"
        />
        {search && (
          <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
            <X size={14}/>
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex flex-wrap gap-1 mb-4 border-b border-gray-200">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={clsx(
              'flex items-center gap-1.5 px-3 py-2 text-[12px] font-medium border-b-2 -mb-px transition-colors',
              tab === t.id
                ? 'border-indigo-600 text-indigo-700'
                : 'border-transparent text-gray-600 hover:text-gray-900'
            )}
          >
            <t.icon size={14}/>
            {t.label}
          </button>
        ))}
      </div>

      {/* ───── Tab: Overview ─────────────────────────────────────────── */}
      {tab === 'overview' && (
        <OverviewTab
          data={data}
          stats={stats}
          onOpenNote={(slug) => { window.location.hash = `#/${slug}`; setTab('processes') }}
          onPickTab={setTab}
        />
      )}

      {/* ───── Tab: Processes (notes) ────────────────────────────────── */}
      {tab === 'processes' && (
        <NotesTab tree={data?.notes_tree || []} flatNotes={data?.notes || []} />
      )}

      {/* ───── Tab: Routes ───────────────────────────────────────────── */}
      {tab === 'routes' && (
        <RoutesTab routes={routes} onOpenFile={setOpenFile} />
      )}

      {/* ───── Tab: Services ─────────────────────────────────────────── */}
      {tab === 'services' && (
        <ServicesTab services={services} onOpenFile={setOpenFile} />
      )}

      {/* ───── Tab: Pages ────────────────────────────────────────────── */}
      {tab === 'pages' && (
        <PagesTab pages={pages} onOpenFile={setOpenFile} />
      )}

      {/* ───── Tab: Tables ───────────────────────────────────────────── */}
      {tab === 'tables' && (
        <TablesTab tables={tables} />
      )}

      {/* ───── Tab: Recent ───────────────────────────────────────────── */}
      {tab === 'recent' && (
        <RecentTab recent={recent} onOpenFile={setOpenFile} />
      )}

      {/* Modals */}
      {openFile && <FileModal file={openFile} onClose={() => setOpenFile(null)} />}
    </div>
  )
}

/* ────────────────────────── Tab components ────────────────────────────── */

function OverviewTab({ data, stats, onOpenNote, onPickTab }) {
  return (
    <div className="space-y-4">
      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
        {[
          { l: 'Routes', v: stats.route_count, c: 'from-blue-500 to-indigo-600', tab: 'routes' },
          { l: 'Services', v: stats.service_count, c: 'from-emerald-500 to-teal-600', tab: 'services' },
          { l: 'Pages', v: stats.page_count, c: 'from-amber-500 to-orange-600', tab: 'pages' },
          { l: 'Tables', v: stats.table_count, c: 'from-purple-500 to-pink-600', tab: 'tables' },
          { l: 'Recent commits', v: stats.commit_count, c: 'from-rose-500 to-red-600', tab: 'recent' },
          { l: 'Notes', v: stats.note_count, c: 'from-slate-500 to-gray-700', tab: 'processes' },
        ].map(s => (
          <button
            key={s.l}
            onClick={() => onPickTab(s.tab)}
            className={`text-left rounded-lg bg-gradient-to-br ${s.c} text-white p-3 hover:scale-[1.01] transition-transform`}
          >
            <div className="text-[10px] uppercase tracking-wide opacity-80">{s.l}</div>
            <div className="text-2xl font-bold leading-tight">{s.v?.toLocaleString() ?? 0}</div>
          </button>
        ))}
      </div>

      {/* Notes (if any) */}
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <BookOpen size={15} className="text-indigo-600"/>
            <h2 className="text-[14px] font-semibold text-gray-900">Start here — developer notes</h2>
          </div>
          <button onClick={() => onPickTab('processes')} className="text-[11px] text-indigo-600 hover:underline">View all →</button>
        </div>
        {!data?.notes?.length && (
          <p className="text-[12px] text-gray-500 italic">
            No notes yet. Drop a markdown file in <code className="px-1 bg-gray-100 rounded">backend/app/docs/dev_guide/</code> and it'll show up here automatically.
          </p>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mt-2">
          {(data?.notes || []).slice(0, 6).map(n => (
            <button
              key={n.slug}
              onClick={() => onOpenNote(n.slug)}
              className="flex items-start gap-2 p-2.5 rounded border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50/40 text-left"
            >
              <FileText size={14} className="text-indigo-600 shrink-0 mt-0.5"/>
              <div className="min-w-0">
                <div className="text-[12.5px] font-medium text-gray-900 truncate">{n.title}</div>
                <div className="text-[10px] text-gray-500 truncate">{n.file} · edited {n.modified_at}</div>
              </div>
              <ChevronRight size={14} className="ml-auto text-gray-400 shrink-0"/>
            </button>
          ))}
        </div>
      </div>

      {/* How this page works */}
      <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-4 text-[12.5px] text-amber-900">
        <div className="font-semibold mb-1 flex items-center gap-2">
          <Terminal size={14}/> How this page stays current
        </div>
        <p className="leading-5">
          Every tab pulls from <code className="px-1 bg-white rounded">/api/v1/dev-guide/index</code>, which:
          inspects the running FastAPI app for routes, parses <code className="px-1 bg-white rounded">app/services/*.py</code> with
          AST for service shapes, scans <code className="px-1 bg-white rounded">frontend/src/pages/*.jsx</code> for the page list,
          reads <code className="px-1 bg-white rounded">INFORMATION_SCHEMA</code> for tables, and runs <code className="px-1 bg-white rounded">git log</code> for recent activity.
          There is no doc-string to keep in sync. Click <strong>Re-introspect</strong> any time you want a fresh read.
        </p>
        <p className="leading-5 mt-2">
          To add a free-form note about a process, drop <code className="px-1 bg-white rounded">{'<slug>.md'}</code> in <code className="px-1 bg-white rounded">backend/app/docs/dev_guide/</code>. It appears in the Processes tab automatically.
        </p>
      </div>
    </div>
  )
}

/* ─────────── Notes tab — 2-pane tree + reader, fullscreen toggle ─── */
function NotesTab({ tree, flatNotes }) {
  // Selected slug — sync with URL hash (#/data-management/upload_data)
  const [selectedSlug, setSelectedSlug] = useState(() => {
    if (typeof window !== 'undefined' && window.location.hash.startsWith('#/')) {
      return decodeURIComponent(window.location.hash.slice(2))
    }
    return null
  })
  const [openFolders, setOpenFolders] = useState({})
  const [treeFilter, setTreeFilter] = useState('')
  const [fullscreen, setFullscreen] = useState(false)

  // Keep URL hash in sync when slug changes
  useEffect(() => {
    if (selectedSlug) {
      const h = `#/${selectedSlug}`
      if (window.location.hash !== h) window.history.replaceState(null, '', h)
    } else if (window.location.hash) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search)
    }
  }, [selectedSlug])

  // Listen for back/forward navigation
  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash
      setSelectedSlug(h.startsWith('#/') ? decodeURIComponent(h.slice(2)) : null)
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // Esc exits fullscreen
  useEffect(() => {
    if (!fullscreen) return
    const onKey = (e) => { if (e.key === 'Escape') setFullscreen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fullscreen])

  const filterText = treeFilter.trim().toLowerCase()

  // When user types in the filter, auto-open matching folders
  const allFolderSlugs = useMemo(() => {
    const slugs = []
    const walk = (nodes) => nodes.forEach(n => {
      if (n.type === 'folder') { slugs.push(n.slug); walk(n.children || []) }
    })
    walk(tree || [])
    return slugs
  }, [tree])

  useEffect(() => {
    if (!filterText) return
    const next = {}
    allFolderSlugs.forEach(s => { next[s] = true })
    setOpenFolders(prev => ({ ...prev, ...next }))
  }, [filterText, allFolderSlugs])

  const handleToggle = useCallback((folderSlug) => {
    setOpenFolders(prev => ({ ...prev, [folderSlug]: !(prev[folderSlug] ?? true) }))
  }, [])

  // Empty state
  if (!tree || tree.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-gray-300 p-8 text-center text-[13px] text-gray-500">
        No notes yet. Add markdown files under <code className="px-1 bg-gray-100 rounded">backend/app/docs/dev_guide/</code>.
      </div>
    )
  }

  const wrapperClass = fullscreen
    ? 'fixed inset-0 z-50 bg-white flex flex-col'
    : 'flex flex-col rounded-lg border border-gray-200 overflow-hidden'

  const wrapperStyle = fullscreen ? {} : { height: 'calc(100vh - 280px)', minHeight: '500px' }

  return (
    <div className={wrapperClass} style={wrapperStyle}>
      {/* Toolbar */}
      <div className="flex items-center justify-between px-3 py-2 border-b bg-gray-50 shrink-0">
        <div className="flex items-center gap-2">
          <BookOpen size={14} className="text-indigo-600" />
          <span className="text-[12.5px] font-semibold text-gray-800">Developer Notes</span>
          <span className="text-[10px] text-gray-400">{flatNotes?.length || 0} pages</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={treeFilter}
              onChange={e => setTreeFilter(e.target.value)}
              placeholder="Filter tree…"
              className="pl-7 pr-2 py-1 border border-gray-300 rounded text-[11.5px] w-44 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          </div>
          <button
            onClick={() => setFullscreen(f => !f)}
            className="p-1.5 rounded hover:bg-gray-200 text-gray-600"
            title={fullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen'}
          >
            {fullscreen ? <Minimize2 size={14}/> : <Maximize2 size={14}/>}
          </button>
        </div>
      </div>

      {/* 2-pane body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left tree */}
        <aside className="w-72 shrink-0 border-r border-gray-200 bg-gray-50/40 overflow-auto py-2 px-1">
          <div className="space-y-0.5">
            {(tree || []).map(node => (
              <TreeNode
                key={node.slug}
                node={node}
                level={0}
                openFolders={openFolders}
                onToggle={handleToggle}
                onPickSlug={setSelectedSlug}
                selectedSlug={selectedSlug}
                filterText={filterText}
              />
            ))}
          </div>
        </aside>

        {/* Right reader */}
        <main className="flex-1 overflow-hidden">
          <NoteContent slug={selectedSlug} onPickSlug={setSelectedSlug} />
        </main>
      </div>
    </div>
  )
}

function RoutesTab({ routes, onOpenFile }) {
  return (
    <div className="rounded-lg border border-gray-200 overflow-hidden">
      <div className="overflow-x-auto">
        <table className="min-w-full text-[12px]">
          <thead className="bg-gray-50 text-gray-700 sticky top-0">
            <tr>
              <th className="px-3 py-2 text-left font-semibold">Method</th>
              <th className="px-3 py-2 text-left font-semibold">Path</th>
              <th className="px-3 py-2 text-left font-semibold">Tags</th>
              <th className="px-3 py-2 text-left font-semibold">Summary</th>
              <th className="px-3 py-2 text-left font-semibold">Source</th>
            </tr>
          </thead>
          <tbody>
            {routes.map((r, i) => (
              <tr key={`${r.method}-${r.path}`} className={clsx(i % 2 ? 'bg-gray-50/40' : 'bg-white', 'hover:bg-indigo-50/40')}>
                <td className="px-3 py-1.5 align-top">
                  <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${HTTP_COLORS[r.method] || 'bg-gray-100 text-gray-700'}`}>{r.method}</span>
                </td>
                <td className="px-3 py-1.5 align-top">
                  <code className="font-mono text-[11.5px] text-gray-800">{r.path}</code>
                </td>
                <td className="px-3 py-1.5 align-top text-[11px] text-gray-600">
                  {(r.tags || []).join(', ')}
                </td>
                <td className="px-3 py-1.5 align-top text-gray-700 max-w-md">
                  {r.summary || <span className="text-gray-400 italic">—</span>}
                </td>
                <td className="px-3 py-1.5 align-top">
                  {r.file ? (
                    <button onClick={() => onOpenFile({ path: r.file, line: r.line })} className="font-mono text-[11px] text-indigo-600 hover:underline flex items-center gap-1">
                      {r.file.split('/').slice(-2).join('/')}:{r.line}
                      <ExternalLink size={10}/>
                    </button>
                  ) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!routes.length && <div className="p-6 text-center text-gray-500 text-[13px]">No routes match.</div>}
    </div>
  )
}

function ServicesTab({ services, onOpenFile }) {
  return (
    <div className="space-y-3">
      {services.map(s => (
        <div key={s.name} className="rounded-lg border border-gray-200 bg-white">
          <div className="px-4 py-2.5 border-b bg-gray-50 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Layers size={14} className="text-emerald-600"/>
              <span className="font-mono text-[13px] font-semibold text-gray-900">{s.name}</span>
            </div>
            <button onClick={() => onOpenFile({ path: s.file, line: 1 })} className="text-[11px] text-indigo-600 hover:underline flex items-center gap-1">
              {s.file.split('/').slice(-2).join('/')} <ExternalLink size={10}/>
            </button>
          </div>
          {s.module_doc && <p className="px-4 pt-2 text-[12px] text-gray-700 italic">{s.module_doc}</p>}
          {s.classes?.length > 0 && (
            <div className="px-4 py-2.5">
              <div className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold mb-1">Classes</div>
              <div className="space-y-1.5">
                {s.classes.map(c => (
                  <button
                    key={c.name}
                    onClick={() => onOpenFile({ path: s.file, line: c.line })}
                    className="block w-full text-left px-2 py-1.5 rounded border border-gray-100 hover:border-indigo-300 hover:bg-indigo-50/40"
                  >
                    <div className="flex items-center justify-between">
                      <code className="text-[12px] font-mono text-emerald-700 font-semibold">{c.name}</code>
                      <span className="text-[10px] text-gray-400">line {c.line} · {c.methods.length} methods</span>
                    </div>
                    {c.doc && <p className="text-[11px] text-gray-600 mt-1">{c.doc}</p>}
                    {c.methods?.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {c.methods.map(m => (
                          <span key={m} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 font-mono">{m}</span>
                        ))}
                      </div>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
          {s.functions?.length > 0 && (
            <div className="px-4 py-2.5 border-t border-gray-100">
              <div className="text-[10px] uppercase tracking-wide text-gray-500 font-semibold mb-1">Top-level functions</div>
              <div className="flex flex-wrap gap-1">
                {s.functions.map(f => (
                  <button
                    key={f.name}
                    onClick={() => onOpenFile({ path: s.file, line: f.line })}
                    className="text-[10.5px] px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 font-mono hover:bg-emerald-100"
                    title={f.doc}
                  >
                    {f.name}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      ))}
      {!services.length && <div className="p-6 text-center text-gray-500 text-[13px]">No services match.</div>}
    </div>
  )
}

function PagesTab({ pages, onOpenFile }) {
  return (
    <div className="rounded-lg border border-gray-200 overflow-hidden">
      <table className="min-w-full text-[12px]">
        <thead className="bg-gray-50 text-gray-700">
          <tr>
            <th className="px-3 py-2 text-left font-semibold">Component</th>
            <th className="px-3 py-2 text-left font-semibold">Route</th>
            <th className="px-3 py-2 text-left font-semibold">Hint</th>
            <th className="px-3 py-2 text-left font-semibold">File</th>
          </tr>
        </thead>
        <tbody>
          {pages.map((p, i) => (
            <tr key={p.component} className={clsx(i % 2 ? 'bg-gray-50/40' : 'bg-white', 'hover:bg-indigo-50/40')}>
              <td className="px-3 py-1.5"><code className="text-[11.5px] font-mono text-amber-700">{p.component}</code></td>
              <td className="px-3 py-1.5">{p.route ? <code className="text-[11.5px] font-mono">{p.route}</code> : <span className="text-gray-400">—</span>}</td>
              <td className="px-3 py-1.5 text-gray-700">{p.hint || <span className="text-gray-400 italic">—</span>}</td>
              <td className="px-3 py-1.5">
                <button onClick={() => onOpenFile({ path: p.file, line: 1 })} className="text-[11px] text-indigo-600 hover:underline flex items-center gap-1">
                  {p.file.split('/').slice(-2).join('/')} <ExternalLink size={10}/>
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!pages.length && <div className="p-6 text-center text-gray-500 text-[13px]">No pages match.</div>}
    </div>
  )
}

function TablesTab({ tables }) {
  return (
    <div className="rounded-lg border border-gray-200 overflow-hidden">
      <table className="min-w-full text-[12px]">
        <thead className="bg-gray-50 text-gray-700">
          <tr>
            <th className="px-3 py-2 text-left font-semibold">Table</th>
            <th className="px-3 py-2 text-right font-semibold">Rows (approx)</th>
            <th className="px-3 py-2 text-left font-semibold">Created</th>
            <th className="px-3 py-2 text-left font-semibold">Last modified</th>
          </tr>
        </thead>
        <tbody>
          {tables.map((t, i) => (
            <tr key={t.table} className={clsx(i % 2 ? 'bg-gray-50/40' : 'bg-white')}>
              <td className="px-3 py-1.5"><code className="text-[11.5px] font-mono text-purple-700">{t.table}</code></td>
              <td className="px-3 py-1.5 text-right text-gray-800">{t.rows.toLocaleString()}</td>
              <td className="px-3 py-1.5 text-gray-600 text-[11px]">{t.created_at?.slice(0, 19) || '—'}</td>
              <td className="px-3 py-1.5 text-gray-600 text-[11px]">{t.modified_at?.slice(0, 19) || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!tables.length && <div className="p-6 text-center text-gray-500 text-[13px]">No tables match.</div>}
    </div>
  )
}

function RecentTab({ recent, onOpenFile }) {
  return (
    <div className="space-y-2">
      {recent.map(c => (
        <div key={c.sha} className="rounded-lg border border-gray-200 bg-white p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-gray-900">{c.subject}</div>
              <div className="text-[11px] text-gray-500 mt-0.5">{c.author} · {c.date?.slice(0, 19)}</div>
            </div>
            <code className="text-[10px] font-mono text-gray-500 shrink-0 mt-1">{c.sha}</code>
          </div>
          {c.files?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {c.files.slice(0, 12).map(f => (
                <button
                  key={f}
                  onClick={() => onOpenFile({ path: f, line: 0 })}
                  className="text-[10.5px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 font-mono hover:bg-indigo-100 hover:text-indigo-700"
                >
                  {f.split('/').slice(-1)[0]}
                </button>
              ))}
              {c.files.length > 12 && <span className="text-[10px] text-gray-500">+{c.files.length - 12} more</span>}
            </div>
          )}
        </div>
      ))}
      {!recent.length && <div className="p-6 text-center text-gray-500 text-[13px]">No recent commits match.</div>}
    </div>
  )
}
