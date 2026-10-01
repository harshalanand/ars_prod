import HelpPage from '@/components/facons/HelpPage'

// GRT ALC — Bin-to-Bin Transfer. Canonical spec: frontend/public/docs/manual/bin_alloc.md.
const SECTIONS = [
  {
    id: 'what', emoji: '📦', title: 'What GRT ALC does',
    lead: 'It reads what the RDC **bins** hold and what each **store** asked for, sets a target for every store and article, and turns the gap into a **bin-by-bin pick list** the warehouse can walk.',
    bullets: [
      'One bin can feed several stores — bins are **split** as needed.',
      'Every run is kept as its own **session**; nothing is ever overwritten.',
      'It replaces the GRT_ART_ALLOC Streamlit tool, and gives the same numbers: the demand table and the allocation were both checked line for line against the tool on the same data.',
    ],
  },
  {
    id: 'steps', emoji: '🧭', title: 'The five steps',
    lead: 'In this order. Each only makes sense once the one before it has finished; the bar at the top of every page shows where you are.',
    rows: [
      ['1 · Upload Data', 'Load the workbook\'s three sheets — Bin Master, STORE MASTER, REQ. Check the file first; nothing is written until you press Load.'],
      ['2 · Settings', 'The 19 settings. Each says whether changing it means rebuilding the demand table or just running again.'],
      ['3 · Build MBQ', 'One row per store × article: the target (MBQ), what the store holds, and the shortfall allocation may fill. Rebuild after every load or demand-setting change; the page says when it is out of date. Click any row to see why it has that number.'],
      ['4 · Run Allocation', 'Choose how stock is shared, which warehouse may ship (required every time), who goes first and how bins are picked. Try a dry run first; every run is its own session and is checked before it counts.'],
      ['5 · Sessions & Pick List', 'The pick list in warehouse walking order, the lines, what stayed in the bins and why, and “Why no stock?” for any store or store × article. Compare two runs. Export once every balance check has passed.'],
    ],
  },
  {
    id: 'daily', emoji: '📅', title: 'A normal cycle',
    rows: [
      ['1', 'Upload the new workbook (Overwrite). Fix anything marked Must fix; read the warnings.'],
      ['2', 'Build MBQ. Read its checks — especially how much of the shortfall rests on “no grid row = zero stock”.'],
      ['3', 'Open the Gap Report: where stock and requirement simply do not meet (no setting can change those).'],
      ['4', 'Dry run with the warehouse rule your operation actually ships by; adjust; then run for real.'],
      ['5', 'In Sessions, check the five balance checks are green, look at Where the stock went, and export the pick list.'],
    ],
  },
  {
    id: 'checks', emoji: '✅', title: 'The balance checks on every session',
    lead: 'Run automatically, on the stored rows, before a session counts. The pick list cannot be exported unless all of them pass.',
    rows: [
      ['Pick units = allocated units', 'Every unit given to a store is traced to a bin.'],
      ['No store over its REQ', 'No store gets more of a category + size than it asked for.'],
      ['No bin over what it holds', 'No bin is asked for more than it contains.'],
      ['No line over the shortfall', 'No store gets more of an article than it is short.'],
      ['Picks + leftovers = Bin Master', 'Bin by bin, what is picked plus what is left adds up to what was loaded.'],
    ],
  },
  {
    id: 'reports', emoji: '📊', title: 'The two reports',
    rows: [
      ['Gap Report', 'Thirteen sheets on where stock and requirement do not line up: every category + size with a verdict (stock but no REQ, REQ but no stock, not enough, more than enough…), stores missing from Store Master, spelling mismatches between the sheets, per-store fill for a session, leftover reasons, and the warehouse balance. Read only; download as one Excel or each sheet as CSV.'],
      ['Store × Article Extract', 'Paste or upload a store list and an article list and get every matching row of any GRT ALC table (or the stock grid and size-share master it reads). It counts first, tells you which codes were not found, then downloads CSV or Excel.'],
    ],
  },
  {
    id: 'upload', emoji: '⬆️', title: 'Uploading — what the check looks for',
    lead: 'The check reads every row, cleans it exactly as the Streamlit loader did, and reports every problem at once. Anything marked **Must fix** stops the load.',
    rows: [
      ['Missing or renamed column', 'Must fix. Known other names are accepted and shown — e.g. **article** is read as ART, **ACS_D** as ACC_D.'],
      ['Value too long, blank key, not a number', 'Must fix. Nothing is ever cut short or blanked silently.'],
      ['RDC and OLD\\NEW swapped', 'Must fix. Seen in the 31 Aug, 1 Sep and 8 Sep DH24 workbooks. One click swaps them for that load; the workbook is not changed.'],
      ['REQ stores missing from Store Master', 'Warning. The demand build skips any store it cannot find in Store Master, with no error — so their REQ can never be filled.'],
      ['Category or size in one sheet only', 'Warning. Stock and requirement that never meet. Spelling differences are called out separately.'],
    ],
  },
  {
    id: 'warehouse', emoji: '🏭', title: 'Warehouses',
    bullets: [
      'The workbooks\' bin codes carry no warehouse (**A2-0101-B1**). The warehouse comes from the workbook — read from its file name (…**-DH24**…), and you can change it. Bins are stored as **DH24-A2-0101-B1**.',
      'To use both warehouses, load one workbook with **Overwrite**, then the other\'s Bin Master and Store Master with **Append**.',
      'Every run asks which warehouse may ship: **Own RDC only**, **Own RDC first**, or **All RDCs**. There is no default. A warehouse with stores but no bins can only be served from the other RDC — or not at all under Own RDC only.',
    ],
  },
  {
    id: 'safe', emoji: '🛡️', title: 'What keeps it safe',
    bullets: [
      'A load writes all chosen sheets in **one transaction**; a build is checked before it replaces the demand table; a session writes its lines, picks and leftovers in one transaction and is checked before it counts. A cancel or a failure changes nothing.',
      'A check made before a newer load cannot be loaded; a run cannot start on an out-of-date demand table; a load or build waits while a run reads the tables.',
      'A refresh or a closed tab loses nothing; each page picks a running job back up.',
    ],
  },
  {
    id: 'terms', emoji: '🔑', title: 'Words you will see',
    rows: [
      ['REQ', 'A store\'s requirement for a category + size. It caps how much of that category + size the store can receive.'],
      ['ACC_D', 'A category figure, the same on every size. Split down to one article by the size share.'],
      ['MBQ', 'The target for one store and one article.'],
      ['Shortfall', 'MBQ (rounded) minus what the store holds, never below zero — what allocation may fill.'],
      ['Still sendable', 'What could still go to a store for an article after the run — the only column that answers “can more ship?”.'],
      ['Leftover reason', 'Why stock stayed in a bin: nobody asked for it, the stores\' REQ was already filled, it is not merchandise, and so on.'],
      ['Servable / orphan REQ', 'Requirement from stores in Store Master (servable) or not (orphan — it can never be filled).'],
    ],
  },
]

export default function BinAllocHelpPage() {
  return <HelpPage title="GRT ALC · Help" tag="Guide" sections={SECTIONS}
    intro="A plain-language guide to GRT ALC — Bin-to-Bin Transfer: what it does, the five steps, a normal cycle, the checks, the reports, and the words you will see." />
}
