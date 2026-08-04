import HelpPage from '@/components/facons/HelpPage'

const SECTIONS = [
  {
    id: 'what', emoji: '📦', title: 'What GRT ALC (Bin Allocation) is',
    lead: 'It assigns pre-packed **bins** (received from the DC or vendor) to the best-fit **store** — giving each whole bin to a single store, **without splitting it**, so the bin stays intact at the DC.',
    bullets: [
      'A "bin" is a sealed box with a known mix of articles and quantities.',
      'The engine matches a bin\'s contents to a store\'s open requirement and picks the store it fits best.',
      'Bins that don\'t fit any store well are surfaced as **unallocated** for review.',
    ],
  },
  {
    id: 'why', emoji: '🎯', title: 'BRD — why it exists',
    lead: 'Today thousands of bins across hundreds of stores are matched by hand in Excel with VLOOKUPs — hours per cycle, inconsistent decisions, no record of why a bin went where, and lost work if the laptop crashes.',
    rows: [
      ['Less effort', 'Bring a ~6-hour manual cycle down to under 30 minutes.'],
      ['Better fit', 'Aim for ≥ 85% of bins aligned at the primary threshold.'],
      ['Less excess', 'Keep over-allocation to a store under ~5%.'],
      ['Full audit', 'Every decision — why a bin was placed or skipped — is logged.'],
      ['No data loss', 'Runs on the server as a job; state is saved, so a long cycle survives a refresh or crash.'],
    ],
  },
  {
    id: 'terms', emoji: '🔑', title: 'Key words you will see',
    rows: [
      ['Bin', 'A pre-packed box with a fixed list of articles + quantities. Allocated whole (no splitting).'],
      ['Requirement (REQ)', 'A store\'s open need. Pulled from the latest allocation output (or uploaded as a fallback).'],
      ['Eligibility %', 'How well a bin fits a store = matched quantity ÷ total bin quantity. Higher = better fit.'],
      ['Threshold', 'The minimum eligibility % to place a bin. **Fixed** = one cut-off; **Cascading** = try a high bar first, then relax step by step.'],
      ['Level', 'What "match" means: by **MAJ_CAT**, by **SIZE**, or by **MAJ_CAT + SIZE**.'],
      ['Strategy', '**One bin → all stores** (find the best store for each bin) or **one store → all bins** (fill each store from the bins).'],
    ],
  },
  {
    id: 'screens', emoji: '🧭', title: 'FSD — how each screen works',
    lead: 'Five screens, in order: load bins → get the need → run → review → audit.',
    rows: [
      ['Bin Master', 'Upload / view the bins and their contents (article, qty per bin).'],
      ['Requirement', 'The open store requirement the bins are matched against — pulled from the latest allocation (or uploaded).'],
      ['Run Allocation', 'Choose the level, threshold (fixed/cascading) and strategy, then run. It runs as a server job you can pause / resume / stop.'],
      ['Results & Export', 'See aligned bins (which store, eligibility %, excess) and unaligned bins; export aligned / unaligned / summary.'],
      ['Eligibility Log', 'The decision trail — for every bin, why it was placed with a store or left unallocated.'],
    ],
  },
  {
    id: 'how', emoji: '⚙️', title: 'How the matching works (simple)',
    bullets: [
      'For each bin, the engine scores how well it fits each candidate store (eligibility % at the chosen level).',
      'It places the bin with the **best-fitting** store that clears the threshold — **greedy best-fit**, whole bin only.',
      'With a **cascading** threshold, if nothing clears the high bar, it lowers the bar step by step until a fit is found (or the bin stays unallocated).',
      'Any quantity a store gets **beyond its requirement** is tracked as **excess**; bins with no acceptable fit are listed as **unallocated**.',
    ],
  },
  {
    id: 'note', emoji: 'ℹ️', title: 'Good to know',
    bullets: [
      '**Status:** specced and scaffolded (2026-07-24) — the engine is built **server-side** as an ARS job (not a browser tool), for audit, RBAC, and crash-safety. Backend build is pending.',
      'The **algorithm** (greedy best-fit, no splitting, eligibility %, thresholds, strategies) is kept exactly as designed; only the runtime moved to the server.',
    ],
  },
]

export default function BinAllocHelpPage() {
  return <HelpPage title="GRT ALC · Help" tag="Guide" sections={SECTIONS}
    intro="A plain-language guide to Bin Allocation (GRT ALC) — what it does (BRD), the key terms, and how each screen and the matching work (FSD)." />
}
