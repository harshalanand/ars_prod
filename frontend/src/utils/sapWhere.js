// Shared SAP WHERE-clause helper. A "condition" is { field, op, value, conn }.
// buildWhere() turns a list of conditions into an RFC_READ_TABLE WHERE string
// (mirrors the Python build_where_clause in sap_pull_service.py — keep in sync).

export const SAP_OPERATORS = ['=', '<>', '<', '<=', '>', '>=', 'LIKE', 'NOT LIKE', 'IN', 'NOT IN', 'IS NULL', 'IS NOT NULL']
export const NO_VALUE_OPS = ['IS NULL', 'IS NOT NULL']

const isNumeric = (s) => /^-?\d+(\.\d+)?$/.test(String(s).trim())
const q = (v) => {
  const s = String(v ?? '').trim()
  return isNumeric(s) ? s : `'${s.replace(/'/g, "''")}'`
}

export function buildWhere(conds = []) {
  const parts = []
  conds.forEach((c, i) => {
    const field = (c.field || '').trim()
    const op = (c.op || '=').toUpperCase()
    if (!field) return
    let expr
    if (NO_VALUE_OPS.includes(op)) {
      expr = `${field} ${op}`
    } else if (op === 'IN' || op === 'NOT IN') {
      const items = String(c.value ?? '').split(',').map(x => x.trim()).filter(Boolean)
      if (!items.length) return
      expr = `${field} ${op} (${items.map(q).join(', ')})`
    } else {
      if (c.value === undefined || c.value === null || String(c.value).trim() === '') return
      expr = `${field} ${op} ${q(c.value)}`
    }
    const conn = i > 0 ? `${(c.conn || 'AND').toUpperCase()} ` : ''
    parts.push(conn + expr)
  })
  return parts.join(' ').trim()
}

export const emptyCond = () => ({ field: '', op: '=', value: '', conn: 'AND' })

// Only send conditions that actually name a field.
export const cleanConds = (conds = []) => conds.filter(c => (c.field || '').trim())
