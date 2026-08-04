---
title: FA and CONS Module
tags: [ars, fa-cons, module, allocation]
updated: 2026-07-29
---

# FA & CONS Module

**FA** (Fixed Assets / Project-store allocation) and **CONS** (Consumables) are two operational allocations being brought into ARS from manual Excel. Both live in the same module—separate streams, shared data-platform. Phases A–C landed 2026-07-29.

## What it is

**FA** — new/UPC stores get a daily allocation of project equipment (fixtures, IT hardware, security systems) from a central warehouse. Demand = project team's MBQ per store × ref-article; allocation works by `pack_size` rounding (nearest pack, min 1).

**CONS** — pan-India consumables (carry bags, cash rolls, tape, tags) need daily auto-replenishment. Demand = auto-calc'd `MIN(max-hold-capacity, 15-day sales) + per-day-sale × cover-days`; reuses existing ACS_D/SAL_PD/ALC_D columns.

Data model: **separate streaming**. One MBQ upload, auto-classified by `VW_MASTER_PRODUCT.DIV` (FA vs CO), then split by scope (UPC stores vs old stores vs all). Each gets its own pending/allocation/gap pipeline (core pend_alc UNTOUCHED).

## Pages & features (all live)

| Page | Feature | Status |
|------|---------|--------|
| **SLOC Settings** | Live per-SLOC stock qty (all active+inactive), % share, sortable, "With stock" filter, X-of-Y coverage footer | ✓ 2026-07-28 |
| **Stock & MSA** | REF_ART grain (clubs articles by real reference); article drill; filter-reactive cards; SLOC chips show qty; auto-sync on load | ✓ Phase 1 2026-07-28 |
| **MBQ Master** | Upload + dry-run + confirm; change-review tab (history, session-wise); faceted filters + comma search; export | ✓ 2026-07-23 |
| **UPC Store List** | Planner-maintained store roster; validation (not-in-master, missing-in-list); status history; auto-priority by OP_DT | ✓ 2026-07-24 |
| **Allocation** | Run FA/CONS/UPC/OLD/ALL; summary tiles; ref-level results; articles modal (max-qty-first split + manual override); session dropdown | ✓ Phase B 2026-07-24 |
| **Pending Alloc** | Separate from core: approve-session, manual-add, deliver, close/reopen; DO lifecycle (type or auto); ops log | ✓ Phase C 2026-07-24 |
| **Gap Report** | Per-store gap, warehouse allocation, purchase req, overstock/return; 3 tabs (store/purchase/return); ATR badge filter | ✓ 2026-07-24 |
| **Help** | BRD/FSD/recorded-rules dossier (7 sections, bookmark rail) | ✓ 2026-07-24 |

Module-wide: progress indicator (busy bar, delivery truck SVG, auto-label for any /fa-cons request); export buttons on all pages.

## Data model — REF_ART grain (Phase 1, LIVE)

MBQ upload is per **reference-article** — a real product grouping from `VW_MASTER_PRODUCT.REF_ART`. The master has ~819K populated REF_ART values (281K distinct), each mapping to 1..many `ARTICLE_NUMBER` (size/color variants).

**Key GOTCHA: "NA" is a real value** — ~9,092 FA articles and ~4,863 CONS articles have REF_ART='NA' (literal string), meaning "no reference, standalone product" (accessory, tag, mannequin). These MUST NOT club into one row — they remain distinct per article.

**Rule: `effective_ref_art`**
```sql
CASE
  WHEN REF_ART IN ('NA', '', '0', ' 0') THEN 'G~' + GEN_ART_NUMBER
  ELSE REF_ART
END
```

This avoids the 1-to-N collapse. Storage: `ARS_FACONS_STOCK` and `ARS_FACONS_MSA` now carry `article_number`, `gen_art`, and `ref_auto` columns.

**Stock & MSA results:**
- `grain='ref'` (default): pivots on (stream, loc, maj_cat, effective_ref_art) → sums articles + shows articles-count badge + hover reveals member articles + their stock
- `grain='article'` (drill): pivots on (…, article_number, sz) → one row per actual SKU

API endpoint: `GET /fa-cons/stock/results?stream={FA|CONS|ALL}&scope={STORE|MSA}&seq=&grain={ref|article}`

**Data facts:**
- FA: 12,573 articles → 1,958 real refs + 9,092 'NA'
- CONS: 7,169 articles → 1,326 real refs + 4,863 'NA'
- Stock reconciles EXACTLY between SLOC Settings (live) and Stock&MSA (persisted calc)

## SLOC model — on/off per source (final)

No standalone FA/CONS SLOC table. Instead, added **`fa_active` / `co_active` BIT columns** to the two LISTING tables:
- `ARS_STORE_SLOC_SETTINGS` (reads ET_STORE_STOCK by WERKS for store-floor SLOCs: 0001, 0002, DW01_PRD_QTY, DW01_STO_QTY_Q, etc.)
- `ARS_MSA_SLOC_SETTINGS` (reads ET_MSA_STK or ET_STORE_STOCK DC-side by RDC for DC SLOCs: HUB_INTRA, HUB_PRD, ST_PRD, V02_FRESH, V02_GRT, etc.)

Calc (`facons_stock_service.calculate`) loops each STREAM (FA/CONS) × SCOPE (STORE/MSA), sums only the active SLOCs for that stream, groups by the fact-table's location grain:
- STORE scope: loc=WERKS (store code)
- MSA scope: loc=RDC (DC code) or WERKS fallback

Results persisted in `ARS_FACONS_STOCK` (per store) and `ARS_FACONS_MSA` (per RDC).

**Perf win:** 15-min TTL cache table `ARS_FACONS_SLOC_STOCK_CACHE` + on-cache-hit serves in ~44ms (was ~6.2s per page open). Discovery auto-folded into the same query. Page auto-syncs on load.

## Key gotchas & resolutions

### 1. ET_MSA_STK is fashion-only
`VW_ET_MSA_STK_WITH_MASTER` (the live MSA stock view) contains **zero rows** for DIV=FA/CO — it's MEN/LADIES/KIDS/ACCESSORIES only. So direct MSA-scope read hits nothing.

**Resolution:** Both STORE and MSA scopes now read `ET_STORE_STOCK` (single fact table). MSA scope groups by RDC (DC-side SLOCs like DW01_PRD_QTY, HUB_INTRA) vs STORE by WERKS (store-floor). This requires the planner to activate DC-side SLOCs in `ARS_MSA_SLOC_SETTINGS`.

### 2. REF_ART missing in stock view
`VW_ET_MSA_STK_WITH_MASTER` (and ET_STORE_STOCK) have no REF_ART column. The stock rows are keyed on ARTICLE_NUMBER/MATNR.

**Resolution:** `_sum_source` LEFT JOINs `VW_MASTER_PRODUCT` on ARTICLE_NUMBER (verified unique across 19,742 articles) to fetch REF_ART + gen_art + color + maj_cat, then applies `effective_ref_art` rule. If JOIN fails (article not in master, orphan data), ref_auto='MISSING' as a marker.

### 3. Negative net-stock SLOCs
Some SLOCs (e.g., CONS MSA SLOC 0044) can have **negative aggregate stock** (returns exceeded receipts), causing % share to exceed 100% when a large negative row shrinks the denominator.

**Resolution:** Clamp per-SLOC qty to `max(0, qty)` in backend `_compute_qty`, and client-side defensive clamp `Math.max(0, totals[sloc])` in the SLOC Settings page. Negative net-stock SLOC contributes 0 to the dispatchable pool.

### 4. DIV=FA/CO in upload auto-classification
Users upload a single MBQ sheet (ST_CD, REF_ART, MBQ_Q, optionally REMARKS & SOURCE_TYPE). System auto-derives the stream.

**Resolution:** `facons_mbq_service.resolve_stream(ref_art)` → lookup in `VW_MASTER_PRODUCT.DIV`: DIV='CO' → CONS, DIV='FA' → FA, else default FA. Backfill: `reclassify_streams()` re-derives existing 342 rows (228 FA / 114 CONS).

### 5. MBQ from new project stores (no ET_STORE_STOCK rows yet)
UPC stores (HA10, HL13, etc.) are listed in `Master_ALC_INPUT_ST_MASTER` but have **zero ET_STORE_STOCK rows** (opened recently, no stock data yet). MBQ Master shows store_stock=0, fill%=0%.

**Resolution:** Correct data, not a bug. Once stock is stocked in the master, numbers populate. Workflow: **set SLOC roles → Calculate → then MBQ refreshes.** Do not expect stock before the store is physically opened.

## Pending & allocation — separate lifecycle

`ARS_FACONS_PEND` and `ARS_FACONS_ALLOC_*` tables (FA/CONS exclusive, core untouched).

**Pending flow:**
1. Allocation runs (ref-level demand vs warehouse pool → alloc_qty per ref)
2. Ref rows split into articles (MAX-QTY-first by default, or manual override)
3. Approve-session converts articles into pending-alloc lines (OPEN status)
4. Manual add (one-off; reason mandatory)
5. Deliver (qty + remarks; auto-close at full)
6. Close / reopen (reason mandatory)
7. Ops log (APPROVE/MANUAL_ADD/DELIVER/CLOSE/REOPEN per line)

**DO (Delivery Order) lifecycle (2026-07-29):**
- Generate DO stamps a DO# on selected OPEN lines (type or auto running 'FACONS-DO-000n')
- One DO covers many lines (logical grouping)
- Delivery is allowed with or without a DO (DO is informational)
- generate_do(pend_ids, do_number) + endpoint POST /pend/generate-do
- update_line(pend_id, article/qty/sz/clr, reason) + endpoint PUT /pend/{id}

## Performance highlights

| Page | Metric | Before | After | Win |
|------|--------|--------|-------|-----|
| SLOC Settings qty | Per-page load | 6.2s | 44ms | **140×** |
| MBQ Master | Per-list | 5.5s | 0.04s | **130×** |
| Stock & MSA | Grain switch | Filter cleared | Filter preserved | UX |

Cache TTL: 15 min (user can force-refresh).

## Related notes

- [[UPC Store Tracking]] — similar upload pattern, event history
- [[Report Generation Hub]] — scheduler for daily CONS auto-calc
- [[Fresh-GRT Allocation]] — ⚠ "GRT" name collision; FA/CONS uses Growth type, Bin uses warehouse type
- [[Pending Allocation and Hold]] — core (untouched); FA/CONS is a separate self-contained pipeline
- `frontend/public/docs/manual/fa_cons.md` — canonical BRD/FSD dossier (source of truth)

## References

Memory: `[[project_fa_cons_module]]` (full dev log, 130 lines of dated entries / design decisions / code fixes).

Sidebar: "FA & CONS" section (after Reports) with Help, SLOC Settings, Stock & MSA, MBQ Master, UPC Store List, Allocation, Pending Alloc, Gap Report.

Routes: `/fa-cons/help`, `/fa-cons/sloc-settings`, `/fa-cons/stock`, `/fa-cons/mbq-master`, `/fa-cons/store-list`, `/fa-cons/allocation`, `/fa-cons/pending`, `/fa-cons/gap-report`.

Endpoints: Prefix `/fa-cons/` for all FA/CONS operations (sloc-settings, stock, mbq, store-list, alloc, pend, gap).
