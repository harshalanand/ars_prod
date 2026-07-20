---
title: Data Model
tags: [ars, data-model, tables, migrations]
updated: 2026-07-17
---

# Data Model

Two logical databases on `HOPC866`: **Claude** (system — RBAC/RLS/audit/jobs, SQLAlchemy-modeled) and **Rep_Data** (business/operational ARS_* tables — mostly **raw-SQL, dynamic-schema**). Full lineage + live-validated sample rows in `docs/ARS_BRD_END_TO_END.md` §4/§15.

> [!warning] ORM vs reality
> `backend/app/models/retail.py` (`retail_gen_article`, `retail_variant_article`, `alloc_header/detail`, `store_stock`…) is a **generic/aspirational** schema — those tables largely **do not exist in prod**. The live catalogue is the view `vw_master_product`; operational tables are dynamic raw-SQL managed via migrations + `sys_table_registry`. Only RBAC/RLS/audit/job tables are truly ORM-backed.

## Tables by lifecycle stage (Rep_Data unless noted)

### Masters (uploaded inputs)
| Table | Grain | Role |
|-------|-------|------|
| `Master_ALC_INPUT_ST_MASTER` | ST_CD | store master: `RDC` map, `INT_DAYS/PRD_DAYS/SL_CVR`, status, `MANUAL_ST_PRIORITY` |
| `Master_ALC_INPUT_CO_MAJ_CAT` / `_ST_MAJ_CAT` | MAJ_CAT / ST×MAJ_CAT | category base + store overlay (carries CM/NM sale, DISP_Q) |
| `Master_ALC_INPUT_CO_ART` / `_ST_ART` | MAJ_CAT×10_DIGIT×CLR (+ST) | article cascade: I_ROD, MANUAL_DENSITY, CORE/AUTO/HH flags |
| `MASTER_GEN_ART_SALE` | ST×MAJ_CAT×GEN_ART×CLR | pre-computed `SAL_PD` |
| `Master_CONT_<dim>` / `Master_CONT_MERGE_<dim>` | ST×MAJ_CAT×dim | contribution per grain / merged (derived by [[Merge Rules]]) |
| `Cont_presets` | preset | KPI/contribution presets (JSON) — see [[Contribution and CONT]] |
| `vw_master_product` (view) | ARTICLE_NUMBER | **the product catalogue**; carries MP cols FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG; ATT_TYP 00/02=variants |
| `et_msa_stk` + `VW_ET_MSA_STK_WITH_MASTER` | DATE×ST_CD×SLOC×ARTICLE | MSA stock source (not `ET_STORE_STOCK`) |
| `ET_STORE_STOCK` | store stock | store-level `STK_TTL` for Grid/Listing |

### MSA → Grid → Listing/Alloc
| Table | Grain | Role |
|-------|-------|------|
| `ARS_MSA_TOTAL` / `_VAR_ART` / `_GEN_ART` | RDC×article×SZ×ALLOC_TYPE (+ rollups) | [[MSA Stock Calculation\|MSA]] outputs (replaced legacy `cl_*`) |
| `MSA_Calculation_Sequence` / `MSA_Column_Definitions` | run / table×column | run tracking / dynamic-column registry |
| `ARS_MSA_SLOC_SETTINGS` | sloc | FRESH/GRT pool classification |
| `ARS_GRID_BUILDER` | grid | [[Grid Builder\|grid]] defs (group, sec_cap, pivot_only, hierarchy_columns) |
| `ARS_GRID_<name>` (`MJ`, `MJ_MERGE_RNG_SEG`, `MJ_RNG_SEG`…) | grid grain | per-grid MBQ/OPT_CNT/DISP_Q/STK_TTL |
| `ARS_GRID_HIERARCHY`, `ARS_CALC_ST_MAJ_CAT`, `ARS_CALC_ST_ART` | MAJ_CAT / store / article | applicability registry (ADD-ONLY) + cascaded params |
| `ARS_MERGE_RULES` | (source_col, source_value) | merge ruleset |
| `ARS_LISTING` / `_WORKING` | OPT / eligible OPT | [[Listing\|listing]] full build / engine input |
| `ARS_LISTED_OPT` | listed OPT | Stage A output |
| `ARS_ALLOC_WORKING` | VAR_ART×SZ | per-size SHIP/HOLD/remarks + `ALLOC_TYPE`, `STOCK_CONSIDER_DT`, `PICKING_DT` (info-only run dates, stamped Part 8.36). **DROP+SELECT INTO each run** |
| `ARS_STORE_RANKING` | store×MAJ_CAT | W_SCORE / ST_RANK |
| `ARS_LISTING_SESSIONS` | session | run history/log, ALLOC_TYPE, REQUEST_JSON |
| `ARS_*_PARKED` / `ARS_*_HISTORY` | run snapshot / promoted | park-then-promote on [[Review and Approve\|approve]] |

### Pend / Hold / BDC (feedback loop)
| Table | Grain | Role |
|-------|-------|------|
| `ARS_PEND_ALC` | SESSION×RDC×ST_CD×ARTICLE×ALLOC_MODE | pending ledger (PK on ID only) |
| `ARS_PEND_ALC_OPERATIONS` | per operation | revertable ops log |
| `ARS_BDC_HISTORY` | per BDC line | SAP-upload audit (OPEN/CLOSED_PARTIAL/CONFIRMED/CANCELLED) |
| `ARS_NL_TBL_HOLD_TRACKING` (+`_SNAPSHOT`, `_SNAPSHOT_SESSIONS`) | WERKS×VAR_ART×SZ×ALLOC_TYPE | hold ledger + revert snapshot |
| `ARS_STORE_BDC_SCHEDULE` | ST_CD × weekday | BDC store selection |
| `ARS_ALLOCATION_MASTER` | file BDC line | file-driven BDC output |
| `ARS_ASYNC_JOBS` | job | cross-worker job state |

### System (Claude DB — ORM)
`rbac_roles/permissions/role_permissions/users/user_roles`; `rls_stores`, `rls_user_{store,region,category}_access`, `rls_column_restrictions`, `rls_table_role_access`, `table_settings`, `table_permissions`; `audit_log`, `data_change_log`; `export_settings/jobs`, `upload_jobs`, `msa_storage_jobs`; `sys_table_registry`, `sys_column_registry`.

### Run audit
| Table | Grain | Role |
|-------|-------|------|
| `ARS_RUN_PARAMS_AUDIT` | one row per `PARAM_GROUP·PARAM_NAME` per run (`RUN_ID`+`SESSION_ID`+`USER_ID`+`RUN_TS`) | tall/EAV snapshot of **every tunable + condition** that produced a listing run — LISTING/RANKING/ALLOCATION/SEC_CAP/FLAGS. Written best-effort by `/listing/generate` (never blocks a run). Incl. flat `SEC_CAP/sec_cap_mode` (`STANDARD`\|`MATRIX`, 2026-07-18) + `growth_matrix` JSON. Read via `GET /listing/sessions/{sid}/run-params`; reviewed on the standalone **Run Parameters** page (`RunParamsPage.jsx`, route `data-prep/listing/run-params`, opened from the Listing cockpit like *View Logs*) — view one run or **Compare** two. |

### Reports
`ARS_REPORTS`, `ARS_REPORT_RUNS`, `ARS_SF_WATERMARKS` — see [[Report Generation Hub]].

## Migrations (`backend/scripts/*.sql`)
> [!warning] Duplicate numbering — two series coexist
> Numbers **014–017 each exist twice** with different scopes. Don't assume one ordered chain.

- **001–013 (Claude core + MSA):** `001_create_schema` (RBAC/RLS/audit), export/data-change tables, `011` legacy `cl_*` MSA tables + sequence, `013` msa_storage_jobs.
- **Old 014–017 (RBAC/RLS):** `014_create_ars_msa_tables` (rename `cl_*`→`ARS_MSA_*`), `015_add_category_rls`, `016_sloc_status_column`, `017_add_module_permissions`.
- **New descriptive 014–021 (highlight these):** `014_create_contribution_tables`; **`015_create_sloc_settings`** (FRESH/GRT pools); **`016_alloc_type_columns`** (ALLOC_TYPE on pend/alloc; widen hold PK); **`017_report_generation`** (report hub); `018_alloc_type_integrity` (CHECK constraints); `019_rename_msa_sloc_settings`; `020_allocation_engine_tables` (score-based `alloc_*` engine — separate from the live pipeline); `021_add_manual_st_priority`.
- **DB bootstrap:** `create_claude_db.sql`, `create_rep_data_db.sql`, `create_master_gen_art_age.sql`.

## Gotchas
- **ALLOC_TYPE sentinels differ:** `ARS_PEND_ALC/ALLOC_HISTORY/ALLOC_WORKING` use NULL for legacy; `ARS_NL_TBL_HOLD_TRACKING` uses `''` (PK member). Code treats both as untyped/both-pools.
- **`ARS_ALLOC_WORKING` is DROP+SELECT INTO each run** — no persistent constraints/indexes; ALLOC_TYPE normalization enforced in code, not CHECK.
- **Two allocation engines** — the live Listing→Alloc pipeline vs the score-based `alloc_*` engine (mig 020, ALC_Fixture pages, superadmin-only). Don't conflate.
- **MSA result tables grow columns at runtime** (`MSA_Column_Definitions`) — schema not static.
- **`ARS_PEND_ALC` PK on ID only** — no unique on the logical grain (double-count risk). See [[Known Risks and Doc Drift]].

## Cross-links
[[Pipeline Overview]] · every stage note · [[Fresh-GRT Allocation]] (migrations 015/016/018/019).
