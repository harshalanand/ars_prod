import HelpPage from '@/components/facons/HelpPage'

const SECTIONS = [
  {
    id: 'what', emoji: '📦', title: 'What this module is',
    lead: 'FA & CONS handles two kinds of non-fashion stock: **FA = Fixed Assets** (project-store fittings — hangers, hard-tags, frames, IT/security hardware) and **CONS = Consumables** (carry bags, price rolls, tags). It decides how much of each to send to each store and tracks it end-to-end.',
    bullets: [
      'Everything is driven by an **MBQ** (Minimum-Buy-Quantity) target per store × reference article.',
      'It runs **separately** from the normal fashion allocation — its own stock, its own pending, its own tables.',
    ],
  },
  {
    id: 'why', emoji: '🎯', title: 'BRD — why it exists (in plain words)',
    lead: 'Project stores and consumables were planned in Excel — slow, inconsistent, and with no record of who changed what or why. This module brings it into ARS.',
    rows: [
      ['One place', 'Upload targets, calculate stock, allocate, and track delivery — all in one module instead of spreadsheets.'],
      ['Accountability', 'Every MBQ change and every allocation action is logged with a reason, the user, and the time.'],
      ['New-store ready', 'For a new (UPC) store, know exactly what fittings/consumables it needs to open, and in what order (older store first).'],
      ['Right split', 'Decide what to send from the warehouse, what to buy, what to hold, and what to pull back — automatically.'],
    ],
  },
  {
    id: 'concepts', emoji: '🔑', title: 'Key words you will see',
    rows: [
      ['MBQ', 'The target quantity a store should have for a reference article.'],
      ['Reference article', 'A group of actual articles (e.g. one hanger type across sizes). Targets are set at this level; the actual article is chosen at allocation.'],
      ['Central vs Local', '**Central** = shipped from the RDC/warehouse (goes through allocation). **Local** = handled at the store. Set per reference article; changeable.'],
      ['UPC vs Old', '**UPC** = a new project store not yet opened. **Old** = an opened/running store. Read automatically from the store master — when a store opens, it becomes Old on its own.'],
      ['Warehouse (MSA) pool', 'Free stock at the DC available to send. Read from ET_MSA_STK per the SLOCs you switch on.'],
      ['Pack size', 'Items ship in whole packs; quantities are rounded to the pack size (minimum 1 pack).'],
    ],
  },
  {
    id: 'screens', emoji: '🧭', title: 'FSD — how each screen works',
    lead: 'The seven screens follow the natural flow: set up → target → who → allocate → track.',
    rows: [
      ['SLOC Settings', 'Switch on which stock locations count as **store stock** and which count as the **warehouse pool**, separately for FA and CONS.'],
      ['Stock & MSA', 'Calculate and view current stock (store + warehouse), SLOC-wise, per stream.'],
      ['MBQ Master', 'Upload the targets (one sheet: store · ref · qty · remarks). Rows auto-sort into FA vs CONS. Every change needs a reason; a Change-Review tab and per-row history show the full audit.'],
      ['UPC Store List', 'The stores this module runs for. Validate them against the store master (missing-in-master / missing-in-list reports), see UPC/Old status and the opening-date priority.'],
      ['Allocation', 'The engine: pick stream + store scope, click Run. For each shortfall it sends whole packs from the warehouse, oldest store first, then splits the reference article into actual articles (most stock first) — with a manual override.'],
      ['Pending Alloc', 'What has been approved to send but not yet delivered. Record deliveries (they auto-close when complete), close or reopen lines, or add a line by hand. Every step is in the operations log.'],
      ['Gap Report', 'The recommendation view: MBQ vs store stock + pending, validated against the warehouse — suggests **Dispatch**, **Purchase**, **Hold**, or **Store-return** per line. Click Run to compute.'],
    ],
  },
  {
    id: 'flow', emoji: '➡️', title: 'A typical cycle',
    bullets: [
      '**1. Set up** — switch on SLOCs (SLOC Settings) and build the store list (UPC Store List).',
      '**2. Targets** — upload the MBQ sheet (MBQ Master), with reasons.',
      '**3. Check** — open the Gap Report to see who is short and what to dispatch vs buy.',
      '**4. Allocate** — run Allocation for FA/CONS × UPC/Old/All; adjust the article split if needed.',
      '**5. Track** — approve into Pending Alloc, record deliveries as they arrive, close when done.',
    ],
  },
  {
    id: 'note', emoji: 'ℹ️', title: 'Good to know',
    bullets: [
      'The warehouse pool reads from **ET_MSA_STK**. Until FA/CONS stock is maintained there, the Gap Report and Allocation will show everything as **Purchase** (correct — nothing to send yet); they switch to **Dispatch** automatically once that stock exists.',
      'Filters everywhere support **multiple values with commas** (type `a, b, c`) and the column funnels let you tick specific values from a searchable list.',
    ],
  },
]

export default function FaConsHelpPage() {
  return <HelpPage title="FA & CONS · Help" tag="Guide" sections={SECTIONS}
    intro="A plain-language guide to the FA & CONS module — what it does (BRD), the key terms, and how each screen works (FSD)." />
}
