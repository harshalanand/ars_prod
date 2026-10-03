/**
 * ConfirmDialog — the app-wide replacement for window.confirm / window.prompt.
 *
 * Native dialogs were used in 35 pages. They render as "localhost:3000 says",
 * cannot show structured consequence text, block the JS thread, and Chrome's
 * "prevent this page from creating more dialogs" checkbox silently makes every
 * later confirm() return false — which on a KILL-session or reset button means
 * the click just does nothing forever.
 *
 * Usage — one line per call site, no local state:
 *
 *   if (!await confirmDialog({ tone: 'danger', title: 'Delete rule?',
 *                              body: 'This cannot be undone.',
 *                              confirmLabel: 'Delete' })) return
 *
 *   const note = await promptDialog({ title: 'Reject session?',
 *                                     input: { label: 'Note', optional: true } })
 *   if (note === null) return          // cancelled (empty string is a real value)
 *
 * <ConfirmHost/> is mounted once in main.jsx next to <Toaster/>. If it is not
 * mounted (or crashed inside its boundary) both helpers fall back to the native
 * dialog, so a call site never hangs on an unresolved promise.
 */
import { useEffect, useRef, useState } from 'react'
import {
  X, AlertTriangle, AlertOctagon, Info, ShieldCheck, Clock,
} from 'lucide-react'

const TONES = {
  info: {
    Icon: Info,
    badge: 'bg-primary-50 text-primary-600',
    btn: 'btn bg-gradient-to-r from-primary-600 to-primary-500 text-white hover:from-primary-700 hover:to-primary-600 shadow-sm',
    sub: 'text-gray-500',
  },
  warn: {
    Icon: AlertTriangle,
    badge: 'bg-amber-50 text-amber-600',
    btn: 'btn bg-gradient-to-r from-amber-600 to-amber-500 text-white hover:from-amber-700 hover:to-amber-600 shadow-sm',
    sub: 'text-amber-600',
  },
  danger: {
    Icon: AlertOctagon,
    badge: 'bg-red-50 text-red-600',
    btn: 'btn bg-gradient-to-r from-red-600 to-red-500 text-white hover:from-red-700 hover:to-red-600 shadow-sm',
    sub: 'text-red-600',
  },
}

/* The host publishes its setter here. Null => no host mounted. */
let publish = null
const pending = []

function request(req) {
  if (!publish) {
    // Degraded fallback — keeps the call site working with no host.
    if (req.input) {
      const v = window.prompt(req.title || '', req.input.defaultValue ?? '')
      return Promise.resolve(v)
    }
    const parts = [req.title, req.body, ...(req.bullets || []).map(b => `• ${b}`)]
    return Promise.resolve(window.confirm(parts.filter(Boolean).join('\n\n')))
  }
  return new Promise((resolve) => {
    const item = { ...req, resolve }
    pending.push(item)
    if (pending.length === 1) publish(item)
  })
}

/** Yes/no gate. Resolves true when confirmed, false on cancel/Esc/overlay. */
export function confirmDialog(opts = {}) {
  const { input, ...rest } = opts
  return request(rest)
}

/**
 * window.prompt replacement. Resolves the entered string, or null when
 * cancelled — so '' stays distinguishable from "user backed out".
 */
export function promptDialog(opts = {}) {
  return request({ ...opts, input: opts.input || {} })
}

export function ConfirmHost() {
  const [req, setReq] = useState(null)
  const [text, setText] = useState('')
  const [gate, setGate] = useState('')
  const [err, setErr] = useState('')
  const confirmRef = useRef(null)
  const cancelRef = useRef(null)
  const firstRef = useRef(null)

  useEffect(() => {
    publish = setReq
    return () => { publish = null }
  }, [])

  /* Reset per-dialog state whenever a new request opens. */
  useEffect(() => {
    if (!req) return
    setText(req.input?.defaultValue != null ? String(req.input.defaultValue) : '')
    setGate('')
    setErr('')
    const t = setTimeout(() => {
      const el = req.input ? firstRef.current
        : (req.tone === 'danger' ? cancelRef.current : confirmRef.current)
      el?.focus()
      if (req.input && firstRef.current?.select) firstRef.current.select()
    }, 20)
    return () => clearTimeout(t)
  }, [req])

  const close = (result) => {
    const item = pending.shift()
    item?.resolve(result)
    setReq(null)
    // Give the exit a tick, then open whatever queued up behind it.
    if (pending.length) setTimeout(() => publish?.(pending[0]), 0)
  }

  if (!req) return null

  const tone = TONES[req.tone] || TONES.info
  const Icon = req.icon || tone.Icon
  const spec = req.input
  const needsGate = !!req.requireText
  const gateOk = !needsGate || gate.trim() === req.requireText

  const validate = () => {
    if (!spec) return true
    const v = text.trim()
    if (!v && !spec.optional) { setErr(`${spec.label || 'Value'} is required`); return false }
    if (spec.type === 'number' && v) {
      const n = Number(v)
      if (!Number.isFinite(n)) { setErr('Enter a number'); return false }
      if (spec.min != null && n < spec.min) { setErr(`Minimum is ${spec.min}`); return false }
      if (spec.max != null && n > spec.max) { setErr(`Maximum is ${spec.max}`); return false }
    }
    setErr('')
    return true
  }

  const onConfirm = () => {
    if (!gateOk) return
    if (!validate()) return
    close(spec ? text.trim() : true)
  }
  const onCancel = () => close(spec ? null : false)

  const onKeyDown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); onCancel(); return }
    if (e.key === 'Enter' && !e.shiftKey) {
      if (e.target.tagName === 'TEXTAREA') return   // newline in a note field
      e.preventDefault()
      onConfirm()
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/40 p-4 animate-fade-in"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel() }}
      onKeyDown={onKeyDown}
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
    >
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-md max-h-[85vh] overflow-y-auto"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* head */}
        <div className="flex items-start gap-2.5 px-4 pt-3.5 pb-2">
          <div className={`shrink-0 w-7 h-7 rounded-lg flex items-center justify-center ${tone.badge}`}>
            <Icon size={15} />
          </div>
          <div className="flex-1 min-w-0">
            <h2 id="confirm-title" className="text-[12.5px] font-semibold text-gray-900 leading-snug">
              {req.title || 'Are you sure?'}
            </h2>
            {req.subtitle && (
              <p className={`text-[10px] mt-0.5 ${tone.sub} ${req.mono ? 'font-mono' : ''}`}>
                {req.subtitle}
              </p>
            )}
          </div>
          <button
            onClick={onCancel}
            aria-label="Close"
            className="shrink-0 p-1 -mt-1 -mr-1 text-gray-400 hover:text-gray-700 hover:bg-gray-100 rounded-md"
          >
            <X size={14} />
          </button>
        </div>

        {/* body — indented to line up under the title */}
        <div className="px-4 pb-3 pl-[46px] space-y-2">
          {req.body && (
            typeof req.body === 'string'
              ? <p className="text-[11px] text-gray-600 leading-relaxed whitespace-pre-line">{req.body}</p>
              : req.body
          )}

          {!!req.bullets?.length && (
            <ul className="space-y-1 text-[11px] text-gray-600">
              {req.bullets.map((b, i) => (
                <li key={i} className="flex gap-1.5 leading-relaxed">
                  <span className="text-gray-400 mt-px">•</span>
                  <span className="min-w-0">{b}</span>
                </li>
              ))}
            </ul>
          )}

          {req.safe && (
            <div className="flex gap-1.5 text-[10.5px] text-emerald-800 bg-emerald-50 rounded-md px-2 py-1.5 leading-relaxed">
              <ShieldCheck size={13} className="shrink-0 mt-px text-emerald-600" />
              <span>{req.safe}</span>
            </div>
          )}

          {spec && (
            <div className="pt-0.5">
              {spec.label && (
                <label className="label">
                  {spec.label}
                  {spec.optional && <span className="ml-1 normal-case font-normal tracking-normal text-gray-400">(optional)</span>}
                </label>
              )}
              {spec.multiline ? (
                <textarea
                  ref={firstRef}
                  rows={spec.rows || 2}
                  value={text}
                  onChange={(e) => { setText(e.target.value); setErr('') }}
                  placeholder={spec.placeholder || ''}
                  className={`input resize-y ${err ? 'input-error' : ''}`}
                />
              ) : (
                <div className="flex items-center gap-1.5">
                  <input
                    ref={firstRef}
                    type={spec.type === 'number' ? 'number' : 'text'}
                    value={text}
                    onChange={(e) => { setText(e.target.value); setErr('') }}
                    placeholder={spec.placeholder || ''}
                    min={spec.min}
                    max={spec.max}
                    className={`input ${spec.mono ? 'font-mono' : ''} ${err ? 'input-error' : ''}`}
                  />
                  {spec.suffix && <span className="text-[10px] text-gray-500 shrink-0">{spec.suffix}</span>}
                </div>
              )}
              {(err || spec.hint) && (
                <p className={`text-[10px] mt-1 ${err ? 'text-red-600' : 'text-gray-400'}`}>
                  {err || spec.hint}
                </p>
              )}
            </div>
          )}

          {needsGate && (
            <div className="pt-0.5">
              <label className="label normal-case tracking-normal text-[10px] font-medium text-gray-500">
                Type <span className="font-mono font-semibold text-gray-800">{req.requireText}</span> to enable the button
              </label>
              <input
                value={gate}
                onChange={(e) => setGate(e.target.value)}
                placeholder={req.requireText}
                className="input font-mono"
                autoComplete="off"
              />
            </div>
          )}

          {req.footnote && (
            <div className="flex items-center gap-1 text-[10px] text-gray-400 pt-0.5">
              <Clock size={11} />
              <span>{req.footnote}</span>
            </div>
          )}
        </div>

        {/* actions */}
        <div className="flex justify-end gap-2 px-4 py-2.5 border-t border-gray-100 bg-gray-50/70 rounded-b-xl">
          <button ref={cancelRef} onClick={onCancel} className="btn-secondary">
            {req.cancelLabel || 'Cancel'}
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            disabled={!gateOk}
            className={tone.btn}
          >
            {req.confirmLabel || 'Continue'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default ConfirmHost
