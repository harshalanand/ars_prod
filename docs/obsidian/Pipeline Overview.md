---
title: Pipeline Overview
tags: [ars, pipeline, architecture]
updated: 2026-07-17
---

# Pipeline Overview

The whole [[ARS Work Log|ARS]] system is one ordered, **stateful** pipeline. Each stage feeds the next; a gate guards every boundary. The dashed feedback loop (pending + hold net down next cycle's stock) is what makes ARS stateful across daily runs.

```
UPLOAD masters + stock (CSV/Excel → upsert; typed-table guard)
      │
      ▼   GATE: R1–R6 reconciliation (Δ=0)
[1] MSA STOCK  ─ 12 steps ─►  FNL_Q = max(STK − PEND − HOLD, 0)   → ARS_MSA_TOTAL/GEN_ART/VAR_ART (row-per-type FRESH+GRT)
      │
      ▼   GATE: every grid row carries FAB/MACRO/MICRO/M_VND/RNG_SEG…
[2] GRID BUILDER ─► MBQ, OPT_CNT, DISP_Q, STK_TTL per grid  ◄── [3] MERGE RULES (E,V→EV; derive Master_CONT_MERGE_*)
      │
      ▼   GATE: ELIG_FLAG=1 (AND-gate) · PRI_CT% ≥ 100 (ALLOC_FLAG)
[4] LISTING ─► OPT_TYPE = RL/TBC/TBL/MIX · demand · ST_RANK · growth → ARS_LISTING_WORKING
      │
      ▼
[5] ALLOCATION (per_opt) ─► RL→TBC→TBL waterfall, hold-first→pool, pak, sec-cap veto → ARS_ALLOC_WORKING  → run PARKED
      │
      ▼   Approve → promote 5 working tables → *_HISTORY   |   Reject → discard
[6] REVIEW (human gate)
      │
      ▼
[7/8] HOLD + PENDING ─► write ARS_PEND_ALC (+PEND→MSA) & hold Step A/B → BDC file → SAP → DO upload draws down PEND
      │
      └──────── feeds next cycle's MSA (STK − PEND − HOLD) ────────┐ (loop)
```

## Stages at a glance
| # | Stage | Trigger / entry | Output tables | Note |
|---|-------|-----------------|---------------|------|
| 0 | [[Data Ingestion (Stage 0)]] | `POST /upload/` (+`/async`) | any target table | typed-table guard blocks generic writes to pool-governed tables |
| 1 | [[MSA Stock Calculation]] | `POST /msa/calculate` | `ARS_MSA_TOTAL/GEN_ART/VAR_ART`, `MSA_Calculation_Sequence` | 12 steps; reads `VW_ET_MSA_STK_WITH_MASTER` |
| 2 | [[Grid Builder]] | `/grid-builder/run(-all)` | `ARS_GRID_*`, `ARS_CALC_ST_*`, `ARS_GRID_HIERARCHY` | Primary vs sec-cap grids |
| 3 | [[Merge Rules]] | `/merge-rules` | `Master_CONT_MERGE_<col>`, `MERGE_<col>` | config + derived masters |
| 4 | [[Listing]] | `POST /listing/generate` | `ARS_LISTING(_WORKING)`, `ARS_LISTED_OPT`, `ARS_STORE_RANKING` | numbered Parts 1–8 |
| 5 | [[Rule Engine (per_opt)]] | Part 8 of listing | `ARS_ALLOC_WORKING` | Stage A/B (SQL) + Stage C band (pandas) + Stage D |
| 6 | [[Review and Approve]] | `/listing/parked-runs/{sid}/approve` | promotes to `*_HISTORY` | run is PARKED until approved |
| 7/8 | [[Pending Allocation and Hold]] | approve → BDC → DO | `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING`, `ARS_BDC_HISTORY` | feedback loop |

## The gates (a run violating any is broken)
- **MSA R1–R6** — pending/hold/stock sums reconcile Δ=0; every open obligation lands on a row. If any ≠0, fix MSA before proceeding.
- **Grid** — sec-cap dimension columns must propagate `listing → listed → alloc` or grids silently drop.
- **Listing** — only `ELIG_FLAG=1` rows enter `ARS_LISTING_WORKING`; `PRI_CT% ≥ 100` sets `ALLOC_FLAG`.
- **Review** — a completed run is PARKED; Generate is blocked while a parked run awaits review (unless multi-parked enabled / admin).

## Cross-cutting invariants
See [[ARS Glossary#Invariants]]: OPT uniqueness · growth at MJ+grid only · MBQ=0 = no constraint (with the per_opt hard-block exception) · sec-cap dims propagate · ACS_D ≠ daily sale · RNG_SEG = MRP tier.
