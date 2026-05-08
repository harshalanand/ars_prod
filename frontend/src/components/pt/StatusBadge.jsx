// Status / Priority / Phase chips for the Project Tracker.
// Colours align with the BRD §10.3 visual conventions.

const STATUS_COLOURS = {
  DRAFT:       { bg: '#F3F4F6', fg: '#374151' },
  NOT_STARTED: { bg: '#E5E7EB', fg: '#374151' },
  IN_PROGRESS: { bg: '#DBEAFE', fg: '#1D4ED8' },
  BLOCKED:     { bg: '#FEE2E2', fg: '#B91C1C' },
  ON_HOLD:     { bg: '#F3F4F6', fg: '#6B7280' },
  COMPLETED:   { bg: '#DCFCE7', fg: '#15803D' },
  CANCELLED:   { bg: '#F1F5F9', fg: '#64748B' },
}

const PRIORITY_COLOURS = {
  CRITICAL: { bg: '#FEE2E2', fg: '#B91C1C' },
  HIGH:     { bg: '#FFEDD5', fg: '#C2410C' },
  MEDIUM:   { bg: '#FEF9C3', fg: '#A16207' },
  LOW:      { bg: '#DCFCE7', fg: '#15803D' },
}

const PHASE_COLOURS = {
  PHASE_1: { bg: '#EDE9FE', fg: '#6D28D9' },
  PHASE_2: { bg: '#E0F2FE', fg: '#0369A1' },
  PHASE_3: { bg: '#F0FDF4', fg: '#15803D' },
  BACKLOG: { bg: '#F3F4F6', fg: '#4B5563' },
  ICEBOX:  { bg: '#E5E7EB', fg: '#6B7280' },
}

function Chip({ label, palette }) {
  const c = palette[label] || { bg: '#E5E7EB', fg: '#374151' }
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: 999,
      fontSize: 11,
      fontWeight: 600,
      background: c.bg,
      color: c.fg,
      whiteSpace: 'nowrap',
    }}>{label || '—'}</span>
  )
}

export const StatusBadge   = ({ value }) => <Chip label={value} palette={STATUS_COLOURS} />
export const PriorityChip  = ({ value }) => <Chip label={value} palette={PRIORITY_COLOURS} />
export const PhaseChip     = ({ value }) => <Chip label={value} palette={PHASE_COLOURS} />
