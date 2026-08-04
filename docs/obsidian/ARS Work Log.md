---
title: ARS Work Log
tags: [ars, moc, index]
updated: 2026-07-31
---

# ARS V2 Retail — Knowledge Vault (Home)

Map of Content for the **V2 Retail Auto Replenishment System** (ARS). This vault is a code-validated companion (read 2026-07-17) to the canonical `frontend/public/docs/manual/<module>.md` dossiers and `docs/ARS_BRD_END_TO_END.md`. Where this vault and code disagree, **the code + `/manual/*` dossiers win**.

> [!note] Recent changes
> **[[2026-07-31]]** — **Business Rules registry migrations**: parking mode (ALC_MULTI_PARKED) + audit mode (LST_AUDIT_ALL_TO_WORKING) migrated from AppSettings → rule registry (6 rules now wired, 9 unwired, total 15). **GOTCHA:** seed `default_active` must match live value at migration or next run flips behavior silently. Back-compat dual-write keeps old Settings toggles in sync. See [[project_business_rules_module]] migration pattern section. **[[2026-07-31]]** — **[[Listing OPT_TYPE classification]]** Option B IN-RUN TBL HOLD RELEASE + RETRY LANDED in `rule_engine_pandas.py` `_run_majcat_waterfall`: freed under-covered holds re-injected into pool_dict, ONE TBL retry pass same-run; 133/133 freed pcs had takers; eligibility mask excludes SKIPPED/gate-vetoed OPTs; `STK_TTL` added to base-load cols; both call sites plumbed. Dossier updated, restart + staging run needed. **[[2026-07-30]]** — **[[Listing OPT_TYPE classification]]** post-alloc LANDED: OPT_STATUS vocab (NL/RL/WRL/TBL/L/MIX); Part 8.55 hold release (MIX options, 3-table zero-ing); data-dict 60 SEEDs + formulas. **[[2026-07-29]]** — **[[FA and CONS module]]** Phases A–C LANDED (7 pages + progress indicator). See [[FA and CONS module]] for ref_art grain model + do lifecycle + gotchas. **[[2026-07-25]]** — [[SAP Integration]] module live: read-only data gateway via universal MCP (no SDK); SAP_CONNECTION+PULL_DEF+PULL_RUN DB tables + scheduled/on-demand pulls; GOTCHA—RFC DATA_BUFFER_EXCEEDED unless explicit fields[] on wide tables.

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
| 2026-07-20 | [[Report Generation Hub]] — **WhatsApp Config module** (DB-backed, access token encrypted at rest via [[project_secret_encryption]]); Settings tab + test/verify/send endpoints; RBAC ADMIN_SETTINGS + SUPER_ADMIN token-modify; audit log captures IP/machine. |
| 2026-07-20 | [[Report Generation Hub]] — **streaming export** for OOM fix (4.5M-row procedures peak 250k in memory not 3+ GiB); peeks result-set size, adaptive hierarchy split for streamed data, CSV/xlsx written via batched writes. Peak heap 48.5 MB vs old 3+ GiB. |
| 2026-07-20 | [[Report Generation Hub]] — **split-threshold fix** (split governed SOLELY by "Max rows per file" not memory cap; ARS_GRID_MJ_MERGE_RNG_SEG ~310k→1 file not 4); adaptive hierarchy two-pass local spill + LRU-bounded grouped writer. |
| 2026-07-20 | [[Report Generation Hub]] — **process isolation** (reports run in separate OS process, not in-thread; fixed app freeze during report generation; health latency ~0.22s vs >25s); worker payload to tempfile + `subprocess.Popen` detached + `/status` running_report_ids. |
| 2026-07-20 | [[Report Generation Hub]] — **storage guardrails** (retention cleanup of old `FOLDER_PER_RUN` dated folders; disk pre-flight free-space assert); config app_settings.json 'reports' {retention_days:7, min_free_mb:500, cleanup_enabled:true}; fixes "[Errno 28] No space left on device". |
| 2026-07-23 | [[Reports]] — MSA Master proc `usp_ars_msa_master` (opening-vs-closing reconciliation; `@Level` rollup DETAIL→RDC via catalog view `vw_ars_msa_master_levels`; comma-list runs levels as ordered STEPS; zero-rows dropped; CAST + `OPTION(RECOMPILE)` perf fixes) |
| 2026-07-23 | Report Generation — proc-param **dropdowns + multi-select** (registry `ARS_PROC_PARAM_VALUES`), **drag-reorder** steps, and **FANOUT** (one suffixed file per selected level, e.g. `MSA_OP_CL_<level>`). New `memory-scribe` subagent auto-syncs memory + this vault. See [[Reports]]. |
| 2026-07-23 | [[Reports]] — Grid Report ALL measures now dimension-split (ALC_Q/HOLD_ALC_Q/ALC_FROM_HOLD_Q/ART_EXCESS_QTY + MSA_OP_Q/MSA_CL_Q/OP_OPT_CNT_>50_PCS/CL_OPT_CNT_>50_PCS reconcile exactly to base-MJ totals on secondary grids); `@MAJ_CAT`/`@WERKS` filters pushed to CTEs → **42.8s→7.6s** perf; added NOLOCK audit. |
| 2026-07-23 | **Report suite committed** (`ars_v2` `659d99d`): usp_ars_grid_report + usp_ars_msa_master + ARS_PROC_PARAM_VALUES registry + ReportGenerationPage + report-gen/scheduler/data-export refactors + memory-scribe agent; all NOLOCK audit + dimension-split measures verified [[Reports]]. |
| 2026-07-22 | [[UPC Store Tracking]] — store-opening lifecycle tracker (`/reports/upc-tracking`): upload ST_CD+proposed/share dates; event history (date/remark/status) with date+time; live MBQ/stock/SLOC/FR by segment (VW_MASTER_PRODUCT SEG, default APP+GM); dispatch-lead Bal Days (dispatch/opening) + Repl Days (1st-share/latest-share/layout/display) + **D.GAP** (last date shift); inline edit status/remark/layout/display/priority; priority synced to master `MANUAL_ST_PRIORITY`; charts value-labelled+clickable; column hide, sticky header, Export All/View, Help panel |
| 2026-07-24 | [[Bin Allocation module]] (sidebar display title **"GRT ALC"** finalized; ⚠ GRT collides with [[Fresh-GRT Allocation\|Growth pool type]]) scaffold + BRD/FSD dossier (server-side Jobs engine, REJECTED browser-only design); 5 sidebar routes; BIN_MASTER/_REQ/_SESSION/_ALIGNED/_UNALIGNED/_LOG tables planned; MAJ_CAT prefix normalization gotcha. Backend deferred. |
| 2026-07-25 | [[SAP Integration]] module **BUILT & WORKING**: read-only SAP data ingestion via universal MCP gateway (no SDK/pyrfc); RFC + OData doors; SAP_CONNECTION/PULL_DEF/PULL_RUN + scheduler daemon; /sap/* API + sidebar (Connection, Data Pulls, Explorer, Run History); **GOTCHA:** RFC_READ_TABLE DATA_BUFFER_EXCEEDED (512-byte limit) unless explicit fields[]; verified LQUA prod pulls. WHERE builder + Discovery panel (catalog DD02T/DD03L/DD04T). **Reverted:** per-field FIELD_MAP labels + type-aware inputs. **REPLACED:** global DISPLAY_MODE ('name'|'label'|'both') in SAP_CONNECTION, synced via sapUiStore.js Zustand store, applies across all screens. |
| 2026-07-27 | [[SAP Integration]] — **fully built & live-verified**: core module (SAP_CONNECTION/PULL_DEF/PULL_RUN tables + scheduler daemon + /sap/* API + sidebar 4-page routes); **WHERE builder** (structured conditions, live preview, frontend/backend round-trip); **SAP Explorer discovery** enhanced with search box, SELECT ALL/CLEAR (respects filter), field chips with autocomplete, double-layer header; **global DISPLAY_MODE setting** (name|label|both) on Connection page synced to zustand + localStorage, applied everywhere; **Snowflake door** (3rd data source: BRONZE.SAP_* direct reads via KEY-PAIR JWT, same scheduler/run-history landing); **GOTCHAS:** OData env=dev-only (qa→502, prod→error); OData services custom (Z_SB_ARTICLE/BSIK/MARD/ACDOCA/EKKO/EKPO), all not_shipped to prod (Basis import via STMS required); **REVERTED:** FieldsEditor, FIELD_MAP landing columns, auto-fetch labels, data-type quoting (P4/P5/P6 rolled back — fields stay raw SAP names, labels display-only). All code live-compiled clean. |
| 2026-07-27 | **[[Snowflake shared config]]** refactor — Snowflake connection extracted from SAP into app-wide SNOWFLAKE_CONNECTION table (Settings → Snowflake tab); key-pair JWT or password auth; shared by SAP Snowflake door + Report Gen scheduler (snowflake_sync gains key-pair support). NEW snowflake_config_service.py + /settings/snowflake API. sap_snowflake_client.py is thin delegate. Gotchas: restart backend, key file must exist, legacy password fallback. |
| 2026-07-29 | **[[FA and CONS module]]** Phases A–C LANDED: SLOC Settings (live per-SLOC qty, 140× cache perf), Stock & MSA (REF_ART grain Phase 1; articles club by real ref; NA-fallback rule), MBQ Master (upload/history/change-review), UPC Store List (planner roster + validation), Allocation (ref→article split + manual override), Pending Alloc (approve-session + DO lifecycle), Gap Report (store/purchase/return tabs). 8 pages + module-wide busy indicator. See [[FA and CONS module]]. |
| 2026-07-30 | **[[Listing OPT_TYPE classification]]** — Pre-alloc: New L type (report-only, not allocated); TBL tightened, RL extended (sold-out+hold-only); MIX(b) removed. Post-alloc Part 8.5: **OPT_STATUS** vocabulary NL/RL/WRL/TBL/L/MIX (exhaustive, no NA); WRL & TBL carry 0 alloc by construction; dry-run 18k options reconcile. Part 8.55: hold release (MIX→0 on 3 table copies; gotcha: PARKED→HISTORY materializes hold). Data-dict: 60 SEEDs + detailed formulas + self-heal (ownership check widened). Backend restart needed. |
| 2026-07-31 | **[[Listing OPT_TYPE classification]]** — **Option B: In-Run TBL Hold Release + Retry** (`rule_engine_pandas.py` `_run_majcat_waterfall`): post-TBL, under-covered options release HOLD_QTY back into pool_dict + get retry pass same-run; eligibility mask excludes gate-vetoed (SKIPPED); POOL_CONSUMED not decremented (self-grab prevention). **Gotchas:** `STK_TTL` added to base-load columns (suffix-scan missed plain name); pool_dict keys str-normalized. Measured: 133/133 freed pcs had takers on same key, 4.5k unmet demand, 29 POOL_EMPTY rows. Dossier updated; needs backend restart + staging run for log delta. | |
| 2026-07-31 | **Business Rules registry** — Module-wise behavior switches via superadmin Settings → Business Rules page; ARS_BUSINESS_RULES + ARS_BUSINESS_RULES_LOG tables; service with 30s TTL fallback semantics (INACTIVE/MISSING → caller default, safe against table breakage); 15 seeded rules (6 wired: ALC_MULTI_PARKED [parking mode migrated], LST_AUDIT_ALL_TO_WORKING [audit mode migrated], LST_IROD_AMIX_FLOOR, LST_OPT_STATUS_STAMP, ALC_HOLD_RELEASE_855, ALC_TBL_HOLD_RETRY; 9 unwired incl. UPC_TRACKING_ENABLED). **Migration gotcha:** seed `default_active` must match live value at migration time or next run flips behavior silently. Back-compat dual-write syncs old Settings toggles ↔ new Business Rules page. [[project_business_rules_module]] | |
| 2026-07-31 | **RBAC audit + Session management + Module-Access permissions** — CRITICAL FIX: ANALYST role stripped of 10 admin-console permissions (ADMIN_USERS_*, ADMIN_ROLES_MANAGE, ADMIN_RLS_MANAGE, ADMIN_SETTINGS, ADMIN_PERMS_MANAGE, ADMIN_AUDIT_READ, COLUMN_EDIT_MANAGE) → now data-work only. **18 MOD_* permissions** auto-seeded per sidebar menu (MOD_DATA_MGMT, MOD_LISTING_ALLOC, etc.), granted once to all roles, restrict via Settings → Roles (visibility-preserving migration). **Session table** rbac_user_sessions with jti enforcement; endpoints POST /auth/logout, GET /auth/sessions, DELETE /auth/sessions/{id} (SUPER_ADMIN); **SEC_SINGLE_LOGIN rule** (wired, seeded inactive → multi-login default preserved). Registry now 16 rules / 7 wired. **GOTCHA:** JWT_SECRET_KEY placeholder in config.py needs real value in .env before handover. [[project_rbac_sessions_module_perms]] | |
| 2026-08-01 | **PAK_SZ over-allocation correctness fix** — Post-pass pack-rounding nets rounded SHIP_QTY WITHOUT checking FNL_Q pool (2 rows shipped 6 qty vs pool 3). Root: per-OPT engine skipped `short=` marker when need already pak-aligned → post-pass was unguarded. **Fix:** move pack conversion in-band to per-OPT engine + apply `MIN(target, pool)` invariant + disable post-pass nets. Both bug rows now ship 3 (pool size). [[gotcha_pak_sz_over_allocation]] |
| 2026-08-01 | **[[Reports\|Grid Report]]** — Two new features (live HOPC866): **MJ_PER_OPT_MBQ column** = ACS_D + SAL_PD × ALC_D (per-OPT grid minimum); **Dynamic OPT_STATUS pivot** (CNT_/ALCQ_* per status: NL/RL/WRL/TBL/L/MIX) built at runtime from ARS_LISTING_WORKING_HISTORY. **Gotchas:** OPT_STATUS ALTER-added + backfilled 3.29M rows (future auto-carry via _history_columns); dim-split reconciliation gotcha (OPTs w/ NULL dim count at base, drop from dim view); ~9s base / ~36s RNG_SEG perf (no covering index). [[project_grid_report_usp]] |

## Environment & verification
- **Live DB (local):** SQL Server `HOPC866` (ex-`HOPC560`, commit `c68e10d`) — `Rep_Data` (business/MSA/grid/listing/alloc/pend/hold) + `Claude` (`rbac_*`, `rls_*`, jobs, audit).
- Verify from `backend/` with the venv: `./venv/Scripts/python.exe`, `from app.database.session import get_data_engine, get_system_engine`. **Do not** use the DataV2 MCP tools — wrong server.
- Deploy: Azure zipdeploy (see `CLAUDE.md`). API `https://ars-v2retail-api.azurewebsites.net`.

## Authoritative docs
- `docs/ARS_BRD_END_TO_END.md` — the master end-to-end BRD (live-validated masters in §4).
- `frontend/public/docs/manual/*.md` — per-module BRD+FSD+Recorded rules (render in-app at `/manual/*`).
- `docs/RULE_MASTER.md` — rule extract. `.claude/agents/ars_flow_kb/` — terse KB extracts (dossiers win).
