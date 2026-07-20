---
title: Merge Rules
tags: [ars, merge, grid]
updated: 2026-07-17
---

# Merge Rules

Stage 3 of the [[Pipeline Overview|pipeline]] — a **configuration + derived-master layer** upstream of the [[Grid Builder|grid]] joins. It is NOT the allocation rule engine (naming clash with `rule_engine`).

- **Source:** `backend/app/api/v1/endpoints/merge_rules.py` (API) + `backend/app/services/derived_masters.py` (engine).
- **Why:** MRP tiers (`RNG_SEG`) are often finer than the budget needs. A `V`-tier option coming up just short is skipped while an interchangeable `E`-tier option sits unused. Merge collapses near-equal source values into a shared bucket (e.g. `E,V → EV`) so budget pools across merged tiers.

## Table & rules
`ARS_MERGE_RULES(rule_id, source_col, source_value, target_value, agg, active…)`, UNIQUE `(source_col, source_value)`. One `agg` per `source_col`; `source_col` must not start with `MERGE_`.

**Live rules:** `RNG_SEG → {E→EV, V→EV, P→PSP, SP→PSP}`, `agg=SUM` ⇒ `MERGE_RNG_SEG ∈ {EV, PSP}`.

## Derivation
On any rule write, `refresh_derived_for_source_col`:
```
MERGE_<col> = CASE [<col>] WHEN 'E' THEN 'EV' WHEN 'V' THEN 'EV' WHEN 'P' THEN 'PSP' ... ELSE [<col>] END
-- single txn TRUNCATE + INSERT:
INSERT Master_CONT_MERGE_<col>
SELECT ST_CD, MAJ_CAT, MERGE_<col>, <agg>(TRY_CAST(CONT AS FLOAT)), GETDATE()
GROUP BY ST_CD, MAJ_CAT, MERGE_<col>            -- agg ∈ SUM|AVG|MAX|MIN
```
Then re-derives `ARS_GRID_HIERARCHY.MERGE_<col>` (`_populate_merge_columns`). All refreshes are **best-effort** — a failure warns but never fails the CRUD call.

## Rules / gates
- One row per `(source_col, source_value)`; unmapped values pass through `ELSE` unchanged.
- One `agg` per `source_col` — enforced at create/update/bulk; mixed → warn + fall back to SUM.
- **CONT reconciliation** (SUM only): parent vs derived `SUM(CONT)` drift > 0.01 warns (a value slipped through `ELSE`).
- A `MERGE_<X>` grid in [[Grid Builder]] requires an Active parent grid ending in `X`.

## Key endpoints
`GET ""` (list) · `GET /source-cols` · `POST ""` (upsert one, agg-conflict guard) · `PUT/DELETE /{rule_id}` · `POST /refresh/{src}` · `POST /bulk` (atomic preflight, validates every row + within-batch dupes, max 5000).

## Cross-links
[[Grid Builder]] (consumes `Master_CONT_MERGE_*` and `MERGE_<col>`; live Primary `MJ_MERGE_RNG_SEG` is built on this) · [[Contribution and CONT]] (CONT is the merged quantity) · [[Data Model]].
