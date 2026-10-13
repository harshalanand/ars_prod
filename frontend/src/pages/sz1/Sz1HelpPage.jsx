import HelpPage from '@/components/facons/HelpPage'
import { C } from '@/theme/colors'
import { FlowRow, DecisionFlow, DataTable } from '@/components/sz1/Sz1Ui'
import { SZ_SHARE_LADDER, SZ_SHARE_EXAMPLE } from '@/components/sz1/sz1Help'

// 1SZ ALC — size allocation. Canonical spec: frontend/public/docs/manual/sz1_alloc.md.
// Every box in a flowchart explains itself on hover.

const Caption = ({ children }) => <div style={{ fontSize: 12, color: C.textMuted, marginTop: 8, lineHeight: 1.5 }}>{children}</div>
const Two = ({ a, b }) => <div style={{ display: 'grid', gap: 16 }}>{a}{b}</div>
const Label = ({ children, color }) => <div style={{ fontSize: 12, fontWeight: 800, color: color || C.primary, margin: '0 0 8px', letterSpacing: '.02em' }}>{children}</div>

const WHOLE = (
  <>
    <FlowRow nodes={[
      { kind: 'data', title: 'ARS masters', body: 'MSA · grids · curves · I_ROD', tip: 'ARS_MSA_TOTAL, ARS_GRID_MJ, ARS_GRID_MJ_VAR_ART, Master_CONT_SZ, ARS_CALC_ST_* — refresh them before a wave.' },
      { n: 1, title: 'Prepare · Supply', body: 'broken-size candidates', tip: 'Prepare → Supply tab (Step 1): which warehouse articles qualify (R1–R4, set in Settings).' },
      { n: 2, title: 'Prepare · Demand', body: 'store × category × size need', tip: 'Prepare → Demand tab (Step 2): the size budget MJ_SZ_SRT for every listed store.' },
      { n: 3, title: 'Plan a run', body: 'stores → options → preview → run', tip: 'Step 3: whole packs to the chosen stores; Preview compares Own RDC and All RDC first; the run is saved as a session.' },
      { n: 4, title: 'Sessions', body: 'parked → approve', tip: 'Every run parks and holds its stock and size budget. Check it, then approve it into Pend ALC — or Re-fit, Remove stores, Discard.' },
      { n: 5, kind: 'good', title: 'Pend ALC', body: 'BDC → DO → closed', tip: 'Approve writes ARS_PEND_ALC rows (session 1SZ-<id>, SOURCE 1SZ). Pend ALC then sends the BDC, takes the DO and closes the rows.' },
    ]} />
    <Caption>Start on <b>Overview</b>: it checks readiness and tells you the next step. Prepare Supply, then Demand, then Plan a run. Every run uses <b>live</b> stock and parks as a session; run as many as you need — each one sees only what the others left. Approve each parked session into Pend ALC when it is right.</Caption>
  </>
)

const STEP1 = (
  <FlowRow nodes={[
    { kind: 'data', title: 'Read ARS_MSA_TOTAL', body: 'RDC × article stock', tip: 'Warehouse stock per article: STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q, PAK_SZ.' },
    { kind: 'check', title: 'FNL_Q > 0 ?', body: 'free stock only', tip: 'FNL_Q = STK_QTY − PEND_QTY. Rows with nothing free are invisible to every count.' },
    { title: 'Count per RDC', body: 'option · category stock', tip: 'VAR_COUNT (live sizes per option), OPT_FNL (option stock), CAT_FNL (category stock).' },
    { title: 'Company CONT %', body: 'CO → store average → 0', tip: 'Master_CONT_SZ has stores and CO, no RDC rows. ART_CONT = CO value × 100; when CO lacks the size, the average of the store rows × 100; 0 only when no row gives the MAJ_CAT + size a CONT (→ gap). OPT_CONT = Σ ART_CONT of the live sizes.' },
    { kind: 'check', title: 'Any enabled rule ?', body: 'and size-applicable', tip: 'R1 option CONT ≤ X % · R2 single A/NA (≤ option qty) · R3 category ≤ X pcs · R4 option ≤ option qty (shared with R2). Values set on the Step 1 page.' },
    { kind: 'good', title: 'Write SZ Base', body: 'then re-apply balances', tip: 'Replaces the table in one transaction, then refreshes the balances: BAL_FNL_Q = live free stock less what every parked session holds.' },
  ]} />
)

const STEP2 = (
  <>
    <FlowRow nodes={[
      { kind: 'data', title: 'Listed stores', body: 'LISTING = 1', tip: 'Master_ALC_INPUT_ST_MASTER, with each store’s home RDC.' },
      { title: '× category + size', body: 'pairs kept in Step 1 (any RDC)', tip: 'Every store gets every pair Step 1 kept in any warehouse, so the All RDC pool can serve it.' },
      { kind: 'check', title: 'Size share', body: 'store → company · missing = 0', tip: 'Listing Level 2 ladder, scaled to 100 %. A / NA = 100 %; no real sizes = 100 ÷ SZ_COUNT; else the store curve, or the company (CO) curve when the store has none, each ÷ its real-size total. A size the curve lacks gets 0 — no budget.' },
      { title: 'Targets', body: 'MJ_MBQ, MJ_SZ_MBQ', tip: 'MBQ = display + sale/day × ALC_D, rounded. The size target uses the share, from unrounded values; 0 when MJ_DISP × share is 0.' },
      { kind: 'good', title: 'MJ_SZ_SRT', body: 'size target − size stock', tip: 'The size budget at build time. A run uses REM_MJ_SZ_SRT: MJ_SZ_MBQ less the store’s stock now (grid, incl. pending) less what parked sessions hold.' },
    ]} />
    <div style={{ marginTop: 12 }}>
      <DataTable maxHeight={260} toolbar={false} sortable={false} filterable={false} cols={[['Measure', 'Measure'], ['Formula', 'Formula'], ['Example', 'Example']]} rows={[
        { Measure: 'MJ_MBQ', Formula: 'round(MJ_DISP + MJ_SAL_PD × ALC_D); 0 without display', Example: 'round(25 + 0.03 × 11) = 25' },
        { Measure: 'MJ_SRT', Formula: 'max(MJ_MBQ − MJ_STK, 0)', Example: '25 − 20 = 5' },
        { Measure: 'Share (STORE)', Formula: 'store CONT ÷ store real-size total × 100 (A / NA not in the total)', Example: '0.3059 ÷ 1.0 = 30.59 %' },
        { Measure: 'CONT_PCT_SZ_REV', Formula: '= CONT_PCT_SZ (0 stays 0 — no budget for that size)', Example: '30.59 %' },
        { Measure: 'MJ_SZ_MBQ', Formula: '0 when MJ_DISP × share = 0, else round(MJ_DISP × share + MJ_SAL_PD × share × ALC_D)', Example: 'round(7.65 + 0.10) = 8' },
        { Measure: 'MJ_SZ_SRT', Formula: 'max(MJ_SZ_MBQ − MJ_SZ_STK, 0)', Example: '8 − 3 = 5' },
      ]} />
    </div>
    <div style={{ marginTop: 14 }}>
      <Label>Size share — Listing Level 2 ladder, first match wins</Label>
      <DataTable maxHeight={300} toolbar={false} sortable={false} filterable={false} rows={SZ_SHARE_LADDER} />
      <Caption>A curve is the real sizes with a CONT above 0, scaled to 100 %. A / NA rows are never part of a divisor: before, a store curve with A = 1.0 halved every real size (HC02 · JGW_SKEEVY 2-3Y: 15.30 % → now 30.59 %; the category’s size budget 3 → now 12).</Caption>
    </div>
    <div style={{ marginTop: 14 }}>
      <Label>Worked example — HC02 · JGW_SKEEVY: MJ_DISP 25, MJ_SAL_PD 0.03, ALC_D 11, stock 20 → MJ_MBQ 25, MJ_SRT 5</Label>
      <DataTable maxHeight={220} toolbar={false} sortable={false} filterable={false} rows={SZ_SHARE_EXAMPLE} />
    </div>
  </>
)

const STEP3 = (
  <FlowRow nodes={[
    { kind: 'data', n: 1, title: 'Check stores', body: 'ok / skipped', tip: 'Not in master · not listed · nothing short · ok. Only ok stores run.' },
    { n: 2, title: 'Candidate lines', body: 'short size × article', tip: 'Each short store × size joined to the Step 1 articles of that size (any RDC under All RDC, home RDC under Own RDC).' },
    { n: 3, title: 'Entitlement', body: 'ART_MBQ → ART_SRT_ROD', tip: 'ART_MBQ = round(ACS_D × share + sale/day × ALC_D); entitlement = packround(I_ROD × ART_MBQ − store stock). Lines with 0 drop out.' },
    { n: 4, title: 'Priority', body: 'ST_SEQ per category + size', tip: 'Balanced (weighted ranks), biggest total shortage, or your own order.' },
    { n: 5, kind: 'start', title: 'Allocate packs', body: 'see “How each pack is decided”', tip: 'Round by round, store by store, one pack at a time, every limit checked.' },
    { n: 6, kind: 'good', title: 'Check & park', body: 'all limits, one transaction', tip: 'Lines, the 10 limit checks, the balances and the session header commit together and the session parks; a breach writes nothing.' },
  ]} />
)

const POOL = (
  <>
    <Two
      a={<div><Label>All RDC — the default</Label><FlowRow nodes={[
        { kind: 'data', title: 'DH24 4 + DW01 3', body: 'article A', tip: 'Each RDC’s free stock of the article.' },
        { kind: 'start', title: 'Pool = 7', body: 'one stock', tip: 'Clubbed into one pool; one pack size per article.' },
        { title: 'Allocate', body: 'all stores, priority order', tip: 'Stores of every RDC compete for the pool.' },
        { kind: 'check', title: 'Protect', body: 'other RDC: surplus only', tip: 'Protect other RDC’s stores (on by default): a store may take another RDC’s stock only beyond what that RDC’s own listed stores need.' },
        { kind: 'check', title: 'Split', body: 'one source per line', tip: 'One source per line (on by default): a store’s line is filled from one warehouse that can cover it; it is split only when none can. Off: each pack from the own RDC while it holds one, else the RDC holding the most.' },
      ]} /></div>}
      b={<div><Label color={C.textSub}>Own RDC</Label><FlowRow nodes={[
        { kind: 'data', title: 'DH24 4 | DW01 3', body: 'article A', tip: 'Two separate pools.' },
        { title: 'DH24 stores', body: 'only DH24’s 4', tip: 'A DH24 store sees only DH24 stock.' },
        { title: 'DW01 stores', body: 'only DW01’s 3', tip: 'A DW01 store sees only DW01 stock.' },
        { kind: 'good', title: 'No split', body: 'one row per line', tip: 'Nothing crosses warehouses.' },
      ]} /></div>} />
    <div style={{ marginTop: 14 }}>
      <Label>Worked example — pack 1, S1 (home DW01, entitled 5) goes before S2 (home DH24, entitled 3)</Label>
      <DataTable maxHeight={220} toolbar={false} sortable={false} filterable={false} cols={[['Pack', 'Pack'], ['Store', 'Store'], ['From', 'From'], ['Why', 'Why'], ['Left', 'DH24 · DW01 left']]} rows={[
        { Pack: 1, Store: 'S1', From: 'DW01', Why: 'own RDC holds a pack', Left: '4 · 2' },
        { Pack: 2, Store: 'S2', From: 'DH24', Why: 'own RDC holds a pack', Left: '3 · 2' },
        { Pack: 3, Store: 'S1', From: 'DW01', Why: 'own RDC', Left: '3 · 1' },
        { Pack: 4, Store: 'S2', From: 'DH24', Why: 'own RDC', Left: '2 · 1' },
        { Pack: 5, Store: 'S1', From: 'DW01', Why: 'own RDC (last unit)', Left: '2 · 0' },
        { Pack: 6, Store: 'S2', From: 'DH24', Why: 'own RDC — S2 now has its 3', Left: '1 · 0' },
        { Pack: 7, Store: 'S1', From: 'DH24', Why: 'own RDC empty → the RDC holding the most', Left: '0 · 0' },
      ]} />
      <Caption>Result with both switches off: S1 gets 4 — split DW01 3 + DH24 1 (two rows, IS_HOME 1 and 0); S2 gets 3 from DH24. The whole pool of 7 is used. Under Own RDC, S1 could take only DW01’s 3.
        With <b>Protect other RDC’s stores</b> on, S1 takes DH24 stock only beyond what DH24’s own stores need. Live, 120 stores (12 Oct 2026): Own RDC 44,213 units · All RDC unprotected 55,172 (41 split lines) · protected + one source per line 51,240 (3 split lines), no limit breached.</Caption>
    </div>
  </>
)

const PACK = (
  <>
    <DecisionFlow
      start={{ title: 'Next store in priority order (ST_SEQ)', body: 'for this article, in this round', tip: 'Stores are visited one pack each, cycling, until a full pass gives nothing.' }}
      steps={[
        { title: 'Any warehouse holds a whole pack?', body: 'All RDC: the pool · Own RDC: home', no: 'Stop this article for the round', tip: 'A pack never ships half from two warehouses.' },
        { title: 'Store total + pack ≤ round target?', body: 'packround(round × ART_MBQ − stock)', no: 'Skip this store, try the next', tip: 'Round 1 target = ART_SRT; it grows each round up to I_ROD.' },
        { title: 'Size budget left ≥ 1 pack?', body: 'MJ_SZ_SRT', no: 'Skip this store, try the next', tip: 'The whole pack must fit the store’s size shortage.' },
        { title: 'Full option: MJ_SRT left ≥ 1 pack?', body: 'real sizes skip this check', no: 'Skip this store, try the next', tip: 'A / NA options only fill what the category has left after its real sizes.' },
      ]}
      end={{ title: 'Give 1 pack', body: 'from the line’s warehouse (All RDC: own RDC or the one that can cover the line; other RDC only from its surplus) · update stock and budgets', tip: 'Under Own RDC the pack always comes from home.' }} />
    <Caption>When a full pass over the stores gives nothing, the next article starts; after the last article, the next round.</Caption>
  </>
)

const LIVE = (
  <>
    <Two
      a={<div><Label>Stock a run may use — per RDC × article</Label><FlowRow nodes={[
        { kind: 'data', title: 'Live MSA free stock', body: 'ARS_MSA_TOTAL FNL_Q now', tip: 'Already net of every approved pending — listing, manual and approved 1SZ sessions.' },
        { kind: 'check', title: '− parked sessions', body: 'Σ their ALLOC_QTY', tip: 'Every parked 1SZ session holds its units until it is approved or discarded.' },
        { kind: 'good', title: 'BAL_FNL_Q', body: 'max(…, 0)', tip: 'What this run may allocate. Two parked sessions never hold the same pieces.' },
      ]} /></div>}
      b={<div><Label>Room a store has — per store × MAJ_CAT × size</Label><FlowRow nodes={[
        { kind: 'data', title: 'Size target', body: 'MJ_SZ_MBQ (Demand)', tip: 'From the Demand build (Step 2).' },
        { kind: 'check', title: '− store stock now', body: 'grid STK_TTL, incl. pending', tip: 'ARS_GRID_MJ_VAR_ART by size (A / NA: the category in ARS_GRID_MJ). It already includes PEND_ALC, and Pend ALC adds every approval at once — so pending is never given twice.' },
        { kind: 'check', title: '− parked sessions', body: 'Σ their ALLOC_QTY', tip: 'Size budget held by parked sessions.' },
        { kind: 'good', title: 'REM_MJ_SZ_SRT', body: 'max(…, 0)', tip: 'The size budget this run may fill. Full options: MJ_MBQ − category stock now − parked.' },
      ]} /></div>} />
    <Caption>There is no Full / Remaining stock choice any more: every run, preview and approve re-check works on these live figures.</Caption>
  </>
)

const LIFE = (
  <>
    <FlowRow nodes={[
      { kind: 'start', title: 'Run', body: 'RUNNING', tip: 'Plan a run → Run. You can cancel while it runs; a cancel or a limit breach writes nothing.' },
      { kind: 'check', title: 'Parked', body: 'holds stock + size budget', tip: 'Any number of sessions can be parked. Listing does not see parked stock — approve or discard within 2 days.' },
      { title: 'Approve', body: 're-check, then write', tip: 'Needs ACT_1SZ_APPROVE. Blocked when live stock or a store’s room no longer covers the session → Re-fit.' },
      { title: 'Pend ALC', body: '1SZ-<id> · SOURCE 1SZ', tip: 'ARS_PEND_ALC rows; MSA free stock and the grid change at once. Revert is possible until a BDC or DO.' },
      { kind: 'good', title: 'BDC → DO → closed', body: 'in Pend ALC', tip: 'Sessions shows the progress: BDC sent, DO received, closed — “Shipped” when every row is closed.' },
    ]} />
    <div style={{ marginTop: 12 }}>
      <DecisionFlow
        start={{ title: 'Approve → Pend ALC', body: 'live re-check first (read only)', tip: 'Nothing is written by the re-check; Check again repeats it.' }}
        steps={[
          { title: 'Session parked and its checks passed?', body: 'status Parked · all limits held', no: 'Blocked — nothing to approve', tip: 'Only a parked session whose limit checks passed can be approved.' },
          { title: 'Live free stock covers every RDC × article?', body: 'MSA FNL_Q now', no: 'Blocked → Re-fit or Discard', tip: 'Listing or another approval may have taken the stock since the run.' },
          { title: 'Every store × MAJ_CAT × size still has room?', body: 'stock now + other parked sessions', no: 'Blocked → Re-fit or Remove stores', tip: 'The store may have received stock since the run.' },
        ]}
        end={{ title: 'Approve — one transaction', body: 'ARS_PEND_ALC rows · MSA + grid updated · APPROVE_1SZ logged · session Approved', tip: 'Any failure rolls everything back. Warnings (an older Supply / Demand build, a session older than 2 days) do not block.' }} />
    </div>
  </>
)

const DAY = (
  <FlowRow nodes={[
    { n: 1, kind: 'data', title: 'Overview', body: 'readiness · next step', tip: 'Shows what is missing, stale or blocking, and the one thing to do next.' },
    { n: 2, title: 'Prepare', body: 'Supply, then Demand', tip: 'Generate Supply (Step 1), then Demand (Step 2). Check the Gap tab for sizes without CONT.' },
    { n: 3, title: 'Plan a run', body: 'stores · options · preview', tip: 'Paste the stores, Check stores, keep All RDC, Preview (compares Own RDC and All RDC in memory), then Run.' },
    { n: 4, title: 'Sessions', body: 'check · approve', tip: 'Limits green, fill per store, split lines — then Approve → Pend ALC (Re-fit if blocked).' },
    { n: 5, kind: 'good', title: 'Pend ALC', body: 'BDC · DO', tip: 'Pend ALC sends the BDC, records the DO and closes the rows. Open in Pend ALC jumps to Reconciliation filtered to the session.' },
  ]} />
)

const SECTIONS = [
  {
    id: 'what', emoji: '📏', title: 'What 1SZ ALC does',
    lead: 'It moves **broken-size and residual warehouse stock** (DH24, DW01) to the stores that are short of **exactly those sizes** — in whole packs, never above a store’s size budget.',
    bullets: [
      'Standard ARS plans per option and does not chase thin size runs; options down to one or two live sizes stay in the warehouse and age. 1SZ ALC picks them up.',
      'Stock means **free stock**: FNL_Q = STK_QTY − PEND_QTY.',
      'Every run is a **session** that **parks**: it holds its stock and size budget until you **approve** it into Pend ALC or discard it. Any number can be parked; each run sees only what the others left.',
      'Every run uses **live** stock: MSA free stock now and each store’s stock now, less all parked sessions. There is no Full / Remaining choice.',
      'An approved session becomes **ARS_PEND_ALC** rows (session **1SZ-<id>**, SOURCE **1SZ**); Pend ALC then runs the BDC, the DO and closing.',
      '**Hover** any button, option, column header or flowchart box in 1SZ ALC for what it does.',
    ],
  },
  { id: 'process', emoji: '🧭', title: 'The whole process', lead: 'Overview → Prepare (Supply, Demand) → Plan a run → Sessions → Pend ALC. Each step reads the table the step before it built. Rules live in Settings.', content: WHOLE },
  {
    id: 'day', emoji: '🗓️', title: 'A day with 1SZ ALC', lead: 'The usual order of work, start to finish.', content: DAY,
    rows: [
      ['1. Overview', 'Read the readiness and the next step. Amber is a warning (e.g. a session parked more than 2 days); red blocks.'],
      ['2. Prepare', 'Generate **Supply**, then **Demand** (Demand reads the Supply you just built). Skip when both are current.'],
      ['3. Plan a run', 'Stores → Options → **Preview** (Own RDC vs All RDC, writes nothing) → **Run**. The session parks.'],
      ['4. Sessions', 'Check the limits and the fill, then **Approve → Pend ALC**. If the re-check is blocked, **Re-fit** (or Remove stores / Discard).'],
      ['5. Pend ALC', 'The rows sit in ARS_PEND_ALC as **1SZ-<id>**: send the BDC, take the DO; the session shows BDC sent, DO received, closed.'],
      ['More runs', 'Run again at any time — each run nets the parked and approved sessions, so nothing is given twice.'],
    ],
  },
  {
    id: 'step1', emoji: '1️⃣', title: 'Prepare · Supply (Step 1) — step by step',
    lead: 'Which warehouse articles are broken-size candidates. One row per RDC + article.', content: STEP1,
    rows: [
      ['0. Set the rules', 'In **Settings** (or the Selection rules popup): switch each rule on or off and set its value — R1 option CONT % (default 40), R3 category pcs (200), and one option-qty value shared by R2 and R4 (20). **Preview these values** tries them without saving; **Save** applies them from the next Generate. **Tool rules** loads the Streamlit tool’s fixed rules.'],
      ['1. Preview', 'Shows the first rows and the per-RDC counts. Writes nothing.'],
      ['2. Tick + Generate', 'Replaces ARS_SZ1_MSA_SZ_BASE in one transaction and refreshes the balances (live free stock less every parked session).'],
      ['3. Read the summary', 'By RDC, by rule combination, by CONT source, by live sizes. Example: M_BRIEF 1114094526 at DH24 has only sizes 100 (11.27 %) and 105 (2.00 %) left → OPT_CONT 13.27 % → R1.'],
    ],
  },
  {
    id: 'step2', emoji: '2️⃣', title: 'Prepare · Demand (Step 2) — step by step',
    lead: 'How short each listed store is, per category and size. MJ_SZ_SRT caps what Step 3 may send.', content: STEP2,
    rows: [
      ['0. Readiness', 'Checked before anything is written: sources, build order, freshness, coverage, parked sessions, changed rules. Red blocks Generate; amber is a warning.'],
      ['1. Preview', 'Sample rows and the grid size per RDC. Writes nothing.'],
      ['2. Tick + Generate', 'Replaces ARS_SZ1_ST_MJ_SZ_REQ_BASE; rebuild it after every Step 1 build (Readiness warns when it is older).'],
      ['3. Read the summary', 'Rows and shortage per RDC, and which rule supplied the size share (CONT_SRC).'],
    ],
  },
  {
    id: 'step3', emoji: '3️⃣', title: 'Plan a run (Step 3) — step by step',
    lead: 'A wizard: 1 Stores → 2 Options → 3 Preview → 4 Run.', content: STEP3,
    rows: [
      ['Warehouse', '**All RDC** (default): pool every warehouse, allocate, split by source — with **Protect other RDC’s stores** and **One source per line**, both on by default. **Own RDC**: each store only from its home warehouse.'],
      ['Store priority', 'Balanced (weight w between total and size shortage), Biggest total shortage, or Own sequence.'],
      ['Stock', 'Always **live**: MSA free stock now and each store’s room now, less every parked session (see Live stock below). No Full / Remaining choice.'],
      ['Preview → Run', '**Preview** allocates in memory under Own RDC and All RDC side by side (pieces, lines, split lines, from the other RDC, size need met, per store) and writes nothing; **Use this** switches the option. Run allocates in the background; you can cancel. The session **parks** and opens in Sessions.'],
    ],
  },
  { id: 'live', emoji: '🔄', title: 'Live stock — what a run may use', lead: 'Every run, preview and approve re-check nets the **live** figures against **all parked sessions**, so two sessions never hold the same pieces or the same size budget.', content: LIVE },
  { id: 'pool', emoji: '🏭', title: 'All RDC vs Own RDC — pool, allocate, split', lead: 'Under **All RDC** every warehouse’s stock of an article is one pool; the allocated packs are then split back to the warehouses they come from.', content: POOL },
  { id: 'pack', emoji: '📦', title: 'How each pack is decided', lead: 'A pack goes to a store only when every limit still has room. Real sizes run all their rounds before full options; everyone gets round 1 before round 2.', content: PACK },
  {
    id: 'checks', emoji: '✅', title: 'The limits checked on every session',
    lead: 'Checked on the stored rows before the session counts. If any is breached the whole session is rolled back — lines, balances, everything — and marked FAILED with examples.',
    rows: [
      ['Size budget', 'No store receives more than MJ_SZ_SRT of a category + size.'],
      ['Entitlement', 'No store receives more of an article than ART_SRT_ROD.'],
      ['Warehouse', 'No warehouse ships more of an article than it holds.'],
      ['Surplus', 'With Protect other RDC’s stores (and the legacy modes): other-RDC stores take no more than that RDC’s surplus, and none from a zero surplus.'],
      ['Whole packs', 'Every quantity a whole number of packs; none negative; nothing without an entitlement.'],
      ['Full options', 'A full option keeps the store’s category total within MJ_SRT.'],
      ['Stored = run', 'The rows written match the run, line for line and unit for unit.'],
    ],
  },
  {
    id: 'sessions', emoji: '🅿️', title: 'Sessions — park, approve into Pend ALC',
    lead: 'RUNNING → **Parked** → **Approved** (in Pend ALC) or **Discarded**. A reverted approval goes back to Parked. Failed and cancelled runs hold nothing.',
    content: LIFE,
    rows: [
      ['Park', 'Every finished run parks. It holds its units and size budget from every other run until approved or discarded. Any number may be parked.'],
      ['Approve → Pend ALC', 'Re-checks live stock and room first (read only). If nothing moved, one transaction writes **ARS_PEND_ALC** rows — session **1SZ-<id>**, SOURCE / ALLOC_MODE **1SZ**, RDC = the ship-from RDC, FRESH / GRT from Supply (a mixed article split by live FRESH free stock, FRESH first) — updates **MSA and the grid**, logs APPROVE_1SZ and marks the session Approved. Any failure rolls it all back.'],
      ['Then in Pend ALC', 'The rows are ordinary pending: **BDC → DO → closed** (also reco and adhoc close). Sessions shows BDC sent, DO received, closed; “Shipped” when every row is closed. **Open in Pend ALC** opens Reconciliation filtered to the session.'],
      ['Re-fit', 'When the re-check is blocked: discards this session and re-runs the same stores and options on what is left now. The new session parks (marked “re-fit of” the old one).'],
      ['Remove stores', 'Drop some stores from a parked session: their lines are deleted, totals and summary recomputed, their stock and room freed at once.'],
      ['Discard', 'Drop a parked session: its stock and size budget go back to the pool. Lines are kept for audit; nothing reaches Pend ALC.'],
      ['Revert', 'Takes an approved session back out of ARS_PEND_ALC (MSA and the grid given back, rows deleted) and parks it again — only while **no row has a BDC or DO**. From the session, or from Pend ALC → Operations Log.'],
      ['Permission', 'Approve and Revert need **ACT_1SZ_APPROVE** (seeded to SUPER_ADMIN; tick it for other roles in Settings → Roles).'],
      ['Stale warning', 'A session parked more than **2 days** is flagged (Overview, Readiness, Sessions): listing cannot see parked stock, so approve or discard it. Approve also warns on an older Supply / Demand build.'],
    ],
  },
  {
    id: 'terms', emoji: '🔑', title: 'Words you will see',
    rows: [
      ['FNL_Q / BAL_FNL_Q', 'Free warehouse stock (live MSA) / what a run may use: live free stock less every parked session.'],
      ['MJ_SRT', 'A store’s shortage for a whole category.'],
      ['MJ_SZ_SRT / REM_MJ_SZ_SRT', 'The same for one size — the size budget at build time / the room now (target − store stock now − parked sessions).'],
      ['ART_SRT_ROD', 'What a store may receive of one article over all its refill rounds.'],
      ['SRC_RDC / IS_HOME', 'The warehouse a row ships from / 1 when it is the store’s own.'],
      ['Split line', 'A store × article served from two warehouses: one row per source.'],
      ['UNMET_QTY', 'What a store was entitled to but did not get.'],
      ['Parked / Approved', 'A session holding stock inside 1SZ / one written to ARS_PEND_ALC.'],
      ['1SZ-<id>', 'The SESSION_ID of an approved session’s rows in ARS_PEND_ALC (PEND_SESSION_ID on the 1SZ session).'],
    ],
  },
]

export default function Sz1HelpPage() {
  return <HelpPage title="1SZ ALC · Help" tag="Guide" sections={SECTIONS}
    intro="Size allocation of broken-size warehouse stock, step by step with flowcharts: the whole process, a day with 1SZ ALC, each step, live stock, how All RDC pools and splits stock, how each pack is decided, the checks every session passes, and how sessions park and are approved into Pend ALC. Hover any box for detail." />
}
