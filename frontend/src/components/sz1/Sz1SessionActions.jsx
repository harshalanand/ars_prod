import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  Check, CheckCircle2, Cpu, ParkingSquare, Send, FileText, Truck, PackageCheck, Trash2, XCircle, ShieldAlert, RefreshCw,
  Store, Info, Search, Loader2,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API } from '@/services/api'
import { confirmDialog } from '@/components/ui/ConfirmDialog'
import { C } from '@/theme/colors'
import { Banner, Kpi, DataTable, Modal, Btn, Chip, Tip, n, when, pct } from './Sz1Ui'
import { SubHead, AgeChip, statusOf, actErr } from './Sz1SessionBits'

// 1SZ ALC — the parked → approved lifecycle pieces of the Sessions page:
// Pend ALC progress, the session timeline, the Approve popup (re-check first,
// Re-fit when stock moved) and the Remove-stores popup.
// Spec: public/docs/manual/sz1_alloc.md §F and §J (P3).

const pctNum = (a, b) => (b ? Math.max(0, Math.min(100, (a / b) * 100)) : 0)

/** Pend ALC progress of an approved session: {rows, alloc_qty, bdc_qty, do_qty, pend_qty, closed_rows, open_rows}. */
export function pendState(p) {
  if (!p) return null
  const rows = +p.rows || 0
  const closed = +p.closed_rows || 0
  return { rows, closed, bdc: pctNum(+p.bdc_qty || 0, +p.alloc_qty || 0), dlv: pctNum(+p.do_qty || 0, +p.alloc_qty || 0),
           rowPct: pctNum(closed, rows), shipped: rows > 0 && closed >= rows }
}

function Bar({ label, value, color, tip, width }) {
  return (
    <Tip tip={tip}>
      <span style={{ display: 'inline-flex', flexDirection: 'column', gap: 3, minWidth: 0, width, cursor: 'help' }}>
        <span style={{ display: 'flex', justifyContent: 'space-between', gap: 6, fontSize: 10.5, color: C.textSub, fontWeight: 600 }}>
          <span style={{ whiteSpace: 'nowrap' }}>{label}</span>
          <span style={{ fontVariantNumeric: 'tabular-nums', color: C.text, fontWeight: 700 }}>{Math.round(value)}%</span>
        </span>
        <span style={{ height: 6, borderRadius: 999, background: '#eef2f7', overflow: 'hidden' }}>
          <span style={{ display: 'block', width: `${value}%`, height: '100%', borderRadius: 999, background: color, transition: 'width .4s' }} />
        </span>
      </span>
    </Tip>
  )
}

/** Three small bars — BDC sent, DO received, rows closed — and a Shipped chip when every row is closed. */
export function PendProgress({ p, width = 92 }) {
  const st = pendState(p)
  if (!st) return <span style={{ fontSize: 11, color: C.textMuted }}>progress not loaded</span>
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', minWidth: 0 }}>
      <Bar label="BDC sent" value={st.bdc} color={C.primary} width={width}
        tip={`${n(p.bdc_qty)} of ${n(p.alloc_qty)} units have a BDC sent to SAP.`} />
      <Bar label="DO received" value={st.dlv} color={C.blue} width={width}
        tip={`${n(p.do_qty)} of ${n(p.alloc_qty)} units have a delivery order back from SAP.`} />
      <Bar label={`Closed ${n(st.closed)}/${n(st.rows)}`} value={st.rowPct} color={C.green} width={width + 20}
        tip={`${n(st.closed)} of ${n(st.rows)} Pend ALC rows are closed; ${n(p.pend_qty)} units still pending.`} />
      {st.shipped && <Tip tip="Every Pend ALC row of this session is closed."><Chip tone="green">Shipped</Chip></Tip>}
    </span>
  )
}

// ── timeline ───────────────────────────────────────────────────────────────
function TNode({ icon: Icon, title, sub, state, tip }) {
  const col = state === 'done' ? C.green : state === 'current' ? C.primary : state === 'bad' ? C.red : state === 'off' ? C.gray : '#cbd5e1'
  const filled = state === 'done' || state === 'current' || state === 'bad' || state === 'off'
  return (
    <Tip tip={tip}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, minWidth: 0, cursor: tip ? 'help' : 'default' }}>
        <span style={{ width: 28, height: 28, borderRadius: '50%', display: 'grid', placeItems: 'center', flex: '0 0 28px',
                       border: `2px solid ${col}`, background: filled ? col : '#fff', color: filled ? '#fff' : col }}>
          {state === 'done' ? <Check size={14} strokeWidth={3} /> : <Icon size={13} />}
        </span>
        <span style={{ minWidth: 0, display: 'grid' }}>
          <span style={{ fontSize: 12.5, fontWeight: 700, color: state === 'todo' ? C.textMuted : C.text, whiteSpace: 'nowrap' }}>{title}</span>
          {sub && <span style={{ fontSize: 11, color: C.textMuted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 190 }}>{sub}</span>}
        </span>
      </span>
    </Tip>
  )
}

/** Run → Parked → Approved → BDC → DO → Closed (or → Discarded / Failed). */
export function SessionTimeline({ s, pend }) {
  const st = statusOf(s)
  const p = pendState(pend)
  const nodes = [{ icon: Cpu, title: 'Run', sub: `${when(s.SESSION_AT)} · ${s.CREATED_BY || '—'}`, state: st === 'RUNNING' ? 'current' : st === 'FAILED' || st === 'CANCELLED' ? 'bad' : 'done',
                   tip: 'The allocation run that made this session.' }]
  if (st === 'FAILED' || st === 'CANCELLED') {
    nodes.push({ icon: XCircle, title: st === 'FAILED' ? 'Failed' : 'Cancelled', sub: 'no lines written', state: 'bad',
                 tip: 'Nothing was parked: the run wrote no lines and holds no stock.' })
  } else {
    nodes.push({ icon: ParkingSquare, title: 'Parked', sub: st === 'PARKED' ? <AgeChip s={s} /> : st === 'RUNNING' ? 'when the run commits' : 'held stock in 1SZ',
                 state: st === 'RUNNING' ? 'todo' : st === 'PARKED' ? 'current' : 'done',
                 tip: 'A parked session holds its stock and size budget inside 1SZ: every later run sees it as taken.' })
    if (st === 'DISCARDED') {
      nodes.push({ icon: Trash2, title: 'Discarded', sub: `${when(s.CLOSED_AT)} · ${s.CLOSED_BY || '—'}`, state: 'off',
                   tip: 'Dropped. Its stock and size budget went back to the pool.' })
    } else {
      const ap = st === 'APPROVED'
      nodes.push({ icon: Send, title: 'Approved', sub: ap ? `${when(s.APPROVED_AT)} · ${s.APPROVED_BY || '—'}` : 'into Pend ALC', state: ap ? 'done' : 'todo',
                   tip: ap ? `Written to ARS_PEND_ALC as session ${s.PEND_SESSION_ID || `1SZ-${s.SESSION_ID}`}.` : 'Approve writes the lines to ARS_PEND_ALC; MSA free stock drops at once.' })
      nodes.push({ icon: FileText, title: 'BDC sent', sub: p ? `${Math.round(p.bdc)}% of units` : '—', state: !ap || !p ? 'todo' : p.bdc >= 100 ? 'done' : p.bdc > 0 ? 'current' : 'todo',
                   tip: 'Units with a BDC sent to SAP (managed in Pend ALC).' })
      nodes.push({ icon: Truck, title: 'DO received', sub: p ? `${Math.round(p.dlv)}% of units` : '—', state: !ap || !p ? 'todo' : p.dlv >= 100 ? 'done' : p.dlv > 0 ? 'current' : 'todo',
                   tip: 'Units with a delivery order back from SAP.' })
      nodes.push({ icon: PackageCheck, title: p?.shipped ? 'Shipped' : 'Closed', sub: p ? `${n(p.closed)} / ${n(p.rows)} rows` : '—',
                   state: !ap || !p ? 'todo' : p.shipped ? 'done' : p.closed > 0 ? 'current' : 'todo', tip: 'Pend ALC rows closed out of all rows of this session.' })
    }
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', rowGap: 10, padding: '10px 12px', borderRadius: 12,
                  background: '#f8fafc', border: `1px solid ${C.cardBorder}` }}>
      {nodes.map((x, i) => (
        <Fragment key={x.title}>
          <TNode {...x} />
          {i < nodes.length - 1 && <span style={{ flex: '1 1 14px', minWidth: 14, maxWidth: 60, height: 2, margin: '0 8px', borderRadius: 2,
                                                 background: x.state === 'done' ? C.green : '#e2e8f0' }} />}
        </Fragment>
      ))}
    </div>
  )
}

// ── approve ────────────────────────────────────────────────────────────────
const STOCK_COLS = [['SRC_RDC', 'Ship-from RDC'], ['ARTICLE_NUMBER', 'Article'], ['NEED', 'Session needs'], ['FREE', 'Free now']]
const BUDGET_COLS = [['ST_CD', 'Store'], ['MAJ_CAT', 'MAJ_CAT'], ['SZ', 'Size'], ['NEED', 'Session gives'], ['ROOM', 'Room now']]

/**
 * Approve session → Pend ALC. Opens with a live re-check (read only); approve
 * only when nothing moved, else the shortfalls and a Re-fit button.
 */
export function ApproveModal({ s, open, onClose, onApproved, onRefit }) {
  const [chk, setChk] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState('')
  const sid = s?.SESSION_ID

  const recheck = async () => {
    setChk(null); setErr(null); setBusy('check')
    try { const { data: r } = await sz1API.recheck(sid); setChk(r.data || r) }
    catch (e) { setErr(actErr(e, 'Could not re-check the session')) }
    finally { setBusy('') }
  }
  useEffect(() => { if (open && sid) recheck() }, [open, sid])   // eslint-disable-line react-hooks/exhaustive-deps

  const approve = async () => {
    setBusy('approve')
    try {
      const { data: r } = await sz1API.approve(sid)
      const d = r.data || r
      toast.success(`Session ${sid} approved: ${n(d.rows)} rows · ${n(d.units)} units in Pend ALC as ${d.pend_session_id}`, { duration: 6000 })
      onApproved?.(d)
    } catch (e) {
      toast.error(actErr(e, 'Could not approve'))
      if (e?.response?.status === 409) recheck()            // something moved in between: show what
    } finally { setBusy('') }
  }

  const t = chk?.totals || {}
  const blocked = chk && !chk.ok
  const footer = (
    <>
      <Btn icon={RefreshCw} onClick={recheck} busy={busy === 'check'} disabled={!!busy} tip="Check live stock and size budgets again. Read only.">Check again</Btn>
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <Btn onClick={onClose} disabled={busy === 'approve'} tip="Close without changing anything">Cancel</Btn>
        {blocked && (
          <Btn kind="primary" icon={RefreshCw} onClick={onRefit} disabled={!!busy}
            tip="Run the same stores and options again on the stock that is left. This session is discarded and the new one parks.">Re-fit</Btn>
        )}
        {!blocked && (
          <Btn kind="primary" icon={Send} onClick={approve} busy={busy === 'approve'} disabled={!chk || !!busy}
            tip={chk ? 'Write the lines to ARS_PEND_ALC now. Needs ACT_1SZ_APPROVE.' : 'Wait for the re-check'}>Approve</Btn>
        )}
      </span>
    </>
  )
  return (
    <Modal open={open} onClose={() => busy !== 'approve' && onClose()} title={`Approve session ${sid} into Pend ALC`} icon={Send} width={900} footer={footer}>
      {busy === 'check' && !chk && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: C.textSub, padding: '18px 4px' }}>
          <Loader2 size={15} className="animate-spin" /> Checking live stock and size budgets…
        </div>
      )}
      {err && <Banner tone="red" label="Re-check failed" text={err} />}
      {chk && (
        <div style={{ display: 'grid', gap: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 }}>
            <Kpi k="Lines" v={n(t.lines)} tip="Session lines that got stock." icon={FileText} tone="indigo" />
            <Kpi k="Units" v={n(t.units)} tip="Units the session allocated." icon={Truck} tone="green" />
            <Kpi k="Stores" v={n(t.stores)} icon={Store} tone="blue" />
            <Kpi k="Pend ALC rows" v={n(t.pend_rows)} tip="Rows written to ARS_PEND_ALC: one per store × article × ship-from RDC." icon={PackageCheck} />
          </div>
          {blocked ? (
            <>
              <Banner tone="red" label="Blocked" text={(chk.block || []).join(' · ') || 'Live stock or a size budget no longer covers this session.'} />
              <div style={{ fontSize: 12.5, color: C.textSub, lineHeight: 1.55 }}>
                Stock moved since this run — for example a listing was approved, or another session took it. Approving now would oversell.
                <b style={{ color: C.text }}> Re-fit</b> runs the same stores and options again on what is left; this session is discarded and the new one parks.
              </div>
              {(chk.short_stock || []).length > 0 && (
                <div><SubHead right={<span style={{ color: C.textMuted }}>the session needs more than is free now</span>}>Short stock ({n(chk.short_stock.length)})</SubHead>
                  <DataTable rows={chk.short_stock} cols={STOCK_COLS} maxHeight={200} exportName={`1SZ_ALC_${sid}_short_stock.csv`} /></div>
              )}
              {(chk.short_budget || []).length > 0 && (
                <div><SubHead right={<span style={{ color: C.textMuted }}>the store’s size budget has less room now</span>}>Short size budget ({n(chk.short_budget.length)})</SubHead>
                  <DataTable rows={chk.short_budget} cols={BUDGET_COLS} maxHeight={200} exportName={`1SZ_ALC_${sid}_short_budget.csv`} /></div>
              )}
            </>
          ) : (
            <div style={{ display: 'flex', gap: 10, padding: '10px 12px', borderRadius: 10, background: C.greenBg, border: `1px solid ${C.greenBd}`,
                          fontSize: 12.5, color: C.text, lineHeight: 1.6 }}>
              <CheckCircle2 size={16} color={C.green} style={{ flex: '0 0 16px', marginTop: 2 }} />
              <div>
                <b>Live stock and size budgets still cover this session.</b> Approve:
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  <li>writes {n(t.pend_rows)} rows to ARS_PEND_ALC as session <b>1SZ-{sid}</b>, SOURCE <b>1SZ</b>, RDC = ship-from RDC;</li>
                  <li>drops MSA free stock and updates the grid at once;</li>
                  <li>hands the BDC, DO and closing to the Pend ALC module;</li>
                  <li>can be reverted until a BDC is sent or a DO comes back.</li>
                </ul>
              </div>
            </div>
          )}
          {(chk.warn || []).length > 0 && (
            <div style={{ display: 'grid', gap: 6 }}>
              {chk.warn.map((w, i) => <Banner key={i} tone="amber" label="Warning" text={w} />)}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}

// ── remove stores ──────────────────────────────────────────────────────────
/** Pick stores to drop from a parked session; their units go back to the pool. */
export function RemoveStoresModal({ s, open, onClose, onDone }) {
  const rows = useMemo(() => (s?.summary?.per_store || []).filter(r => +r.ALLOC_QTY > 0), [s])
  const [pick, setPick] = useState(() => new Set())
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (open) { setPick(new Set()); setQ('') } }, [open])
  const shown = q ? rows.filter(r => `${r.ST_CD} ${r.ST_NM || ''} ${r.RDC || ''}`.toUpperCase().includes(q.toUpperCase())) : rows
  const units = rows.filter(r => pick.has(r.ST_CD)).reduce((a, r) => a + (+r.ALLOC_QTY || 0), 0)
  const all = shown.length > 0 && shown.every(r => pick.has(r.ST_CD))
  const flip = code => setPick(p => { const x = new Set(p); x.has(code) ? x.delete(code) : x.add(code); return x })
  const flipAll = () => setPick(p => { const x = new Set(p); shown.forEach(r => (all ? x.delete(r.ST_CD) : x.add(r.ST_CD))); return x })

  const go = async () => {
    const stores = [...pick]
    if (stores.length >= rows.length) {
      toast.error('That is every store with stock. Discard the session instead.')
      return
    }
    const ok = await confirmDialog({            // the app-wide confirm stacks above this popup
      tone: 'warn', title: `Remove ${stores.length} store${stores.length > 1 ? 's' : ''} from session ${s.SESSION_ID}?`, confirmLabel: 'Remove stores',
      body: `${n(units)} units go back to the pool for other runs.`,
      bullets: [stores.slice(0, 12).join(', ') + (stores.length > 12 ? ` … +${stores.length - 12}` : ''),
                'Their lines leave the session; the other stores keep theirs.', 'This cannot be undone — re-run those stores to allocate them again.'],
    })
    if (!ok) return
    setBusy(true)
    try {
      const { data: r } = await sz1API.removeStores(s.SESSION_ID, stores)
      toast.success(r.message || `Removed ${stores.length} store(s) from session ${s.SESSION_ID}`)
      onDone?.()
    } catch (e) { toast.error(actErr(e, 'Could not remove the stores')) }
    finally { setBusy(false) }
  }

  const cell = { padding: '6px 8px', fontSize: 12, borderTop: '1px solid #f1f5f9', whiteSpace: 'nowrap' }
  const num = { ...cell, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }
  return (
    <Modal open={open} onClose={() => !busy && onClose()} title={`Remove stores from session ${s?.SESSION_ID}`} icon={Store} width={760}
      footer={(
        <>
          <span style={{ fontSize: 12, color: C.textSub }}>{n(pick.size)} picked · {n(units)} units back to the pool</span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <Btn onClick={onClose} disabled={busy} tip="Close without changing anything">Cancel</Btn>
            <Btn kind="primary" icon={Trash2} onClick={go} busy={busy} disabled={!pick.size || busy}
              tip={pick.size ? 'Drop the picked stores’ lines from this parked session' : 'Tick at least one store'}>Remove {pick.size || ''} store{pick.size === 1 ? '' : 's'}</Btn>
          </span>
        </>
      )}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <Tip tip="Filter by store code, name or RDC">
          <span style={{ position: 'relative', display: 'inline-flex' }}>
            <Search size={13} color={C.textMuted} style={{ position: 'absolute', left: 9, top: 9 }} />
            <input className="input" value={q} onChange={e => setQ(e.target.value)} placeholder="Find a store" style={{ width: 200, paddingLeft: 28 }} />
          </span>
        </Tip>
        <Chip tone="indigo">{n(rows.length)} stores hold stock</Chip>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: C.textMuted }}>
          <Info size={13} /> Stores that got nothing are not listed.
        </span>
      </div>
      {!rows.length ? <div style={{ fontSize: 12.5, color: C.textMuted, padding: 12 }}>No store in this session holds stock.</div> : (
        <div style={{ maxHeight: 380, overflow: 'auto', border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead style={{ position: 'sticky', top: 0, background: '#f8fafc', zIndex: 1 }}>
              <tr style={{ fontSize: 11, color: C.textSub, textAlign: 'left' }}>
                <th style={{ ...cell, width: 34 }}>
                  <Tip tip={all ? 'Untick every shown store' : 'Tick every shown store'}>
                    <input type="checkbox" checked={all} onChange={flipAll} aria-label="Tick every shown store" />
                  </Tip>
                </th>
                <th style={cell}>Store</th><th style={cell}>Name</th><th style={cell}>RDC</th>
                <th style={{ ...num }}>Lines served</th><th style={{ ...num }}>Units</th><th style={{ ...num }}>Fill %</th>
              </tr>
            </thead>
            <tbody>
              {shown.map(r => {
                const on = pick.has(r.ST_CD)
                return (
                  <tr key={r.ST_CD} onClick={() => flip(r.ST_CD)} style={{ cursor: 'pointer', background: on ? C.redBg : undefined }}>
                    <td style={cell}><input type="checkbox" checked={on} onChange={() => flip(r.ST_CD)} onClick={e => e.stopPropagation()} aria-label={`Remove ${r.ST_CD}`} /></td>
                    <td style={{ ...cell, fontWeight: 700 }}>{r.ST_CD}</td>
                    <td style={{ ...cell, maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.ST_NM || '—'}</td>
                    <td style={cell}>{r.RDC || '—'}</td>
                    <td style={num}>{n(r.SERVED_LINES ?? r.LINES)}</td>
                    <td style={{ ...num, fontWeight: 700 }}>{n(r.ALLOC_QTY)}</td>
                    <td style={num}>{r.FILL_PCT != null ? `${(+r.FILL_PCT).toFixed(1)}%` : pct(r.ALLOC_QTY, r.SZ_SRT)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: C.textMuted }}>
        <ShieldAlert size={13} /> Only a parked session can lose stores. The size budget and stock of the removed stores free up at once.
      </div>
    </Modal>
  )
}
