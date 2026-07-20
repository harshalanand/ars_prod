---
title: ARS Glossary
tags: [ars, glossary, reference]
updated: 2026-07-17
---

# ARS Glossary

Domain terms and formula vocabulary for [[ARS Work Log|ARS V2 Retail]]. Column-level detail lives in the in-app Data Dictionary (`ARS_DATA_DICTIONARY`, `/data-dictionary`).

## Core units
| Term | Meaning |
|---|---|
| **OPT** | Option — one allocation unit = `(WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)`; one style+colour of an article at one store |
| **OPT_TYPE** | Exactly one of **RL / TBC / TBL / MIX** per OPT per run (mutually exclusive at OPT grain) |
| **WERKS** | Store code (SAP plant) = ship-**to** store; = `ST_CD` in alloc history |
| **RDC** | Regional Distribution Centre = ship-**from** warehouse; renamed from `ST_CD` right after the MSA pivot |
| **MAJ_CAT** | Major category = the planning unit (e.g. `M_TEES_HS`); 242 active |
| **GEN_ART / VAR_ART** | Generic article (colour-level) / variant article (style+size = SKU) |
| **CLR / SZ** | Colour / size (default `'A'` when missing) |
| **SEG** | Segment — filtered to `APP` (apparel) + `GM` (general merchandise) |
| **SLOC** | Storage location (shelf) within an RDC; classified FRESH/GRT — see [[Fresh-GRT Allocation]] |
| **ATT_TYP** | SAP article category — `00` single, `02` variant (kept); `01` header, `11` prepack (dropped) |

## OPT_TYPE classes
- **RL** — Replenish (live): adequate display + fresh MSA supply; standard top-up.
- **TBC** — To-Be-Continued: low positive stock + supply available; partial top-up.
- **TBL** — To-Be-Listed: zero/negative stock + supply available; fresh launch, holds a ramp-up buffer.
- **MIX** — clearance / insufficient supply / poor colour-fill; rolled up, not allocated.
- **NL** — New Launch merchandise held back at WH for ramp-up (a hold concept, not an OPT_TYPE class value).

## Stock / demand / budget
| Term | Meaning / formula |
|---|---|
| **STK / STK_TTL** | Stock quantity (total across selected SLOCs) |
| **PEND** | Pending — approved but not-yet-dispatched allocation reserved against stock |
| **HOLD / HOLD_REM** | Held stock reserved at RDC for a store; `HOLD_REM` = still on hold |
| **FNL_Q** | **Free-to-allocate** = `max(STK − PEND − HOLD, 0)` — never negative |
| **FNL_Q_REM** | Live pool remaining **after** an OPT's draw (per-OPT engine) |
| **MBQ** | Minimum/Merchandise Budget Quantity at a grain. `MBQ=0` = *no constraint* (see Invariants) |
| **OPT_MBQ** | `ROUND(ACS_D + rate × ALC_D, 0)`; `OPT_REQ = max(0, OPT_MBQ − STK_TTL)` |
| **OPT_MBQ_WH** | `OPT_MBQ` + `HOLD_DAYS` uplift **for TBL only** (ramp-up buffer) |
| **SZ_MBQ** | Size-level = `ROUND(OPT_MBQ × CONT, 0)`; `OPT_MBQ = Σ SZ_MBQ` |
| **`*_MBQ_ORIG / *_MBQ_REV`** | Pre-growth snapshot / post-growth grid budget |
| **CONT** | Contribution — share of budget for a grain; size CONT sums to 1 per OPT. See [[Contribution and CONT]] |
| **ACS_D** | **Accessories density** = one OPT's *display* quantity. **NOT daily sale** |
| **MAX_DAILY_SALE** | True velocity metric (MAX of L-7 daily and `AUTO_GEN_ART_SALE`) |
| **ALC_D** | Sale-cover days = `INT_DAYS + PRD_DAYS + SL_CVR` |
| **SAL_PD** | Per-day sale (blended current-month CM + next-month NM) |
| **I_ROD** | Ideal Rounds Of Dispatch (how many MBQ multiples to ship) |
| **PAK / PAK_SZ** | Pack multiple constraint at size grain |
| **RNG_SEG** | MRP tier — `E` economy / `V` value / `P` premium / `SP` super-premium |

## Grids & flags
| Term | Meaning |
|---|---|
| **Primary grid** | Budget baseline, counts to `PRI_CT%`, NOT sec-capped. Live: `MJ`, `MJ_MERGE_RNG_SEG` |
| **Secondary / sec-cap grid** | Ceiling on a second dimension (`sec_cap_applicable=1`, default 130%). See [[Secondary-Grid Cap]] |
| **GH_<grid> / H_<grid>** | Grid applies to MAJ_CAT (0/1) / applies AND has a requirement |
| **PRI_CT% / SEC_CT%** | Primary/secondary coverage = `ΣH / ΣGH × 100` |
| **ALLOC_FLAG** | Allocation-eligible (`PRI_CT% ≥ 100`) |
| **ELIG_FLAG / ELIG_REASON** | Listing eligibility AND-gate result / first failing gate |
| **ALLOC_TYPE** | Typed pool of a row — `FRESH` or `GRT` (legacy NULL/`''` folds to FRESH) |
| **ST_RANK / W_SCORE** | Store priority rank per MAJ_CAT / its score |

## Lifecycle & SAP
| Term | Meaning |
|---|---|
| **DO** | Delivery Order — SAP shipment confirmation event |
| **BDC** | Bulk Delivery Creation — SAP-ready allocation upload file/channel |
| **PARKED** | A completed run awaiting human review (not yet official) |
| **RLS / RBAC** | Row-Level Security (per-user MAJ_CAT/store scoping) / Role-Based Access Control |

## Invariants
A run violating any of these is broken:
1. **OPT uniqueness** — one `(WERKS, MAJ_CAT, GEN_ART, CLR)` ⇒ exactly one OPT_TYPE.
2. **Growth at MJ+grid only** — growth % applied at MAJ_CAT×grid, never per OPT_TYPE (per-OPT_TYPE sliders are downward *caps*, not growth).
3. **MBQ sparseness** — `MBQ=0` = *no constraint* at that grain — **except** the per_opt hard-block: an *applicable* sec-cap grid with empty value / `MBQ_ORIG=0/NULL` blocks the OPT (see [[Secondary-Grid Cap]] and [[Known Risks and Doc Drift]]).
4. **Sec-cap dims propagate** — grid extras survive `listing → listed → alloc`.
5. **`ACS_D` ≠ daily sale** — ACS_D is display density; velocity uses `MAX_DAILY_SALE`.
6. **`RNG_SEG` = MRP tier** — values `E/V/P/SP` only.
