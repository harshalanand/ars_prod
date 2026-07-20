---
title: Data Ingestion (Stage 0)
tags: [ars, ingestion, upload]
updated: 2026-07-17
---

# Data Ingestion (Stage 0)

The very start of the [[Pipeline Overview|pipeline]] — masters + warehouse stock must be current before any run. ARS is only as correct as its masters.

- **Entry:** `POST /upload/` (sync) and `POST /upload/async` (background job). Permission `DATA_UPLOAD`. Source: `upload.py` + `file_upload_service.py` + `upload_job_service.py` + `upsert_engine.py`.

## How it works
- Reads CSV/Excel via pandas (off the event loop), normalizes column names (uppercase, `_`), validates PKs, drops null-PK rows, cleans cells, pre-validates types → `UpsertEngine.upsert` (chunked `UPLOAD_CHUNK_SIZE=10000`, row-level audit + `batch_id`). Modes: `upsert` (default) / `delete`.
- **Cell semantics:** blank → `__SKIP__` (keep existing DB value); `"NA"` → literal "NA"; `|` or `-` → `__NULL__` (set NULL).
- Async path saves to `uploads/`, inserts an `UploadJob` row, enqueues on a single daemon worker (FIFO, one at a time). Uploads >50k rows skip detailed row audit (sample only). File auto-deleted after processing.

## Gates
- **Typed-table guard** (`TYPED_TABLES` / `_reject_typed_table`): a generic upload into any `ALLOC_TYPE`-bearing / pool-governed table (`ARS_PEND_ALC`, `ARS_MSA_*`, hold/parked/alloc-history) is **rejected (400)** — those are written only by the engine/lifecycle paths.
- MP columns (`FAB, MACRO_MVGR, MICRO_MVGR, M_VND_CD, RNG_SEG`) must be present in `vw_master_product` or [[Secondary-Grid Cap|sec-cap]] grids silently drop.
- Uploading a `Master_CONT_<col>` parent triggers a `derived_masters` rebuild of `Master_CONT_MERGE_<col>` (see [[Merge Rules]]).

## What gets uploaded
The masters listed in [[Data Model]] (store/category/article masters, sale, contribution, `vw_master_product`, store stock). Warehouse MSA stock (`VW_ET_MSA_STK_WITH_MASTER` over `et_msa_stk`) is provided by Snowflake daily **before** the ARS window.

## Endpoints
`POST /upload/` · `/upload/async` · `/upload/preview` · `/upload/sheets` · `/upload/jobs` (+ `/jobs/all` admin, `/jobs/{id}`, cancel, delete, `/queue/status`).

## Cross-links
[[Data Model]] (target tables) · [[MSA Stock Calculation]] (consumes stock) · [[Platform and Infrastructure]] (upload subsystem) · [[Pipeline Overview]].
