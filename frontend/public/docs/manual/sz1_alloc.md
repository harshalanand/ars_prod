# 1SZ ALC · Size Allocation — ARS Manual

*Move broken-size and residual warehouse stock to the stores that are short of exactly
those sizes — in whole packs, never above a store's size budget.*

> **Status (2026-10-12): phase 1 — correct numbers + readiness.** Steps 1–3, Sessions and
> Help are live under **1SZ ALC** in the sidebar (`/sz1-alloc/*`), right after GRT ALC.
> Phase 1 (business decision 2026-10-12): Step 2's size share now follows the Listing
> Level 2 ladder, scaled to 100 % (§C), and every build and run is checked by Readiness
> first (§A). The Streamlit tool's share rule stays available as `share_rule="tool"`, so
> `backend/scripts/sz1_parity.py` still proves the port: Step 1 (7,989 rows), Step 2
> (303,840 rows) and Step 3 under all 4 warehouse modes × 3 priority modes match column
> for column. Next: phases 2–4 (see *Plan* below), first production builds and one
> end-to-end wave signed off by the business head, then retire the tool.
>
> **Source:** the SZ1 Allocation Streamlit tool (`D:\001- WIP PROJECT WITH VERSION\1SZ ALLOC`:
> `msa_base.py`, `req_base.py`, `art_store.py`; tables `SZ1_*` on HOPC866) and the
> artifact *1SZ Allocation — BRD & FSD* (2026-10-09). ARS owns the **`ARS_SZ1_*`** tables,
> so both run side by side without touching each other.

---

## BRD — Why this exists

Standard ARS allocation plans per store × option and does not chase thin size runs.
Options down to one or two live sizes stay in DH24 / DW01 and age while stores are short
of exactly those sizes. Of 39,535 live articles (FNL_Q > 0), 11,185 holding 394,017 pcs
qualified as candidates in the tool's first full build; 358,492 pcs sit in options down to
one or two live sizes.

**Problems today:** broken size runs stay in the warehouse · store need is planned per
MAJ_CAT, not per size · runs are assembled by hand in Excel (BGT template) with no audit
trail · gross STK_QTY overstates what can be sent · no controlled cross-warehouse use.

**Objectives:**

| # | Objective | Metric | Target |
|---|---|---|---|
| O1 | Liquidate broken-size residual stock | % of candidate FNL_Q allocated per cycle | to be agreed with Merchandising |
| O2 | Fill store size gaps | % of MJ_SZ_SRT met by the run | to be agreed |
| O3 | Never over-allocate | lines above size budget, stock, entitlement, or not whole packs | **0** — enforced: a breach rolls the session back |
| O4 | Protect home-warehouse stores | units another RDC's stores take out of the owner's claimed need | **0** — surplus reserve |
| O5 | Cut manual effort | store list → result | < 30 min per wave |
| O6 | Full traceability | sessions with settings + per-line result | 100% — ARS_SZ1_SESSION |

**In scope:** DH24 and DW01; stores with LISTING = 1 in `Master_ALC_INPUT_ST_MASTER`;
Steps 1–3 and the sessions. **Out of scope:** STO / delivery creation in SAP, picking,
maintaining the ARS masters, full-option replenishment (standard ARS), categories with
SZ_APPLICABLE = 'N'.

**Business requirements** (BR-01…BR-12): free stock only (FNL_Q) · each RDC its own pool ·
rules R1–R4, size-applicable only · per-store size need from the store curve → company
curve, scaled to 100 % (no even split since 2026-10-12) · A / NA = a complete option on the whole MAJ_CAT need · any store list up to
2,000 · whole packs, never above size shortage or stock · priority: own sequence / biggest
total shortage / weighted · I_ROD rounds, everyone's round 1 first · every run a session ·
back-to-back waves on remaining stock (Should) · controlled cross-RDC that never takes
the owning RDC's need (Should).

**Open question:** who signs off a session before warehouse dispatch, and in which system.

---

## FSD — How it works

Three steps in order; each reads the table the previous step built. Every Step 3 run is a
session; after it, the balances (BAL_FNL_Q, REM_MJ_SZ_SRT) are re-applied to both base
tables so a remaining-stock wave never re-issues what the first allocated.

| Page | Step | Writes |
|---|---|---|
| Overview (`/sz1-alloc`) | readiness, key numbers, the next step | — |
| Prepare · Supply (`/sz1-alloc/prepare?tab=supply`) | Generate SZ Base (Step 1) | ARS_SZ1_MSA_SZ_BASE (replaced) |
| Prepare · Demand (`?tab=demand`) | Generate ST/MJ/SZ req (Step 2) | ARS_SZ1_ST_MJ_SZ_REQ_BASE (replaced) |
| Prepare · Gap (`?tab=gap`) | CONT gap report | — |
| Plan a run (`/sz1-alloc/run`) | wizard: Stores → Options → Preview (compare, writes nothing) → Run | ARS_SZ1_ST_ART_REQ (appended per session) + ARS_SZ1_SESSION |
| Sessions (`/sz1-alloc/sessions`) | review, export, delete | delete removes one session and re-applies balances |
| Settings (`/sz1-alloc/settings`) | Step 1 selection rules, size-share ladder, run defaults | ARS_SZ1_SETTING |

Old links `/sz1-alloc/base`, `/req`, `/gap` open the matching Prepare tab. Permissions are
unchanged: Overview needs MOD_1SZ_ALC; Prepare and Settings PAGE_1SZ_BASE (the Demand and Gap
tabs PAGE_1SZ_REQ / PAGE_1SZ_GAP); Plan a run PAGE_1SZ_RUN; Sessions PAGE_1SZ_SESSIONS.

### A. Validate first, then write

* **Check first, then create.** Readiness (below) runs before every build and run.
* **Preview never writes** (Steps 1, 2 and 3). Step 3 also checks every store code first:
  *not in store master* · *not listed (LISTING ≠ 1)* · *listed, nothing short* · *ok*. Only
  ok stores run; the others are listed on the session (SKIPPED_JSON).
* A build needs a confirmation tick (`confirm=true`), runs as a background job, and
  replaces the table in **one transaction** (TRUNCATE → INSERT → re-apply balances). A
  failed build leaves the previous build in place.
* **Readiness** (2026-10-12, `sz1_readiness.check(scope)`, `GET /api/v1/sz1/readiness?scope=`
  with scope `build1` / `build2` / `run`). A Readiness panel on the Step 1, Step 2 and Run
  pages shows the result before anything is written. The same check runs again when a
  build or run starts: a **block** refuses it; **warnings** let it go ahead and are stored
  on the build summary / session summary under key `readiness`.

| Check | What it looks at | Level |
|---|---|---|
| Sources | every source table the step reads is present | block |
| Order | Step 2 needs a DONE Step 1; a run needs a Step 2 built after the latest Step 1; no other 1SZ job running | block |
| Freshness | a source table changed after the build that read it — change time = the later of `sys.tables.modify_date` and `sys.dm_db_index_usage_stats.last_user_update` | warn |
| Coverage | listed store × MAJ_CAT with no `ARS_GRID_MJ` row (its budget is 0; about 2,136 rows / 236 stores on 12 Oct 2026) · store × MAJ_CAT with no store and no CO curve (NO_CURVE) · zero-CONT gap articles | warn |
| Sessions | the session whose stock is held in the balances is older than 2 days — it is not in Pend ALC yet | warn |
| Rules | Step 1 settings changed since the Step 1 build | warn |
| I_ROD sources | `ARS_CALC_ST_ART` / `ARS_CALC_ST_MAJ_CAT` empty — I_ROD falls back to 1 | warn |

* The balance-mismatch warning (button: *Re-apply balances*) still shows when the balance
  columns disagree with the recorded allocation.
* **One 1SZ ALC job at a time** (Step 1 build, Step 2 build or a run). Session delete and
  re-apply balances also wait for the slot.
* A run's lines, the allocation-limit checks (§E) on the **stored** rows, the balance push
  and the DONE header commit together. Any breach → rollback, session FAILED with up to 6
  examples per breached check, nothing changed. Cancel → nothing written.

### B. Step 1 — Generate SZ Base → ARS_SZ1_MSA_SZ_BASE

Grain: RDC + article. `sz1_base_service.BASE_CTE` (the tool's SQL).

1. Read `ARS_MSA_TOTAL` rows with **FNL_Q > 0** only (FNL_Q = STK_QTY − PEND_QTY).
2. Aggregate per RDC + MAJ_CAT + GEN_ART_NUMBER + CLR + ARTICLE_NUMBER + SZ: sums of
   STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q; PAK_SZ = largest non-zero, else 1; ALLOC_TYPE
   joined when mixed (FRESH+GRT).
3. Per RDC + option (OPT = MAJ_CAT + GEN_ART_NUMBER + CLR): VAR_COUNT = distinct articles,
   SZ_COUNT = distinct SZ, OPT_FNL = Σ FNL_Q. Per RDC + MAJ_CAT: CAT_FNL = Σ FNL_Q.
4. SZ_APPLICABLE from `ARS_GRID_HIERARCHY` on MAJ_CAT.
5. **Company size contribution.** `Master_CONT_SZ` holds stores and `CO`, no RDC rows, so
   Step 1 (per RDC) uses the company figure of MAJ_CAT + size, first match wins:
   CO row CONT > 0 → × 100 (CONT_SRC = **CO**) · else the average of the store rows' CONT
   (blank = 0) > 0 → × 100 (**STORE_AVG** — CO lacks the size, stores carry it) · else 0:
   the size has rows but every CONT is 0 (**ZERO_IN_MASTER**) or no row at all
   (**NOT_IN_MASTER**). Example: YBW_SHACKET 14-15Y / 16-17Y — not in CO, 525 stores
   have them → 10.20 % each, OPT 20.40 %, no gap.
   OPT_CONT_PCT = Σ ART_CONT_PCT of the option's live sizes in the RDC — how much of the
   size curve the option still covers. (Every CO curve sums to 1.0 on 9 Oct 2026.)
6. Keep when **SZ_APPLICABLE = 'Y'** and any **enabled** rule fires; RULES lists them. Every
   value and switch is set on the Step 1 page (`ARS_SZ1_SETTING`, `sz1_settings.py`) and
   stamped on each build's summary (`rules`); a change applies from the next Generate:

| Rule | Condition | Default |
|---|---|---|
| R1 broken size curve | SZ not in (A, NA) and OPT_CONT_PCT ≤ X % (basis *cont*) — or VAR_COUNT ≤ N (basis *var*, the tool's rule) | X = 40 |
| R2 no-size option | SZ_COUNT = 1, SZ in (A, NA), and OPT_FNL ≤ **OPT_FNL_MAX** when the cap is on | cap on |
| R3 low-stock category | CAT_FNL ≤ X | 200 |
| R4 near-empty option | OPT_FNL ≤ **OPT_FNL_MAX** — the same variable as R2's cap | 20 |

   *Tool rules* preset (R1 VAR_COUNT ≤ 2, R2 no cap, R3 200, R4 20) reproduces the Streamlit
   tool exactly (`sz1_parity.py`). Defaults on 9 Oct 2026: R1 4,772 articles · R2 394 · R3 1,170
   · R4 5,372 → 7,011 articles / 414,332 pcs (tool rules: 7,989 / 713,203).

7. **Zero-CONT gap.** A size gets 0 only when **no row of Master_CONT_SZ (CO or any
   store) gives its MAJ_CAT + size a CONT %**. A size-applicable real-size article with
   OPT_CONT_PCT = 0 (**OPT_NO_CONT** — no live size of the option has any CONT % in the
   master) or ART_CONT_PCT = 0 (**SZ_NO_CONT** — this size has none while other sizes of
   the option do) is written to
   `ARS_SZ1_GAP` with every build (GAP_TYPE, CONT_SRC, RULES_IF_KEPT, EXCLUDED) and, with
   setting GAP_EXCLUDE on (default), kept out of SZ Base. A / NA options are exempt
   (GAP_INCL_FULL_OPT off): their CO size-curve value is 0 by design — 604 of 609 on
   9 Oct 2026, so including them would remove almost every R2 candidate. Report: page
   *CONT Gap Report* (`/sz1-alloc/gap`, `sz1_gap_service.py`). 9 Oct 2026 (store-average
   fallback): 267 OPT_NO_CONT + 41 SZ_NO_CONT articles.
   **Validated against the complete MSA.** Each build stores `msa_recon` (and the report
   works it out live for an older build): all ARS_MSA_TOTAL articles → minus not free
   (FNL_Q ≤ 0, not considered) → free stock split into not size-applicable / has CONT /
   OPT_NO_CONT / SZ_NO_CONT / A-NA exempt, which must add back to the free stock. 9 Oct 2026:
   131,515 articles, FNL 6,625,674; free 21,352 = 1,629 + 18,896 + 267 + 41 + 519.
8. BAL_FNL_Q = FNL_Q, then the latest session (chain) is re-applied.

*Worked example:* M_BRIEF 1114094526 / A_MIX at DH24 has six sizes but only 100 and 105
carry free stock (size 95: 370 stock, 329 pending → 0 free), so VAR_COUNT = 2 and R1 fires.
On gross STK_QTY it would have had three live sizes and failed.

### C. Step 2 — Generate ST/MJ/SZ req → ARS_SZ1_ST_MJ_SZ_REQ_BASE

Grain: RDC + store + MAJ_CAT + SZ. `sz1_base_service.req_sql` (the tool's SQL with the
ARS size share below; `share_rule="tool"` gives the tool's share for `sz1_parity.py`).

* Stores: `Master_ALC_INPUT_ST_MASTER` with LISTING = 1, with their home RDC.
* MAJ_CAT + SZ pairs: the distinct pairs Step 1 kept **in any RDC** (2026-10-09), so under
  All RDC a store can be served a size only the other warehouse holds. The tool used only
  the store's own RDC's pairs; Own RDC runs simply find no articles for the other pairs.
* SEG / DIV / SUB_DIV / SSN from `ARS_MSA_TOTAL` (one value per MAJ_CAT);
  SZ_COUNT = distinct real sizes (not A / NA) per RDC + MAJ_CAT in `ARS_MSA_TOTAL`.
* From `ARS_GRID_MJ` (WERKS + MAJ_CAT): MJ_STK, MJ_DISP, MJ_SAL_PD, ALC_D, ACS_D.
  MJ_SZ_STK = Σ `ARS_GRID_MJ_VAR_ART`.STK_TTL for the size; A / NA rows take MJ_STK.
* **Size share CONT_PCT_SZ — Listing Level 2 ladder, scaled to 100 %** (2026-10-12;
  `Master_CONT_SZ`, duplicate rows grouped by MAX). A store's *curve* = its real sizes
  (not A / NA) with CONT > 0 for the MAJ_CAT. A / NA rows are never part of a divisor.
  First match wins:

| # | Condition | Value | CONT_SRC |
|---|---|---|---|
| 1 | SZ is A / NA | 100 | FULL_OPT |
| 2 | SZ_APPLICABLE = 'N' | 100 ÷ SZ_COUNT | EVEN_N |
| 3 | store has a curve and has this size | store CONT ÷ store real-size total × 100 | STORE |
| 4 | store has a curve but not this size (or it is 0) | 0 — no budget | STORE_SIZE_MISSING |
| 5 | store has no curve; CO has the size | CO CONT ÷ CO real-size total × 100 | CO |
| 6 | store has no curve; CO curve lacks the size | 0 | CO_SIZE_MISSING |
| 7 | neither store nor CO curve | 0 | NO_CURVE |

  The shares of one store × MAJ_CAT's real sizes add to 100 % (or 0). There is no even
  split for a missing curve and no CO top-up for a size the store curve lacks — Listing's
  Level 2 ladder (Stage B), with each curve scaled to 100 %.
* CONT_PCT_SZ_REV = CONT_PCT_SZ. No 9.99 default: a 0 share gives no budget. The column
  is kept for the BGT template.

```
MJ_MBQ       = round(MJ_DISP + MJ_SAL_PD × ALC_D)        (0 when MJ_DISP = 0; blanks = 0)
MJ_SRT       = max(MJ_MBQ − MJ_STK, 0)        MJ_EXC = max(MJ_STK − MJ_MBQ, 0)
MJ_STR       = MJ_DISP / MJ_SAL_PD
share        = CONT_PCT_SZ / 100
MJ_SZ_DISP   = MJ_DISP × share                MJ_SZ_SAL_PD = MJ_SAL_PD × share
               (both stored rounded, for display only)
MJ_SZ_MBQ    = 0 when MJ_DISP × share = 0, else
               round(MJ_DISP × share + MJ_SAL_PD × share × ALC_D)   from unrounded values
MJ_SZ_SRT    = max(MJ_SZ_MBQ − MJ_SZ_STK, 0)  MJ_SZ_EXC = max(MJ_SZ_STK − MJ_SZ_MBQ, 0)
REM_MJ_SZ_SRT = MJ_SZ_SRT, then less the latest session (chain)
```

*Worked example (live, 12 Oct 2026):* HC02 (Ambikapur, DH24) · JGW_SKEEVY. Store curve
2-3Y 0.3059, 4-5Y 0.3882, 6-7Y 0.3059, A 1.0. Grid: MJ_DISP 25, MJ_SAL_PD 0.03, ALC_D 11,
MJ_STK 20 → MJ_MBQ round(25.33) = 25, MJ_SRT 5.

| SZ | Old share (÷ 2.0 incl. A) | New share (STORE) | MJ_SZ_MBQ | Size stock | MJ_SZ_SRT new (old) |
|---|---|---|---|---|---|
| 2-3Y | 15.30 % | 30.59 % | 8 | 3 | **5** (1) |
| 4-5Y | 19.41 % | 38.82 % | 10 | 3 | **7** (2) |
| 6-7Y | 15.30 % | 30.59 % | 8 | 12 | **0** (0) |
| A | 100 % | 100 % (FULL_OPT) | 25 | 20 (whole category) | **5** |

The old rule divided by the curve total 2.0, which included A = 1.0, and so halved every
real size.

### D. Step 3 — Store × article run → ARS_SZ1_ST_ART_REQ

Grain: session + store + article + source RDC. `sz1_run_service.SELECT_SQL` +
`sz1_alloc_engine` (the tool's SQL and engine).

**Run inputs**

| Input | Options | Default |
|---|---|---|
| Stores | pasted (any separator) or a column of an uploaded xlsx / csv / txt; ≤ 2,000 | — |
| Store priority | Balanced (weight w on MJ_SRT rank) · Biggest total shortage · Own sequence | Balanced, w = 0.5 |
| Stock basis | Full stock (FNL_Q, MJ_SZ_SRT) · Remaining stock (BAL_FNL_Q, REM_MJ_SZ_SRT) | Full stock |
| Warehouse | **All RDC** (pool, allocate, split) · **Own RDC** | **All RDC** |
| All RDC: Protect other RDC's stores | another RDC's stock moves only from its SURPLUS_QTY | **on** |
| All RDC: One source per line | fill a store's line from one warehouse; split only when it can't cover it | **on** |
| Round scaling | fixed: only the article target grows per round | — |

**Candidate lines.** Each ok store's Step 2 rows with size budget > 0 joined to Step 1
articles on MAJ_CAT + SZ: every RDC under *All RDC*, the home RDC under *Own RDC*. (The
tool's *Home first / Pooled / Fallback* are still accepted by the API and engine, for
parity, but not offered.)

**All RDC — pool, allocate, split** (`sz1_alloc_engine._allocate_pool`, default):
1. Per article, every RDC's free stock is one pool; one pack size per article (the
   largest), and ART_SRT / ART_SRT_ROD are re-derived on it.
2. The unit of allocation is the store × article: stores of every RDC compete for the
   pool in ST_SEQ order, with the same rounds, order and limits as below.
3. A pack never ships half from two warehouses. Where it comes from:
   * **Protect other RDC's stores** (default on): a store may draw another RDC's stock only
     up to that RDC's SURPLUS_QTY (its stock less OWN_NEED, what its own listed short
     stores claim — *Warehouse reserve* below); home stock is not limited. Off: OWN_NEED = 0,
     SURPLUS_QTY = the stock, the pool is shared purely on priority.
   * **One source per line** (default on): a line's first pack goes to the home RDC if it
     can cover the line's whole remaining need (stock less what other lines already drawing
     on it still expect), else to an RDC that can, else home, else the RDC with the most; the
     line then stays on that RDC while it holds a whole pack. Off: each pack comes from the
     home RDC while it holds a whole pack, else from the RDC holding the most.
4. A line served from two RDCs is **split**: one row per source RDC (SRC_RDC, IS_HOME),
   each a whole number of packs; UNMET_QTY is the store-level figure on both.
5. Limit checks 4 and 5 (surplus) hold the protected pool to SURPLUS_QTY on the stored rows.

*Example:* article A, DH24 4 + DW01 3, pack 1; S1 (home DW01, entitled 5) before S2 (home
DH24, entitled 3) → S1 DW01 3 + DH24 1 (split), S2 DH24 3; the pool of 7 is used. Under
Own RDC S1 could take only DW01's 3. Live, 12 Oct 2026, 120 stores (60 per RDC), Balanced,
in memory: Own RDC 44,213 units · All RDC unprotected 55,172 (41 split lines, 26,717 from the
other RDC) · protected 51,240 (19 split, 8,804) · protected + one source 51,240 (**3 split**,
8,817); no limit breached. ART_FNL_Q = FNL_Q (or BAL_FNL_Q); ART_STK = the store's stock of the
article (`ARS_GRID_MJ_VAR_ART`).

```
ART_DISP    = ACS_D × REV / 100
ART_SAL_PD  = ART_DISP / MJ_STR                       (MJ_STR = 90 when blank or ≤ 0)
ART_MBQ     = round(ART_DISP + ART_SAL_PD × ALC_D), at least 1 when ART_DISP > 0
ART_SRT     = packround(ART_MBQ − ART_STK)
ART_SRT_ROD = packround(I_ROD × ART_MBQ − ART_STK)    a line is admitted when > 0
packround(q) = round(q / PAK_SZ) × PAK_SZ, halves up; 0 when q ≤ 0
```

**I_ROD:** store + option from `ARS_CALC_ST_ART`, else store + MAJ_CAT from
`ARS_CALC_ST_MAJ_CAT`, else 1 (0 counts as missing); CLR or SZ in (A, A_MIX) → at least 2
(a higher maintained value is kept); capped at 12.

**Warehouse reserve (All RDC protected, Own RDC, legacy modes):** per RDC + article, OWN_NEED = Σ ART_SRT of **all** that RDC's listed
short stores (not only this run's), each store's article needs scaled down to fit its
MJ_SZ_SRT; rounded. SURPLUS_QTY = max(stock − OWN_NEED, 0). Only the surplus can go to
another RDC's stores; home rows ignore it.

**Compare before running** (`POST /sz1/run/simulate`, `sz1_run_service.simulate`, writes
nothing): the same stores and settings allocated in memory under up to 4 warehouse variants
(default Own RDC and All RDC protected + one source), with units, lines, stores served, split
lines, units from the other RDC and size need met (units ÷ the stores' size shortage), plus
units per store per variant. Reads the database once (the All RDC frame; Own RDC is its home
rows) and the reserve once; about 40 s read + 20 s per variant for 120 stores.

**Priority ST_SEQ** (inside each MAJ_CAT + SZ): Balanced = w × rank(MJ_SRT) + (1 − w) ×
rank(MJ_SZ_SRT), lowest first, ties by store code · Biggest total shortage = MJ_SRT desc,
ties by MJ_SZ_SRT · Own sequence = the given order, unlisted stores last.

**Allocation, pack by pack** (`sz1_alloc_engine.allocate`):

1. Real sizes run all their rounds before any full option (A / NA).
2. Rounds 1 … max I_ROD; a line takes part in round r when its I_ROD ≥ r — every store gets
   round 1 before any store gets round 2.
3. Within a round, articles go most warehouse stock first (home rows first under Home first).
4. For each article, stores are visited in ST_SEQ order, one pack each, cycling until a full
   pass issues nothing. A pack is issued only when **all** hold:
   * a warehouse holds ≥ 1 whole pack — All RDC: any RDC of the pool, Own RDC: the home
     RDC (else stop this article for the round);
   * legacy cross modes, other-RDC store: surplus left ≥ 1 pack;
   * store's article total + pack ≤ packround(r × ART_MBQ − ART_STK);
   * store's size budget MJ_SZ_SRT left ≥ 1 whole pack;
   * full option only: the store's MJ_SRT left (after its real sizes) ≥ 1 pack.
5. Per line: ALLOC_QTY, PACKS, ALC_I_ROD (last round that shipped), UNMET_QTY =
   ART_SRT_ROD − all the store got of the article (same on both rows of a cross-RDC
   article), BAL_ART_FNL_Q, REM_MJ_SZ_SRT.

*Worked example (FSD §8.5):* DH24, M_TSHIRT; HB05 short 15 in M and 8 in L, HB06 short 7 in
M; I_ROD 2 for HB05 on P1. Result: HB05 P1-M 9, P3-M 6, P1-L 8; HB06 P1-M 3, P3-M 3.
BAL_FNL_Q P1-M 36 → 24, P3-M 12 → 3, P1-L 20 → 12; REM_MJ_SZ_SRT HB05 M 0, HB05 L 0,
HB06 M 1 (a 3-pack cannot fill 1 pc).

### E. Allocation limits checked on every session (zero breaches)

Run on the stored rows inside the write transaction (`sz1_run_service.check_stored`):

1. size budget MJ_SZ_SRT never exceeded (store + MAJ_CAT + SZ);
2. no store + article above ART_SRT_ROD;
3. no warehouse oversold (SRC_RDC + article ≤ ART_FNL_Q);
4. other-RDC stores took ≤ SURPLUS_QTY;
5. no other-RDC store served from zero surplus;
6. every allocation a whole number of packs;
7. no negative allocation;
8. nothing allocated without an entitlement;
9. full options keep the store's MAJ_CAT total within MJ_SRT;
10. the stored rows match the run (row count, units).

### F. Sessions

* SESSION_ID = run time **DDMMYYHHMM**; a second run in the same minute gets
  DDMMYYHHMMSS (the tool replaced the earlier run instead).
* The header (ARS_SZ1_SESSION) keeps every setting, the store list (and sequence), skipped
  codes, the Step 1 / Step 2 build ids it read, totals, checks, a summary (per store, by
  round, by I_ROD, by warehouse, top categories, real sizes vs full options), timings.
* **Lifecycle** (STATUS): RUNNING → **PARKED** (every finished run; legacy 'DONE' reads as
  PARKED) → **APPROVED** (posted to Pend ALC) or **DISCARDED**; FAILED / CANCELLED hold
  nothing. A reverted approval returns to PARKED. Approved sessions cannot be deleted.
* **Any number of parked sessions;** each holds what it allocated until approved or
  discarded. **Live netting** (no Full / Remaining choice — every run is "live"):
  * stock a run may use (BAL_FNL_Q) = max(live ARS_MSA_TOTAL FNL_Q of the RDC × article —
    already net of every approved pending, listing and 1SZ — − Σ parked ALLOC_QTY, 0);
  * store room (REM_MJ_SZ_SRT) = max(MJ_SZ_MBQ − the store's stock **now** in the grid
    (ARS_GRID_MJ_VAR_ART.STK_TTL by store × MAJ_CAT × SZ; A / NA rows the category's
    ARS_GRID_MJ.STK_TTL) − Σ parked ALLOC_QTY, 0); full-option cap = max(MJ_MBQ − category
    stock now − Σ parked, 0).
  The grid's STK_TTL **includes** PEND_ALC (store SLOCs + hub + pending), and Pend ALC adds
  every approval to it at once, so pending to a store — and lines DO has closed that the
  next grid build has not yet seen in the store's own stock — is never given twice. Step 2
  therefore does not subtract pending again; MJ_PEND / MJ_SZ_PEND keep the open pending at
  build time for information. `push_balances` writes these figures (every run refreshes
  them first; simulate / preview compute them in the SELECT and write nothing).
* **Approve** (`sz1_approve_service`, permission **ACT_1SZ_APPROVE**): re-check first (read
  only, `GET /sz1/sessions/{id}/recheck`): blocks when the session is not parked, its checks
  failed, any RDC × article needs more than live MSA free stock now, or any store × MAJ_CAT ×
  size needs more than its room now (store stock now and the other parked sessions);
  warns on an older Step 1 / 2 build or a session older than 2 days. Then one transaction:
  ARS_PEND_ALC rows (SESSION_ID `1SZ-<id>`, SOURCE / ALLOC_MODE `1SZ`, SRC_RDC = the line's
  ship-from SRC_RDC, RDC = the store's own warehouse from the store master, idempotency guard
  on SRC_RDC, ALLOC_TYPE from Step 1 — a mixed article split by live FRESH free stock, FRESH
  first), `apply_pend_alc_delta(+1)` to MSA (by SRC_RDC) and the grid, operation `APPROVE_1SZ`, session →
  APPROVED (APPROVED_BY / AT, PEND_SESSION_ID), balances. Any failure rolls all back.
  From then on Pend ALC manages the rows (BDC, DO, reco, adhoc close).
* **Re-fit** (blocked approve): discard the session and re-run the same stores and options
  on what is left now (REFIT_OF on the new one). **Remove stores** from a parked session
  (lines deleted, totals and summary recomputed). **Discard** a parked session (lines kept
  for audit). **Revert** an approval only while no row has BDC or DO — from the session or
  the Pend ALC operations log; −1 delta, rows deleted, session PARKED again.
* Readiness warns for each session parked longer than 2 days (listing cannot see parked
  stock) and counts sessions approved since the latest build.
* Sessions page: Parked and Approved lists (Pend ALC progress: BDC sent, DO received, closed;
  "Shipped" when all rows closed), Closed list; per session the timeline, checks, totals,
  per-store fill, rounds, warehouses, categories, every line, CSV export, and the actions
  above.

### G. Tables

| Table | Grain | Rebuilt |
|---|---|---|
| ARS_SZ1_MSA_SZ_BASE | RDC × article | replaced per Step 1 build |
| ARS_SZ1_ST_MJ_SZ_REQ_BASE | RDC × store × MAJ_CAT × SZ | replaced per Step 2 build |
| ARS_SZ1_ST_ART_REQ | session × store × article × source RDC | appended per session |
| ARS_SZ1_SESSION | session | one row per run; STATUS lifecycle, APPROVED_BY/AT, PEND_SESSION_ID, CLOSED_BY/AT, REFIT_OF |
| ARS_SZ1_BUILD | build | one row per Step 1 / 2 build |

Schema: `backend/scripts/039_sz1_module.sql` (self-heals via `sz1_schema.ensure_tables`).
Sources (read only): ARS_MSA_TOTAL, ARS_GRID_HIERARCHY, Master_ALC_INPUT_ST_MASTER,
ARS_GRID_MJ, ARS_GRID_MJ_VAR_ART, Master_CONT_SZ, ARS_CALC_ST_MAJ_CAT, ARS_CALC_ST_ART.

### H. Code map

| File | Role |
|---|---|
| `backend/app/services/sz1_schema.py` | table names, self-heal, row counts |
| `backend/app/services/sz1_base_service.py` | Steps 1–2 SQL, previews, build jobs, job slot, balances |
| `backend/app/services/sz1_alloc_engine.py` | priority + pack-by-pack engine (pure pandas) |
| `backend/app/services/sz1_run_service.py` | Step 3 SQL, reserve, run job, checks, summaries, sessions |
| `backend/app/services/sz1_readiness.py` | Readiness `check(scope)` for build1 / build2 / run (§A) |
| `backend/app/services/sz1_approve_service.py` | re-check, approve → Pend ALC, revert, discard, re-fit, remove stores, Pend ALC progress (§F) |
| `backend/app/api/v1/endpoints/sz1.py` | `/api/v1/sz1/*` |
| `backend/scripts/sz1_parity.py` | proof against the Streamlit tool (read only) |
| `frontend/src/pages/sz1/*`, `components/sz1/Sz1Ui.jsx` | pages; `sz1API` in `services/api.js` |

Permissions: module `MOD_1SZ_ALC`; pages `PAGE_1SZ_BASE`, `PAGE_1SZ_REQ`, `PAGE_1SZ_RUN`,
`PAGE_1SZ_SESSIONS`, `PAGE_1SZ_HELP`; action `ACT_1SZ_APPROVE` (approve / revert; seeded to
SUPER_ADMIN, tick it for other roles in Settings → Roles).

### I. UAT

* Step 1 counts by rule and RDC reconcile to a manual query on the same day's ARS_MSA_TOTAL;
  M_BRIEF 1114094526 at DH24 qualifies under R1 with VAR_COUNT = 2.
* Step 2: 10 sampled store × MAJ_CAT × SZ rows match a hand calculation under the §C ladder,
  with the right CONT_SRC — at least one each of STORE, STORE_SIZE_MISSING, CO,
  CO_SIZE_MISSING, NO_CURVE and FULL_OPT; HC02 · JGW_SKEEVY gives 30.59 / 38.82 / 30.59 %
  and MJ_SZ_SRT 5 / 7 / 0; no store × MAJ_CAT's real-size shares add to more than 100 %.
* Readiness: each scope shows its blocks and warnings before a build / run; a block refuses
  the build or run; the warnings appear on the build or session summary (`readiness`).
* Step 3: a 20-store run keeps all limits under each warehouse mode; one store × size
  hand-checked against §D including an I_ROD > 1 article; a remaining-stock wave after a
  full-stock wave never re-issues units the first allocated.
* Park & approve: two parked sessions on the same article never hold the same pieces; a
  blocked approve shows the shortfall and Re-fit; approving a small session writes
  ARS_PEND_ALC `1SZ-<id>` rows that match its lines (SRC_RDC = the line's SRC_RDC, RDC = the
  store's own warehouse), MSA FNL_Q drops by the same units at the SRC_RDC,
  BDC preview lists them; revert before BDC restores MSA, deletes the rows and parks it again.
* Business head signs off one end-to-end wave before go-live.

### J. Plan

All three blueprint phases (https://claude.ai/artifact/3vxC4XZXikzkiAuTu3WfQc) are built:
phase 1 §A Readiness + §C size share; phase 2 §D warehouse options + the UI; phase 3 §F
park & approve into Pend ALC. Open: the first real approval on PROD (a small session,
approved then reverted) as the end-to-end proof; master clean-up of Master_CONT_SZ
(A = 1.0 rows, overlapping sizes).

---

## Recorded rules

* **2026-10-09 — Port proven.** `sz1_parity.py` on HOPC866: Step 1 7,989 rows / 22 columns,
  Step 2 303,840 rows / 26 columns, Step 3 candidates (30 columns) and allocation (ST_SEQ,
  ALLOC_QTY, ALC_I_ROD, PACKS, UNMET_QTY, BAL_ART_FNL_Q, REM_MJ_SZ_SRT) identical for 120
  stores × {same, home_first, pooled, fallback} × {weighted, mj_srt, sequence}.
* **Remaining-stock runs also use the remaining size budget.** The tool's balance run read
  BAL_FNL_Q but the full MJ_SZ_SRT, so a store filled in wave 1 could be filled again in
  wave 2. ARS uses REM_MJ_SZ_SRT as the size budget (and for the store check and the
  reserve) and nets the chain's allocation off MJ_SRT for full options, per FSD §8.5
  ("a second wave starts from these balances"). Full-stock runs are identical to the tool.
* **I_ROD override joined as text.** ARS_CALC_ST_ART.GEN_ART_NUMBER is BIGINT; the tool's
  implicit conversion would fail the whole run on one non-numeric article. Same matches on
  real data (parity above).
* **A breach rolls the session back.** The tool wrote the session regardless; ARS checks the
  stored rows before commit and keeps nothing if any limit fails.
* **Base tables are permanent;** builds replace rows in one transaction instead of DROP +
  CREATE, and stamp BUILD_ID. A session records the build ids it read.
* **Round scaling is fixed** ("none"): only the article target grows per round; MJ_SZ_SRT and
  MJ_SRT stay at one round. The engine still accepts size / both for parity.
* **Half-pack budgets are off:** a pack is issued only when the whole pack fits the size budget.
* **2026-10-09 — All RDC pool is the default warehouse mode.** Business ask: "club all RDC
  into one, then allocate, and split in case the allocation is done from multiple RDCs";
  the run offers only All RDC (default) and Own RDC. Pool → allocate → split by source,
  own RDC first. Step 2 now builds every store's need for the pairs of all RDCs. Proven:
  synthetic two-RDC case exact; 150 live stores, all limits held. The tool's other three
  modes remain proven by `sz1_parity.py` (Step 2 compared on the tool's own-RDC pairs).
* **2026-10-09 — Step 1 uses company size contribution; all rule values editable.** Business
  decision: the ARS listing size-CONT ladder (Stage B — store row, CO only when the store's
  whole curve is empty, a missing size stays 0, raw values) is final; Step 1 has no store, so
  it applies the ladder to the CO curve. R1 moved from "≤ 2 live sizes" to "option CONT ≤ X %"
  (the old rule stays as basis *var*); R2 gained a qty cap that shares R4's variable
  (OPT_FNL_MAX); every rule can be switched off. Steps 2 and 3 are unchanged.
* **2026-10-09 — Zero-CONT gap.** Business rule: a size or a whole option with 0 company CONT
  is recorded as a gap and not considered (§B.7). A / NA options exempt by default (setting).
  Tool preset has the gap exclusion off, so parity still holds.
* **2026-10-09 — UI: Step 1 / 2 rearranged.** Build controls and the settings labels sit in
  one row; Selection rules and the CONT rule open as popups from those labels; Browse has
  RDC / MAJ_CAT / article / size / rule / CONT-source filters plus per-column filters in the
  table (text contains; numbers > < = and ranges); Build history has *Clear history*
  (keeps the latest DONE build and anything running).
* **2026-10-09 — UI: collapse / expand.** Every titled panel folds to its header; the page
  header has Collapse all / Expand all; the fold state is remembered per page in the browser.
* **2026-10-09 — Export of the built tables.** Steps 1 and 2 → Browse → *Export all CSV* /
  *Export filtered CSV* (`GET /sz1/export/{step}?rdc&maj_cat&store`): every column, every
  row (or the filtered ones), streamed. Measured: Step 2 complete = 475,197 rows, 96 MB, 34 s.
* **2026-10-09 — "Running" is read from the database, not process memory.** Several backend
  processes share HOPC866 (main API, DEV, test APIs); one had marked another's live build
  as orphaned. A job counts as running in any process while its RUNNING row is younger than
  30 min (build) / 120 min (run); only older RUNNING rows are marked FAILED, and the one-job
  slot checks those rows too. A finished build clears any stale ERROR.
* **2026-10-09 — UI:** module design kit `components/sz1/Sz1Ui.jsx` (hero, step rail,
  panels, option tiles, sticky action bar, flowcharts); every control, column header and
  flowchart box explains itself on hover (`components/sz1/sz1Help.js`); Help is step by
  step with flowcharts (whole process, each step, pool & split, per-pack decision).
* **2026-10-09 — CONT % from the whole master; gap = no CONT anywhere.** Step 1's company
  CONT falls back from the CO row to the store average when CO lacks the size (CONT_SRC
  STORE_AVG); OPT_NO_CONT / SZ_NO_CONT now mean no CONT % for the MAJ_CAT + size in any
  row of Master_CONT_SZ. Was CO-only, which flagged YBW_SHACKET|1131103527|OLV_GRN as
  OPT_NO_CONT although 525 stores carry its sizes. Gap report reconciles to the complete
  MSA (panel *Gap vs complete MSA stock*); categories list distinct gap sizes beside the
  sizes the master does carry (MASTER_SIZES). Regenerate Step 1, then Step 2, to apply.
* **2026-10-09 — Column filters search the whole table.** Browse (Steps 1 / 2) and Gap
  lines filter on the server (`cf` JSON, `sz1_filter.py`): a funnel on every header lists
  the column's distinct values with counts from the whole table (under the other filters),
  plus search and a condition (>10, <=5, 10-20, contains). Export follows the same filters.
  CONT % columns show as a percentage with 2 decimals.
* **2026-10-09 — Every table sorts, filters and exports.** Click a header: ascending →
  descending → off (also in the filter menu). Browse and Gap lines sort on the server
  (`ob`/`od`, column checked against the table) so the order covers the whole table, and
  their Export streams every matching row in that order; other tables export what they show.
  One-line toolbar per table: row count · sort/filter chips · Clear filters · Export.
* **2026-10-12 — Step 2 size share = Listing Level 2 ladder, scaled to 100 %.** Business
  decision (phase 1, "all recommended"). Store curve (real sizes, CONT > 0) → STORE; a size
  the store curve lacks → 0 (STORE_SIZE_MISSING); no store curve → CO curve (CO) or 0
  (CO_SIZE_MISSING); neither → 0 (NO_CURVE). A / NA never count in a divisor. The even
  splits (EVEN_NO_CURVE, EVEN_CO_MISSING), the CO top-up (CO_SIZE / STORE_MISSING) and the
  9.99 default for a 0 share are gone; CONT_PCT_SZ_REV = CONT_PCT_SZ. MJ_SZ_MBQ is worked
  from unrounded values. Why: store curves that carry A / NA > 0 beside real sizes (6,225 in size-applicable categories, 7,557 in all; corrected 12 Oct from an overstated 17,175) put A into the divisor, which
  halved every real size; 12,504 store curves have overlapping sizes (e.g. 14-15Y next to
  14Y / 15Y) and add to ~120 %. Scaling fixes the math; the master should still be
  cleaned. Measured read only on 12 Oct 2026, both rules on the same live inputs (614,250
  rows): total size budget 3,743,480 → 3,806,252 pcs (+62,772); rows with a budget 165,116 →
  168,847; 5,805 rows change (5,591 up; 214 down by 1 pc from no longer rounding part-way);
  store × MAJ_CAT pairs whose shares added to more than 100 %: 79 → 0. Example: HC02 · JGW_SKEEVY 15.30 / 19.41 / 15.30 % →
  30.59 / 38.82 / 30.59 %, MJ_SZ_SRT 1 / 2 / 0 → 5 / 7 / 0. The tool's rule stays as
  `share_rule="tool"` in `sz1_base_service.req_sql`, used only by `sz1_parity.py`.
  Regenerate Step 2 to apply.
* **2026-10-12 — Readiness: check first, then create.** New `sz1_readiness.check(scope)`
  (build1 / build2 / run), `GET /api/v1/sz1/readiness?scope=`, and a Readiness panel on the
  Step 1, Step 2 and Run pages. Blocks (missing source, wrong order, another 1SZ job)
  refuse the build or run; warnings (stale source, coverage gaps, an old session still
  holding stock, Step 1 settings changed, empty I_ROD tables) are stored on the build /
  session summary under `readiness`. Why: builds and runs could start on stale or partial
  inputs without telling anyone — e.g. about 2,136 listed store × MAJ_CAT rows (236 stores)
  had no grid row and so a 0 budget on 12 Oct 2026. Rules in §A.
* **2026-10-12 — All RDC: protect the other RDC's stores, one source per line (phase 2).**
  Two run options, both on by default and kept on the session (`STORE_LIST.options`,
  `summary.options`): another RDC's stock moves only from its surplus over its own listed
  stores' need (reserve), and a store's line is filled from one warehouse where one can cover
  it. Why: a 150-store DH24 run had taken 18,627 pcs DW01's own stores needed, and 82 lines
  were picked in two warehouses. Live 120 stores: unprotected 55,172 units / 41 splits →
  protected + one source 51,240 / 3 splits; Own RDC 44,213. New `POST /sz1/run/simulate`
  compares the modes before a run. No table change (options live in the JSON columns).
* **2026-10-12 — UI rebuilt to the blueprint (phase 2).** Sidebar Overview · Prepare · Plan a run
  · Sessions · Settings · Help. Overview shows readiness and one "next step"; Prepare holds
  Supply / Demand / Gap as tabs; Plan a run is a 4-step wizard whose Preview compares Own RDC
  and All RDC in memory; Settings holds the Step 1 rules as a page. The Readiness panel opens by
  itself only on a block; passed checks fold into a count.
* **2026-10-12 — Park many sessions; approve into Pend ALC (phase 3).** Every run parks and
  holds its stock and size budget; runs net live MSA free stock and the grid's live store
  stock less all parked sessions (no Full / Remaining choice). Approve re-checks live stock
  and room, then writes ARS_PEND_ALC (`1SZ-<id>`, SOURCE / ALLOC_MODE 1SZ, RDC = ship-from),
  applies the MSA + grid delta and logs APPROVE_1SZ in one transaction; revert (before BDC /
  DO) returns it to PARKED; Re-fit, Remove stores, Discard. **Correction during build:** the
  first draft also subtracted open pending in Step 2, but the grid's STK_TTL already includes
  PEND_ALC (83,384 of 93,370 rows with pending match SLOCs + hub + PEND_ALC exactly) and Pend
  ALC adds approvals to it live — subtracting again would have halved needs (HC06 · M_BRIEF
  293 → 87). Room is therefore MBQ − live grid stock − parked. Live on 12 Oct: session
  0910261150 (legacy DONE, 3 days) re-checks as blocked — 2,017 RDC × articles short of free
  stock, 397 store × size rows over room — so it must be re-fitted or discarded.
* **2026-10-12 — Hardening after phase 3.** The run API (start, preview, simulate) accepts only
  `cross_rdc` all / same; the tool's home_first / pooled / fallback remain for
  `sz1_parity.py` only (parity re-run: Step 2 and Step 3 MATCH on 20 stores). Deleting a
  session re-checks STATUS under the same row lock approve takes and deletes only when it is
  not APPROVED, so a delete can never remove an approval's lines. Reverting an APPROVE_1SZ
  from the Pend ALC Operations Log also needs ACT_1SZ_APPROVE.
* **2026-10-12 — UI follow-ups.** "Open in Pend ALC" opens `/pend-alc/reco?session=1SZ-<id>`
  (the Reco page now reads `?session=`, `?source=` → Mode filter, and `?closed=`). The Prepare
  menu item and route open for any of PAGE_1SZ_BASE / REQ / GAP (nav item `perms`, any-of);
  each tab keeps its own code. Confirm dialogs sit above 1SZ popups (z-order: tooltips / toasts
  > confirm > filter popover > 1SZ Modal) and Esc closes only the confirm.
* **2026-10-12 — Master data quality report** (Prepare → Gap → *Master data quality*,
  `sz1_quality_service.py`, `GET /sz1/quality` + `/{key}/rows|distinct|export`, read only,
  cached 5 min). Live 12 Oct 13:15: **A** 6,225 size-applicable store curves with A / NA > 0
  beside real sizes · **B** 12,600 store curves whose real sizes don't total 100 % (12,504 over,
  8,336 with overlapping sizes such as 14-15Y beside 14Y / 15Y, typically 120.4 %) · **C** 0
  CO problems in size-applicable categories · **D** 0 duplicate keys · **E** 2,777 listed store
  × category pairs (303 stores, 38 categories; DW01 2,711) with no ARS_GRID_MJ row → budget 0 ·
  **F** 0 pairs with no curve at all (972 fall back to CO). **E diagnosis:** not lost data — the
  grid only has a store × category row when the store's own RDC carries the category in MSA or
  the store holds stock; All RDC offers every Step 1 category to every listed store, so mostly
  DW01 stores meet DH24-only categories and get 0 budget (e.g. JGW_THRM_W at 224 DW01 stores).
  **Decided 12 Oct:** grid rows at company level — Grid Builder Business Rule GRID_ROW_SCOPE =
  COMPANY (see grid.md), effective from the next Run All. The rows then exist but stay at
  budget 0 until the store × category has DISP_Q in Master_ALC_INPUT_ST_MAJ_CAT (only 153 of the
  2,777 pairs have a display today).
* **2026-10-12 — Readiness review.** Clicking Readiness (title, chip or *Review*) opens
  "Readiness review — <scope>": Blocks / Warnings / Info / Passed filter chips, every check
  with a what-to-do hint, and *View list* for checks with rows behind them — sources,
  freshness (one shared verdict helper with the check), rules at build vs now, cov_grid
  (store × MAJ_CAT with no grid row, where 1SZ holds the stock, GRID_NOW for the run scope),
  cov_curve, cov_gap, parked / approved sessions, balance_drift. Lists match the check's
  count; first 5,000 rows on screen, full CSV from the server.
  `GET /sz1/readiness/{scope}/items/{key}/rows|export`; items carry `has_rows`.
* **2026-10-14 — Approve writes SRC_RDC = ship-from, RDC = the store's warehouse.** Pend ALC's
  ledgers now carry both: `RDC` = the destination store's own warehouse (store master,
  descriptive only), `SRC_RDC` = the warehouse the qty ships from, read everywhere as
  `COALESCE(NULLIF(SRC_RDC,''), RDC)` (see pendalc.md). `sz1_approve_service` therefore writes
  `SRC_RDC` = the 1SZ line's `SRC_RDC` and `RDC` = store master, and its idempotency guard is on
  `SRC_RDC`. Supersedes "RDC = ship-from" in the 2026-10-12 phase 3 rule above. 1SZ's own tables
  are unchanged — they already carried RDC = store, SRC_RDC = ship-from — and its free stock reads
  ARS_MSA_TOTAL, so it inherits MSA's corrected pending deduction (by SRC_RDC). Code on branch
  `for_merge`, not yet deployed or migrated (`backend/scripts/041_src_rdc_concept.py`).
