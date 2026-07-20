---
title: Allocation Engine v2 (score-based)
tags: [ars, allocation, v2, score-based]
updated: 2026-07-17
---

# Allocation Engine v2 (score-based)

A **separate, newer** allocation engine — NOT the live [[Rule Engine (per_opt)|per_opt]] pipeline. It scores each article against each store on weighted attributes and cascades budget by score. In rollout; the UI (ALC_Fixture pages) is superadmin-only. Don't conflate the two engines.

- **Router:** `/allocation-engine` (`backend/app/api/v1/endpoints/allocation_engine.py`) + service `backend/app/services/allocation_engine.py`.
- **Migration:** `scripts/020_allocation_engine_tables.sql` (Rep_Data).
- **Frontend:** `/alc-fixture/{tunables,execute,review,dashboard,jobs}` (AlcFixture*Page).

## Concept
Instead of the OPT_TYPE waterfall, an article's fit for a store is a **weighted score** over matching attributes; budget cascades to the highest-scoring article×store assignments.

## Score config (`alloc_score_config`, UI-tunable)
Seeded `default` weights (attribute → weight):
`ST_SPECIFIC 9999` (store-specific override, effectively infinite — article only scores for its target stores) · `NATIONAL_HERO 100` · `CORE_FOCUS 60` · `SEG 30` · `ASSORTED 30` · `MACRO_MVGR 25` · `VENDOR 20` · `MRP_RANGE 15` · `MVGR1 15` · `FABRIC 10` · `COLOR 10` · `SEASON 10` · `GP_PSF_RANK 10` · `NECK 5`. Unique on `(config_name, attribute_name)`; `is_active` toggles a factor.

## Tables (mig 020, Rep_Data)
`alloc_score_config` (weights) · `alloc_engine_settings` (key/value settings, typed) · `alloc_runs` (run header) · `alloc_budget_cascade` (budget flow) · `alloc_article_scores` (per article×store score) · `alloc_option_assignments` / `alloc_variant_assignments` (results at option / variant grain) · `alloc_delivery_orders` · `alloc_run_summary`.

## Key endpoints (`/allocation-engine`)
- **Run/status:** `POST /run`, `GET /runs`, `GET /status/{run_id}`.
- **Config:** `GET /config`, `PUT /config/weights`, `PUT /config/settings`.
- **Results:** `GET /results/{run_id}/{summary|assignments|scores|variants|store-summary}`.
- **Budget:** `POST /budget/upload`, `GET /budget/data`.
- **Misc:** `GET /majcats`, `GET /seed-test-data`, `GET /snowflake/test`.

## Status & caveats
- Score-based engine is **parallel to, not part of** the production Listing→Allocation flow (which is [[Rule Engine (per_opt)]]). Runs write to `alloc_*` tables, not `ARS_ALLOC_*`.
- Superadmin-only ALC_Fixture pages during rollout.
- This note is scoped from migrations + route signatures; the scoring/cascade internals are not yet deep-read to the level of the [[Allocation Waterfall (Step by Step)]] note. Deep-dive is a good follow-up if this engine goes live.

## Cross-links
[[Rule Engine (per_opt)]] (the live engine) · [[Data Model]] (mig 020 tables) · [[Frontend Map]] (ALC_Fixture routes).
