/**
 * guideSteps — data for the click-by-click Process manual.
 * One entry = one step card: annotated screenshot + comment (+ tip / warn).
 * Images are captured by scratchpad/capture_steps.js; re-running it never
 * touches these captions.
 */

const img = (module, n, name) => `/docs/guide/${module}/step-${String(n).padStart(2, '0')}-${name}.png`

export const MODULES = [
  { id: 'start',      title: 'Getting Started',            short: 'Start',       desc: 'Log in and find your way around' },
  { id: 'msa',        title: 'Step 1 · MSA Stock',         short: 'MSA',         desc: 'Compute free-to-allocate stock' },
  { id: 'grid',       title: 'Step 2 · Grid Builder',      short: 'Grid',        desc: 'Build MBQ budgets per store' },
  { id: 'merge',      title: 'Step 3 · Merge Rules',       short: 'Merge',       desc: 'Combine range segments' },
  { id: 'listing',    title: 'Step 4 · Listing & Alloc',   short: 'Listing',     desc: 'The main run screen — every knob' },
  { id: 'review',     title: 'Step 5 · Review Results',    short: 'Review',      desc: 'Approve, reject, inspect' },
  { id: 'hold',       title: 'Step 6 · Hold Process',      short: 'Hold',        desc: 'Stock reserved, not shipped' },
  { id: 'pendalc',    title: 'Step 7 · Pending Allocation',short: 'Pend-Alc',    desc: 'From promise to delivery' },
  { id: 'dictionary', title: 'Data Dictionary Tips',       short: 'Dictionary',  desc: 'Look up any column, any formula' },
]

export const STEPS = {
  /* ═══ Getting started ══════════════════════════════════════════ */
  start: [
    { n: 1, name: 'login', title: 'Log in',
      comment: 'Open the ARS URL and sign in with your username and password. Your role decides which menus you will see afterwards.',
      tip: 'Sessions expire — the timer in the top bar shows how long you have been logged in.' },
    { n: 2, name: 'dashboard', title: 'The ARS Dashboard',
      comment: 'After login you land on the dashboard: total allocated quantity, allocation split by OPT_TYPE, top MAJ_CATs and gap charts. It reads from the last approved run.',
      tip: 'Use the date filter (top right of the dashboard) to look at earlier periods.' },
    { n: 3, name: 'sidebar', title: 'Moving around the sidebar',
      comment: 'The sidebar opens one section at a time. Click a section name to open it — the previous one closes. A small dot on a closed section means the page you are on lives inside it.',
      tip: 'Keyboard: ↑/↓ move, → opens a section, ← closes it, Enter opens the page. When the rail is collapsed, hover or press Enter on an icon for its fly-out menu.' },
    { n: 4, name: 'pipeline-home', title: 'Where the pipeline lives',
      comment: 'Everything in this guide happens under Listing & Alloc: MSA Stock Calculation → Grid Builder → Merge Rules → Listing. Reports and Pending Allocation have their own sections.',
    },
  ],

  /* ═══ 1 · MSA ══════════════════════════════════════════════════ */
  msa: [
    { n: 1, name: 'open', title: 'Open MSA Stock Calculation',
      comment: 'Listing & Alloc → MSA Stock Calculation. This screen answers: how much sellable stock exists per option per RDC? Every later step splits this number.',
    },
    { n: 2, name: 'sources', title: 'Check data freshness and filters',
      comment: 'The green strip confirms the ET_MSA_STK source date. Below it, pick the store codes (RDCs), storage locations (SLOCs) and SEG (APP/GM) to include. V02_FRESH is the fresh pool; V02_GRT the returns pool.',
      warn: 'If the data date is old, upload fresh stock before running — an MSA on stale stock poisons every downstream number.' },
    { n: 3, name: 'run', title: 'Press "Calculate MSA"',
      comment: 'The blue button starts the 9-step job: filter SLOCs → normalize → fill dims → tag SEG → pivot → merge pending → FNL_Q = MAX(stock − pending − hold, 0) → colour variants → aggregate.',
      tip: 'Only 1 SLOC selected? The yellow note warns coverage will be limited — use "Select All" for a full run.' },
    { n: 4, name: 'outputs', title: 'Check the outputs',
      comment: 'When the job finishes, results land in ARS_MSA_TOTAL (totals), ARS_MSA_GEN_ART (per colour — Listing reads this) and ARS_MSA_VAR_ART (per size). Stored Calculation Sequences at the bottom keeps a history of runs.',
      tip: 'Worked example: 420 fresh pieces − 60 pending = FNL_Q 360 for GEN_ART 1114058292 · NAVY at DW01.' },
  ],

  /* ═══ 2 · Grid Builder ═════════════════════════════════════════ */
  grid: [
    { n: 1, name: 'open', title: 'Open Grid Builder',
      comment: 'Listing & Alloc → Grid Builder. Grids are budget tables: how many pieces each store should hold per MAJ_CAT, per fabric, per vendor, per range segment.',
    },
    { n: 2, name: 'majcat', title: 'Pick what to build',
      comment: 'Choose the MAJ_CAT(s) to (re)build — or everything for a full refresh. The primary grid (MJ_RNG_SEG, budget per MRP tier E/V/P/SP) plus the secondary fence grids (FAB, MICRO_MVGR, vendor) are produced together.',
    },
    { n: 3, name: 'tunables', title: 'Review the tunables',
      comment: 'Sales window, growth factors and rounding drive the MBQ formula: MBQ = ROUND(SAL_PD × ALC_D + DISP_Q, 0) × store contribution. Defaults are right for a routine refresh.',
    },
    { n: 4, name: 'build', title: 'Build the grids',
      comment: 'Press Build. Every grid grain gets MBQ (target) and REQ = MAX(MBQ − stock, 0) (what the store still needs). Results write to the ARS_GRID_* tables and register in ARS_GRID_HIERARCHY.',
      warn: 'A secondary-grid MBQ of 0 means "no fence at this level" — never "ship zero".' },
    { n: 5, name: 'results', title: 'Read the result table',
      comment: 'Example HB05 × M_TEES_HS: tier E gets MJ_MBQ 24 with stock 6 → MJ_REQ 18. The COTTON fence is 16, CREW-NECK fence 10 — allocation may fill the 18 but can never breach a fence.',
    },
    { n: 6, name: 'gap-banner', title: 'Why the Listing page nags about grids',
      comment: 'If any MAJ_CAT is missing from ARS_GRID_HIERARCHY, the Listing page shows this yellow banner and those MAJ_CATs are skipped. Click "Grid Builder →" to build them, or "Generate Anyway" to accept the gap knowingly.',
      warn: 'The count (e.g. 901/451 covered) tells you exactly how many MAJ_CATs are ready.' },
  ],

  /* ═══ 3 · Merge Rules ══════════════════════════════════════════ */
  merge: [
    { n: 1, name: 'open', title: 'Open Merge Rules',
      comment: 'Listing & Alloc → Merge Rules. A merge rule says "treat these range segments as one bucket" so a good option is not skipped just because one small tier is full while its sibling has room.',
    },
    { n: 2, name: 'rows', title: 'Read the current mappings',
      comment: 'Each row maps a source value to a target bucket: here E→EV, V→EV (economy + value merged) and P→PSP, SP→PSP. The header shows how many rules are active and the aggregation (SUM).',
    },
    { n: 3, name: 'mapping', title: 'Add or change a rule',
      comment: 'Click "New rule" (or the pencil on a row) to map a source segment to a merged bucket. Example effect: separate budgets E: REQ 1 and V: REQ 6 become one EV budget with REQ 7 — an option that was skipped now ships.',
    },
    { n: 4, name: 'save', title: 'Refresh the derived tables',
      comment: 'Rules made here auto-refresh the derived Master_CONT_MERGE tables. If anyone edited rules directly in SQL, press "Refresh derived" — then re-run Grid Builder for the affected MAJ_CATs so budgets rebuild on the merged buckets.',
      warn: 'Merging tiers that genuinely behave differently lets one tier starve the other. Merge only near-equal price bands.' },
  ],

  /* ═══ 4 · Listing & Allocation ═════════════════════════════════ */
  listing: [
    { n: 1, name: 'open', title: 'The cockpit',
      comment: 'Listing & Alloc → Listing. One Generate press runs listing (which options qualify where) and allocation (how many pieces of each size) together. The next 15 steps cover every control top-to-bottom.',
    },
    { n: 2, name: 'opt-types', title: 'OPT Types — what kinds of options to run',
      comment: 'All (RL→TBC→TBL) is the normal choice: refill live options first, then continuing ones, then new listings. "RL only" restricts the run to refilling what stores already sell.',
    },
    { n: 3, name: 'alloc-mode', title: 'Alloc — which engine',
      comment: 'Per-OPT (sequential, new) is the production default: one option at a time with honest skip reasons. Pandas is the vectorized alternative; Sequential is a slow SQL fallback.',
    },
    { n: 4, name: 'order-workers', title: 'Order, workers, Writer-Q',
      comment: 'Order A processes all RL rounds, then TBC, then TBL. Workers = parallel threads (4 is safe). Keep Writer-Q ON — it funnels all DB writes through one thread so workers cannot deadlock.',
      tip: 'Controls that do not apply to the selected engine grey out — they never disappear, so the toolbar does not jump.' },
    { n: 5, name: 'store-search', title: 'Choose stores',
      comment: 'Type a store code, name, RDC or hub — here "patna" lists every Patna store with its RDC · HUB shown on the right. Tick several: the list stays open while you pick. Each row has a checkbox.',
      warn: 'Leave the filter empty to run ALL active stores.' },
    { n: 6, name: 'hub-bulk', title: 'Select a whole hub or RDC at once',
      comment: 'Search a hub or RDC code (here "db03") and a highlighted group row appears: "HUB DB03 — select all 31 stores". One click ticks all of them; click again to untick. You can then remove individual stores below.',
    },
    { n: 7, name: 'majcat-search', title: 'Choose MAJ_CATs the same way',
      comment: 'The MAJ_CAT search understands SEG, DIV, SUB_DIV and SSN. Typing "ladies" offers "DIV LADIES — select all 68 MAJ_CATs" plus the individual categories, each showing its APP · LADIES · … path.',
    },
    { n: 8, name: 'stock-excess', title: 'Tunables — Stock & Excess',
      comment: 'Stock % (0.6) is the RL line: stock ≥ 60% × ACS_D (display quantity, default 18) classifies an option RL. Excess × marks overstock. Hold Days and AGE are lookback windows.',
      tip: 'ACS_D is how many pieces one option occupies on the floor — it is NOT a daily-sale number.' },
    { n: 9, name: 'store-ranking', title: 'Tunables — Store Ranking',
      comment: 'When stock is short, stores are served by score: Req % weighs how much a store needs, Fill % weighs its historical fill rate (60/40 default). The ranked order decides who gets scarce stock first.',
    },
    { n: 10, name: 'run-pool-hold', title: 'Run Pool & Hold Control',
      comment: 'Pick which warehouse pool this run draws from: Fresh or GRT (returns). One MSA covers both — the choice happens here, at run time. Hold Control toggles whether APP/GM holds apply and lets you skip holds for UPC stores.',
      warn: 'Generate stays disabled until a pool is selected.' },
    { n: 11, name: 'caps-growth', title: 'Tunables — Caps & Growth',
      comment: 'PRI ≥ 100% toggles list RL/TBC only where priority coverage is complete. Dispatch decides overflow behaviour: Complete (all-or-skip) keeps clean displays; Scaled ships a proportionally shrunk quantity. Growth % lifts every grid budget (110 = +10% headroom) without compounding on re-runs.',
    },
    { n: 12, name: 'sizing-season', title: 'Tunables — Sizing & Season',
      comment: 'Size Cov % skips a TBL option whose size run is too broken (default: less than half the sizes in stock). Keep Sec-grid Cap ON to enforce the fabric/vendor fences. SSN checkboxes restrict the run to chosen seasons.',
    },
    { n: 13, name: 'run-scope', title: 'Run Mode, RDC Scope, Mix-Line',
      comment: 'Run Mode: "Listing only" uses existing MSA + grids; "Full Pipeline" runs MSA → Grid → Listing → Allocation in one go. RDC Scope "Own" serves each store from its own RDC (normal). Mix-Line sets result granularity — "MAJ only" is one line per store × MAJ_CAT.',
    },
    { n: 14, name: 'key-numbers', title: 'Key Numbers — sanity check before running',
      comment: 'The tiles show MSA rows, grid rows, active stores, current listing size, new items and total alloc/hold quantities. Glance here before Generate: a zero where you expect millions means an upstream step was skipped.',
      tip: 'Every section collapses — click its header to hide it; the choice is remembered.' },
    { n: 15, name: 'generate', title: 'Generate',
      comment: 'Press Generate Listing. A live dashboard appears: stage strip (Listing → Allocation), a percentage bar, per-MAJ_CAT done/failed counts and a Retry Failed button. When it completes, the run is PARKED — nothing is final until you approve it (next module).',
      warn: 'While a parked run awaits review, Generate is blocked for everyone unless an admin allows multiple parked runs in Settings → Application.' },
    { n: 16, name: 'preview', title: 'Preview the result tables',
      comment: 'At the bottom, pick Working / Full Listing / Alloc, choose a row count and press Fetch to inspect the raw rows — including OPT_TYPE, ship quantities, skip reasons and remarks — without leaving the page.',
    },
  ],

  /* ═══ 5 · Review ═══════════════════════════════════════════════ */
  review: [
    { n: 1, name: 'parked-bar', title: 'The Parked Runs bar',
      comment: 'After a run completes, this amber-tagged bar appears at the top of the Listing page. Expand it to see the session: who ran it, parked row counts, total Ship and Hold quantities. Approve moves the 5 working tables into history — the run becomes official. Reject discards it.',
      warn: 'This bar only exists while a run awaits review — if you do not see it, there is nothing parked.' },
    { n: 2, name: 'view-logs', title: 'View Logs',
      comment: 'The View Logs button (next to Generate) opens the session history — every past Generate with its status, duration, row counts and per-session log file.',
    },
    { n: 3, name: 'session-logs', title: 'Listing Session Logs',
      comment: 'Each session shows SUCCESS/FAILED, the engine used, elapsed time and rows produced. Click a session to read its step-by-step log — the first place to look when a run behaves unexpectedly.',
    },
    { n: 4, name: 'alloc-review', title: 'Alloc Review',
      comment: 'Sidebar → Alloc Review: the browsing screen for allocation results. Check coverage (stores vs expected), volume vs last cycle, and that skip reasons are dominated by normal budget caps (MBQ_CAP_*).',
    },
    { n: 5, name: 'filters', title: 'Filter to what you care about',
      comment: 'Date range, store and MAJ_CAT filters narrow the view. Drill from MAJ_CAT totals down to store and size level.',
    },
    { n: 6, name: 'drill', title: 'Charts and drill-down',
      comment: 'The charts split allocation by RDC, hub, season and division. Worked example: HB05 × M_TEES_HS should show the 18 pieces traced through this guide (8 NAVY + 10 WHITE, BLACK skipped MBQ_CAP_MJ).',
    },
  ],

  /* ═══ 6 · Hold ═════════════════════════════════════════════════ */
  hold: [
    { n: 1, name: 'open', title: 'Open the Hold Dashboard',
      comment: 'Reports → Hold Dashboard. Hold = pieces the run deliberately reserved at the RDC instead of shipping: TBL/NL ramp-ups ship half now and hold the rest until the store proves sell-through.',
    },
    { n: 2, name: 'kpi', title: 'How much is held',
      comment: 'The total and per-RDC hold quantities. Held stock is subtracted from free stock (no other run can take it) but has no delivery order yet.',
      warn: 'Hold much larger than usual? The run listed many new options at once, or the hold window caught a big receiving week — confirm it is intentional.' },
    { n: 3, name: 'by-rdc', title: 'Hold by RDC and drill',
      comment: 'Split per warehouse with drill to option level and the rule that created each hold. When a store sells through its first drop, the next run releases the held pieces automatically.',
    },
  ],

  /* ═══ 7 · Pending Allocation ═══════════════════════════════════ */
  pendalc: [
    { n: 1, name: 'overview', title: 'Pending Allocation — Overview',
      comment: 'An approved allocation is a promise; this section tracks every promised piece until delivered or consciously closed. Overview shows how much PEND exists, where, and how old.',
      warn: 'Neglected pendings eat free stock: MSA subtracts them (FNL_Q = stock − pending − hold). Keep this book clean.' },
    { n: 2, name: 'ageing', title: 'Watch the ageing',
      comment: 'Sort by age each morning. Anything older than your service window (say 7 days) needs a reason — chase the DO desk or close it.',
    },
    { n: 3, name: 'do-entry', title: 'Daily DO Entry',
      comment: 'The DO desk records the delivery orders the warehouse actually created against pending quantities — the link between plan and truck.',
    },
    { n: 4, name: 'reco', title: 'Reconciliation',
      comment: 'Match planned vs actually shipped. Complete shipments disappear from pending; partial ships stay with the remainder. Example: 18 planned, 16 shipped (one pak damaged) → 2 remain pending.',
    },
    { n: 5, name: 'adhoc', title: 'Adhoc Close',
      comment: 'For pendings that will never ship (damaged stock, closed store): close them with a reason. The freed quantity returns to FNL_Q at the next MSA run.',
    },
    { n: 6, name: 'ops-log', title: 'Operations Log',
      comment: 'Every DO entry, reconciliation and close is recorded here with user and timestamp — the audit trail for the whole pending lifecycle.',
    },
  ],

  /* ═══ Data Dictionary ══════════════════════════════════════════ */
  dictionary: [
    { n: 1, name: 'open', title: 'When you meet an unknown column',
      comment: 'Data Management → Data Dictionary. Every important column with its full form, plain-words purpose, related tables and the exact formula from the code — 100+ entries covering MSA, Grid, Listing and Allocation.',
    },
    { n: 2, name: 'search', title: 'Search anything',
      comment: 'One search box matches names, meanings, tables and formulas — searching FNL_Q finds the column and every formula that uses it. Filter by module with the pills, and use the pencil to improve any wording.',
    },
  ],
}

export const stepImg = (moduleId, s) => img(moduleId, s.n, s.name)
