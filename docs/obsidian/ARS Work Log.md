---
title: ARS Work Log
tags: [ars, moc, index]
updated: 2026-07-18
---

# ARS V2 Retail — Knowledge Vault (Home)

Map of Content for the **V2 Retail Auto Replenishment System** (ARS). This vault is a code-validated companion (read 2026-07-17) to the canonical `frontend/public/docs/manual/<module>.md` dossiers and `docs/ARS_BRD_END_TO_END.md`. Where this vault and code disagree, **the code + `/manual/*` dossiers win**.

> [!note] Recent changes
> **[[2026-07-18]]** — RL/TBC dispatch is now **COMPLETE-only** (SCALED disabled + a COMPLETE-overshoot fall-through bug fixed); Hold Control pre-fills from Run Pool; Run Setup MAJ_CAT search now surfaces exact SSN groups. New Grid Report analytics proc (dim-aware hold split) — see [[Reports]]. See [[dispatch-complete-only]].

> [!info] What ARS does
> Answers one question automatically, at scale: **what stock goes to which store, and how many pieces of each size.** 320+ stores · 242 MAJCATs · replaces a 150-machine 24 hour Excel macro process. Owner: Akash Agarwal (Director, V2 Retail). Repo `github.com/harshalanand/ars`, branch `ars_v2`.

## The pipeline (read in order)
[[Pipeline Overview]] ties these together with the gates between them.

0. [[Data Ingestion (Stage 0)]] — upload masters + stock → any target table (typed-table guard)
1. [[MSA Stock Calculation]] — 12-step free-stock calc → `ARS_MSA_*`
2. [[Grid Builder]] — MBQ / grids / hierarchy → `ARS_GRID_*`
3. [[Merge Rules]] — collapse tiers (E,V→EV) → `Master_CONT_MERGE_*`
4. [[Listing]] — classify OPT_TYPE, eligibility → `ARS_LISTING_WORKING`
5. [[Rule Engine (per_opt)]] — the allocation waterfall → `ARS_ALLOC_WORKING`
   - [[Allocation Waterfall (Step by Step)]] — **exhaustive gate-by-gate deep dive**
   - [[Secondary-Grid Cap]] · [[Contribution and CONT]]
6. [[Review and Approve]] — human gate; park → promote to history
7. [[Pending Allocation and Hold]] — feedback loop → BDC → SAP → DO

## Cross-cutting
- [[Fresh-GRT Allocation]] — typed FRESH/GRT pools through the whole pipeline
- [[Contribution and CONT]] — analytical vs size-level CONT
- [[Allocation Waterfall (Step by Step)]] — exhaustive gate-by-gate band trace
- [[Allocation Engine v2 (score-based)]] — the separate score-based engine (rollout / ALC_Fixture)
- [[Report Generation Hub]] — scheduled / manual / event-triggered reports
- [[Reports]] — ad-hoc / stored-proc SQL reports catalogue (incl. the parameterized Grid Report)
- [[Dashboards Trends and Admin Tools]] — read-side analytics + operator/admin pages
- [[ARS Glossary]] — every abbreviation and formula term
- [[Data Model]] — table catalogue + migrations
- [[Platform and Infrastructure]] — auth, RBAC/RLS, upload, settings, reset
- [[Frontend Map]] — routes, API client, state
- [[Known Risks and Doc Drift]] — open correctness risks + stale docs found in the read

## Vault conventions
- **Templates** (`Templates/` folder, wired to the Templates core plugin): `Module Note`, `Deep-Dive Note`, `Table Reference`, `Daily Note`. Insert via command palette → "Templates: Insert template". Daily notes land in `Journal/`.

## Milestones (2026)
| Date | What landed |
|------|-------------|
| 2026-07-09 | [[Fresh-GRT Allocation\|Fresh/GRT]] typed pools + selective hold; [[Report Generation Hub]] Phases 1–4 |
| 2026-07-10 | **Engine consolidation** — `per_opt` becomes the ONLY band; pandas/sequential/parallel/SQL engines + `listing_allocator.py` removed (`allocation_mode != per_opt` → 400) |
| 2026-07-10 | Report hub: SQL-query steps, split output, email, Snowflake scaffold, stop/cancel, orphan cleanup |
| 2026-07-13 | [[Secondary-Grid Cap\|Sec-cap]] two-pass — hard-block veto beats Primary overshoot admit |
| 2026-07-15 | End-to-end BRD authored + live-validated against `HOPC866`; DB target confirmed local `HOPC866` |
| 2026-07-16 | `write_pend_alc` MAJ_CAT join fix (pending no longer fans out per MAJ_CAT) |
| 2026-07-18 | Dispatch **COMPLETE-only** (SCALED disabled, fall-through double-stamp fixed); run-cockpit UX (Hold Control pre-fill, MAJ_CAT search ranking, banded Tunable Params, BDC-schedule store auto-load); **info-only run dates** `STOCK_CONSIDER_DT`+`PICKING_DT` on alloc output; **rounding** SAL_PD→2dp / CONT%→4dp; **data dictionary** audit + top-up + `.xlsx` export ([[2026-07-18]]) |
| 2026-07-18 | [[Reports]] — Grid Report proc `usp_ars_grid_report` (any `ARS_GRID_MJ[_dim]`; dynamic type-aware ISNULL, store/product/listing/MSA/hold joins, dim-aware hold split, OPT>50 count) |
| 2026-07-22 | [[UPC Store Tracking]] — store-opening lifecycle tracker (`/reports/upc-tracking`): upload ST_CD+proposed/share dates; event history (date/remark/status) with date+time; live MBQ/stock/SLOC/FR by segment (VW_MASTER_PRODUCT SEG, default APP+GM); dispatch-lead Bal Days (dispatch/opening) + Repl Days (1st-share/latest-share/layout/display) + **D.GAP** (last date shift); inline edit status/remark/layout/display/priority; priority synced to master `MANUAL_ST_PRIORITY`; charts value-labelled+clickable; column hide, sticky header, Export All/View, Help panel |

## Environment & verification
- **Live DB (local):** SQL Server `HOPC866` (ex-`HOPC560`, commit `c68e10d`) — `Rep_Data` (business/MSA/grid/listing/alloc/pend/hold) + `Claude` (`rbac_*`, `rls_*`, jobs, audit).
- Verify from `backend/` with the venv: `./venv/Scripts/python.exe`, `from app.database.session import get_data_engine, get_system_engine`. **Do not** use the DataV2 MCP tools — wrong server.
- Deploy: Azure zipdeploy (see `CLAUDE.md`). API `https://ars-v2retail-api.azurewebsites.net`.

## Authoritative docs
- `docs/ARS_BRD_END_TO_END.md` — the master end-to-end BRD (live-validated masters in §4).
- `frontend/public/docs/manual/*.md` — per-module BRD+FSD+Recorded rules (render in-app at `/manual/*`).
- `docs/RULE_MASTER.md` — rule extract. `.claude/agents/ars_flow_kb/` — terse KB extracts (dossiers win).
