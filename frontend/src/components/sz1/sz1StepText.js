import { Layers, Table2 } from 'lucide-react'
import { SZ_SHARE_LADDER, SZ_SHARE_EXAMPLE } from './sz1Help'

// The fixed wording of 1SZ ALC Step 1 (Supply · SZ Base) and Step 2
// (Demand · ST/MJ/SZ Req): titles, the CONT / size-share rule tables and their
// notes. Shared by the Prepare tabs and the Settings page so they never drift.
// Spec: public/docs/manual/sz1_alloc.md §B–C.
export const STEP_TEXT = {
  1: {
    key: 'base', tab: 'supply', tabName: 'Supply', step: 'Step 1', title: 'Generate SZ Base', table: 'ARS_SZ1_MSA_SZ_BASE', unit: 'articles', icon: Layers,
    subtitle: 'Pick the broken-size candidates in each warehouse from ARS_MSA_TOTAL — free stock only, size-applicable categories, any enabled rule R1–R4. Sizes and options with no company CONT are a gap, kept out.',
    rules: [
      { Case: '1 · CO row has the MAJ_CAT + size with CONT > 0', ART_CONT_PCT: 'CO CONT × 100', CONT_SRC: 'CO' },
      { Case: '2 · else: average over every store row of the MAJ_CAT + size > 0', ART_CONT_PCT: 'store average × 100', CONT_SRC: 'STORE_AVG' },
      { Case: '3 · else: the size is in the master but every CONT is 0', ART_CONT_PCT: '0 → gap', CONT_SRC: 'ZERO_IN_MASTER' },
      { Case: '4 · else: the MAJ_CAT + size is not in the master at all', ART_CONT_PCT: '0 → gap', CONT_SRC: 'NOT_IN_MASTER' },
      { Case: 'Option (OPT_CONT_PCT)', ART_CONT_PCT: 'Σ ART_CONT_PCT of its live sizes', CONT_SRC: '—' },
      { Case: 'Gap OPT_NO_CONT', ART_CONT_PCT: 'OPT_CONT_PCT = 0 — no live size of the option has any CONT % in the master', CONT_SRC: '—' },
      { Case: 'Gap SZ_NO_CONT', ART_CONT_PCT: 'this size 0, other sizes of the option > 0', CONT_SRC: '—' },
    ],
    rulesTitle: 'Article CONT % — company curve',
    rulesSummary: 'CO → store average → 0 (gap)',
    rulesNote: 'Master_CONT_SZ holds stores and CO, no RDC rows, so Step 1 (per RDC) uses the company figure: the CO row first; when CO lacks the size, the average of the store rows (e.g. YBW_SHACKET 14-15Y / 16-17Y: not in CO, 525 stores have them → STORE_AVG 10.20 %). A size is a gap only when no row of the master gives it a CONT. Counted on free stock (FNL_Q > 0). A / NA options are exempt from the gap unless set.',
  },
  2: {
    key: 'req', tab: 'demand', tabName: 'Demand', step: 'Step 2', title: 'Generate ST/MJ/SZ Req', table: 'ARS_SZ1_ST_MJ_SZ_REQ_BASE', unit: 'rows', icon: Table2,
    subtitle: 'For every listed store, how short it is per category and size. MJ_SZ_SRT is the size budget no run may exceed.',
    rules: SZ_SHARE_LADDER,
    rulesTitle: 'Size share — Listing Level 2 ladder',
    rulesSummary: 'store → company · missing size = 0',
    rulesNote: 'First match wins. A curve is the real sizes with CONT > 0, scaled to 100 %: A / NA rows are never part of a divisor (before, a store curve with A = 1.0 halved every real size — HC02 · JGW_SKEEVY 2-3Y was 15.30 %, now 30.59 %, and the category’s size budget went from 3 to 12). CONT_PCT_SZ_REV = CONT_PCT_SZ — no 9.99 default any more, so a 0 share gives that size no budget. MJ_MBQ = round(MJ_DISP + MJ_SAL_PD × ALC_D). MJ_SZ_MBQ = 0 when MJ_DISP × share = 0, else round(MJ_DISP × share + MJ_SAL_PD × share × ALC_D), from unrounded values (MJ_SZ_DISP and MJ_SZ_SAL_PD are stored rounded for display only). MJ_SZ_SRT = max(MJ_SZ_MBQ − size stock, 0). A / NA rows take the whole category figures.',
    example: SZ_SHARE_EXAMPLE,
    exampleTitle: 'Worked example — HC02 · JGW_SKEEVY: MJ_DISP 25, MJ_SAL_PD 0.03, ALC_D 11, stock 20 → MJ_MBQ 25, MJ_SRT 5',
  },
}
