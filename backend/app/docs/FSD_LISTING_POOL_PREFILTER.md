# Functional Specification Document (FSD)
## Listing Generate — Pre-filter MSA DISTINCT options by RUN POOL + `FNL_Q > 0`

**Document Version:** 1.0
**Date:** 2026-10-09
**Parent BRD:** `BRD_LISTING_POOL_PREFILTER.md`
**Related spec:** `FSD_FRESH_GRT_HOLD_CONTROL.md` — this change hoists FS-02's `FNL_Q > 0` gate earlier in the pipeline
**File touched:** `backend/app/api/v1/endpoints/listing.py` (one file, three localized edits)
**Dossier update:** `frontend/public/docs/manual/listing.md` (one Recorded-rule bullet)

---

## 1. Where the gate lives today vs. after

```
/listing/generate
  │
  ├─ Part 1 — Grid INSERT (IS_NEW=0)                     [unchanged]
  │
  ├─ Part 2 — MSA missing INSERT (IS_NEW=1)
  │     reads DISTINCT (MAJ_CAT, GEN_ART_NUMBER, CLR) FROM msa_table
  │     ⚠ TODAY: filtered only by RDC + MAJ_CAT; fans every option × every store
  │     ✅ AFTER: also filtered by (ALLOC_TYPE = pool AND FNL_Q > 0)  ← NEW GATE
  │
  ├─ Part 2.5 — Indexes                                   [unchanged — scans fewer rows]
  ├─ Part 3.5a — Attribute merge (vw_master_product)      [unchanged — scans fewer rows]
  ├─ Part 3.55 — MSA_FNL_Q + VAR_COUNT                    [unchanged — already type-filtered]
  ├─ Part 3.6 — OPT_TYPE classification                   [unchanged — scans fewer rows]
  ├─ Part 3.7 — MIX handling                              [unchanged — scans fewer rows]
  │
  └─ Stage A/B → _create_alloc_working (FS-02)
            │
            └─ existing `FNL_Q > 0` pre-filter             [kept — defence in depth]
```

Design principle: the downstream gate at `_create_alloc_working` is **not removed**. It stays as the authoritative drop point and defence-in-depth; the new filter is purely a **performance hoist** that avoids fanning dead options across every store in the first place.

---

## 2. Code changes (listing.py — three edits)

### FS-LPP-01 — Build the pool filter once, right after `msa_rdc_filter`

**Location:** between the end of the `msa_rdc_filter` block and the `msa_sql` definition. Approximately **lines 1359–1368** in the pre-change file.

**Insert this block:**

```python
# ── Pool pre-filter: pull FS-02's FNL_Q>0 gate forward from
# _create_alloc_working. Skips options with no stock in the chosen
# pool (and other-pool typed rows), which otherwise fan across every
# store in Part 2 as untouchable IS_NEW=1 phantoms. Guards on column
# presence so legacy MSA (no ALLOC_TYPE / no FNL_Q) stays byte-identical.
_msa_cols_pool = _get_columns(conn, req.msa_table) if _table_exists(conn, req.msa_table) else []
_msa_pool_filter = ""
_msa_pool_params: dict = {}
if "ALLOC_TYPE" in _msa_cols_pool:
    _msa_pool_filter += " AND ISNULL([ALLOC_TYPE],'FRESH') = :pool_alloc_type"
    _msa_pool_params["pool_alloc_type"] = req.alloc_type
if "FNL_Q" in _msa_cols_pool:
    _msa_pool_filter += " AND TRY_CAST([FNL_Q] AS FLOAT) > 0"
```

Then **append `{_msa_pool_filter}`** to the `WHERE` clause of `msa_sql`:

```python
msa_sql = f"""
    SELECT DISTINCT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
    FROM [{req.msa_table}]
    WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}{_msa_pool_filter}
"""
```

**Why this shape, not a per-site rebuild:**
- One source-of-truth variable (`_msa_pool_filter`) reused by the diagnostic AND Part 2's INSERT — a drift here would make the diagnostic count disagree with the row actually fanned.
- `_get_columns` is already in use at Part 3.55 (same pattern — the ALLOC_TYPE guard there uses the same column check).
- `ISNULL([ALLOC_TYPE],'FRESH')` matches the normative FS-02 default: untyped legacy rows are treated as FRESH (the historical pool). A GRT run therefore does NOT see legacy untyped rows via this filter — that is intentional (GRT consumes the typed-only subset).
- `TRY_CAST([FNL_Q] AS FLOAT) > 0` tolerates NVARCHAR MSA columns (MSA stores most columns as VARCHAR(MAX)) and treats NULL/non-numeric as 0.

### FS-LPP-02 — Pass params to the diagnostic COUNT(*) + extend the log

**Location:** the `diag_msa` line (currently line **1418** pre-change).

**Replace:**
```python
diag_msa = conn.execute(text(f"SELECT COUNT(*) FROM ({msa_sql}) X")).scalar()
logger.info(f"Diagnostic: stores={diag_stores}, grid_rows={diag_grid}, msa_options={diag_msa}")
```

**With:**
```python
diag_msa = conn.execute(text(f"SELECT COUNT(*) FROM ({msa_sql}) X"), _msa_pool_params).scalar()
logger.info(
    f"Diagnostic: stores={diag_stores}, grid_rows={diag_grid}, msa_options={diag_msa}"
    f"{' (pool=' + req.alloc_type + ', FNL_Q>0)' if _msa_pool_filter else ''}"
)
```

**Why the suffix only when active:** on legacy MSA (no ALLOC_TYPE / FNL_Q) the suffix is absent → a grep on today's historical logs still matches the old line verbatim.

### FS-LPP-03 — Apply the filter to Part 2's `msa_with_rdc` + pass params to `_run`

**Location:** the `msa_with_rdc` definition and the `_run(conn, …)` INSERT immediately after (currently lines **1466–1488** pre-change).

**Replace `msa_with_rdc`:**
```python
# MSA base (RDC column preserved for joining, except when clubbing).
# Pool pre-filter (ALLOC_TYPE + FNL_Q>0) is applied here too — skipping
# these options is where the Part 2 fan-out savings come from.
msa_with_rdc = f"""
    SELECT DISTINCT
        {_p2_rdc_select}
        {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
    FROM [{req.msa_table}]
    WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}{_msa_pool_filter}
"""
```

**Replace the `_run(conn, """…""")` call tail with the params argument:**
```python
_run(conn, f"""
    INSERT INTO [{LISTING_TABLE}] ({all_cols})
    SELECT
        S.[ST_CD] AS WERKS, S.[RDC],
        M.[MAJ_CAT], M.[GEN_ART_NUMBER], M.[CLR]
        {stk_zeros_str}, 0 AS STK_TTL, 0 AS STR, 1 AS IS_NEW, NULL AS OPT_TYPE, S.[ST_STATUS]
    FROM ({msa_with_rdc}) M
    INNER JOIN ({stores_sql}) S ON 1=1 {msa_rdc_join}
    WHERE NOT EXISTS (
        SELECT 1 FROM [{LISTING_TABLE}] E
        WHERE E.[WERKS] = S.[ST_CD]
          AND E.[MAJ_CAT] = M.[MAJ_CAT]
          AND E.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
          AND E.[CLR] = M.[CLR]
    )
""", _msa_pool_params or None)
```

**Why `_msa_pool_params or None`:** `_run` (alias for `app.utils.db_helpers.run_sql`) accepts `params: dict = None`. Passing `None` when the filter is a no-op avoids binding an unused `:pool_alloc_type` parameter.

---

## 3. SQL semantics — proof of no-op

Let `S` = the set of distinct `(MAJ_CAT, GEN_ART_NUMBER, CLR)` tuples currently emitted by the pre-change `msa_sql`.
Let `S'` = the set emitted after the change (same WHERE + `AND ISNULL(ALLOC_TYPE,'FRESH') = pool AND TRY_CAST(FNL_Q AS FLOAT) > 0`).

`S' ⊆ S`. For every tuple `t ∈ S \ S'` (an option dropped by the new filter), one of two conditions holds:

1. **`ALLOC_TYPE ≠ pool`** on every MSA row for `t`: the typed-pool contract in `FSD_FRESH_GRT_HOLD_CONTROL` says such rows do not contribute to the chosen pool's FNL_Q_EFF. Part 3.55 already treats them as 0 via `_mtype_filter`, and `_create_alloc_working` would drop them for `FNL_Q = 0`.
2. **`FNL_Q = 0`** on every MSA row for `t` of the correct type: `_create_alloc_working`'s FS-02 pre-filter at `listing_allocator.py:167–179` would drop every store-row for `t` with pool stock 0.

In both cases, the pre-change pipeline inserts the option into `[LISTING_TABLE]`, computes `OPT_TYPE` / sec-cap / MBQ caps, and then drops it with 0 ship / 0 hold. `_create_alloc_working` is kept — so even if the pre-filter is incorrectly loosened (e.g. missing columns), the final allocation still cannot ship from an empty pool.

---

## 4. Non-functional requirements

| ID | Requirement |
|---|---|
| NF-LPP-1 | Pool-filter overhead ≤ 1 call to `INFORMATION_SCHEMA.COLUMNS` + 1 extra predicate per DISTINCT scan. On live HOPC866 the diagnostic query is ~200 ms either way. |
| NF-LPP-2 | Measured Part 2 row-count reduction ≥ 50 % on any multi-pool MSA (observed 91.9 % on 2026-10-09 production MSA). |
| NF-LPP-3 | Byte-identical `ARS_ALLOC_WORKING` / `ARS_ALLOC_HISTORY` vs. pre-change baseline on the acceptance test TC-04 (regression gate). |
| NF-LPP-4 | Legacy MSA (missing ALLOC_TYPE or FNL_Q) → `_msa_pool_filter == ""` → byte-identical SQL to today. |

---

## 5. Acceptance tests (HOPC866, project SQLAlchemy engine)

| ID | Test | Expected |
|---|---|---|
| TC-01 | FRESH `/listing/generate` on current `ARS_MSA_GEN_ART` | Diagnostic log shows `msa_options=9188 (pool=FRESH, FNL_Q>0)` |
| TC-02 | GRT `/listing/generate` on same MSA | Diagnostic log shows a different (typed) count; GRT-pool options only |
| TC-03 | FRESH on an MSA snapshot pre-ALLOC_TYPE (reproducible via a 2026-05 backup table) | Diagnostic log shows the raw count, no `(pool=…)` suffix — back-compat |
| TC-04 | Approve the TC-01 run; diff `SHIP_QTY_TOTAL` and `HOLD_QTY_TOTAL` vs. pre-change baseline for the same request | Totals identical (≤ ±0 pcs) |
| TC-05 | Measure Part 2 INSERT time, Part 2.5 Index time, Part 3.5a attribute-merge time | Each < 50 % of the pre-change baseline (observed 9 %, 10 %, pending) |
| TC-06 | Confirm Part 1 (grid IS_NEW=0 rows) unchanged | Row count equals grid_rows |

**Live validation (2026-10-09, session `20261009_232448_185` vs. hung `20261009_161826_077`):**
- `msa_options` 103,654 → 9,188 (−91.1%)
- Part 2 rows 38,832,290 → 3,112,304 (−91.9%)
- Part 2 INSERT 98.1s → 9.3s (−90.5%)
- Part 2.5 Indexes 61.0s → 6.4s (−89.5%)

---

## 6. Rollout / implementation order

1. **FS-LPP-01** — add `_msa_pool_filter` + `_msa_pool_params`, extend `msa_sql`. **Testable alone** (diagnostic count drops).
2. **FS-LPP-02** — thread params + extend log suffix. **Testable alone**.
3. **FS-LPP-03** — extend `msa_with_rdc` + pass params to `_run`. **End-to-end gain lands here**.
4. Append the Recorded-rule bullet in `frontend/public/docs/manual/listing.md` under `## Recorded rules` (dated 2026-10-09; cite the measured impact).
5. Deploy via standard zipdeploy flow in `CLAUDE.md`.

Each step is independently verifiable; a failure in step 3 does not require rolling back step 1 or 2.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| A future MSA stops stamping `ALLOC_TYPE` → pool filter accidentally drops GRT rows | Column-presence guard: filter skipped when `ALLOC_TYPE` absent. |
| `FNL_Q` stored as a string that doesn't parse | `TRY_CAST(... AS FLOAT)` returns NULL on parse failure; NULL > 0 is FALSE in SQL Server — row is dropped, matching the intent (no stock info ⇒ no allocation). |
| `_create_alloc_working` pre-filter is one day removed as "redundant" | **Do NOT remove it.** This change is a hoist, not a replacement. The downstream gate is defence in depth and must stay. |
| MSA_FNL_Q at listing grain (per `(MAJ_CAT, GEN_ART_NUMBER, CLR)`) and allocator pool (per RDC × ARTICLE) use different grains, so FNL_Q > 0 at MSA grain could still mean 0 at RDC grain | Part 3.55 recomputes MSA_FNL_Q per listing row with the typed sum; the engine's own pool read then applies the `FNL_Q > 0` gate at the correct grain. The pre-filter here is a strict subset — if MSA aggregate FNL_Q = 0, every per-RDC subset is also 0. |

---

## 8. Full file-level diff (apply verbatim)

Only one file is touched. The three hunks correspond to FS-LPP-01, FS-LPP-02, FS-LPP-03 in order.

```diff
--- backend/app/api/v1/endpoints/listing.py  (pre-change)
+++ backend/app/api/v1/endpoints/listing.py  (post-change)
@@ around the end of the `msa_rdc_filter` block @@
         else:
             stores_sql = _stores_sql()
             msa_rdc_filter = ""

+        # ── Pool pre-filter: pull FS-02's FNL_Q>0 gate forward from
+        # _create_alloc_working. Skips options with no stock in the chosen
+        # pool (and other-pool typed rows), which otherwise fan across every
+        # store in Part 2 as untouchable IS_NEW=1 phantoms. Guards on column
+        # presence so legacy MSA (no ALLOC_TYPE / no FNL_Q) stays byte-identical.
+        _msa_cols_pool = _get_columns(conn, req.msa_table) if _table_exists(conn, req.msa_table) else []
+        _msa_pool_filter = ""
+        _msa_pool_params: dict = {}
+        if "ALLOC_TYPE" in _msa_cols_pool:
+            _msa_pool_filter += " AND ISNULL([ALLOC_TYPE],'FRESH') = :pool_alloc_type"
+            _msa_pool_params["pool_alloc_type"] = req.alloc_type
+        if "FNL_Q" in _msa_cols_pool:
+            _msa_pool_filter += " AND TRY_CAST([FNL_Q] AS FLOAT) > 0"
+
         # ── MSA unique options (proper types + RDC filtered) ──────────────
         msa_sql = f"""
             SELECT DISTINCT {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
             FROM [{req.msa_table}]
-            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}
+            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}{_msa_pool_filter}
         """

@@ around the diagnostic COUNT(*) + log line @@
-        diag_msa = conn.execute(text(f"SELECT COUNT(*) FROM ({msa_sql}) X")).scalar()
-        logger.info(f"Diagnostic: stores={diag_stores}, grid_rows={diag_grid}, msa_options={diag_msa}")
+        diag_msa = conn.execute(text(f"SELECT COUNT(*) FROM ({msa_sql}) X"), _msa_pool_params).scalar()
+        logger.info(
+            f"Diagnostic: stores={diag_stores}, grid_rows={diag_grid}, msa_options={diag_msa}"
+            f"{' (pool=' + req.alloc_type + ', FNL_Q>0)' if _msa_pool_filter else ''}"
+        )

@@ around `msa_with_rdc` and the Part 2 INSERT @@
-        # MSA base (RDC column preserved for joining, except when clubbing)
+        # MSA base (RDC column preserved for joining, except when clubbing).
+        # Pool pre-filter (ALLOC_TYPE + FNL_Q>0) is applied here too — skipping
+        # these options is where the Part 2 fan-out savings come from.
         msa_with_rdc = f"""
             SELECT DISTINCT
                 {_p2_rdc_select}
                 {_msa_col('MAJ_CAT')}, {_msa_col('GEN_ART_NUMBER')}, {_msa_col('CLR')}
             FROM [{req.msa_table}]
-            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}
+            WHERE [MAJ_CAT] IS NOT NULL AND [GEN_ART_NUMBER] IS NOT NULL{mc_where}{mp_majcat_filter}{msa_rdc_filter}{_msa_pool_filter}
         """
         _run(conn, f"""
             INSERT INTO [{LISTING_TABLE}] ({all_cols})
             SELECT
                 S.[ST_CD] AS WERKS, S.[RDC],
                 M.[MAJ_CAT], M.[GEN_ART_NUMBER], M.[CLR]
                 {stk_zeros_str}, 0 AS STK_TTL, 0 AS STR, 1 AS IS_NEW, NULL AS OPT_TYPE, S.[ST_STATUS]
             FROM ({msa_with_rdc}) M
             INNER JOIN ({stores_sql}) S ON 1=1 {msa_rdc_join}
             WHERE NOT EXISTS (
                 SELECT 1 FROM [{LISTING_TABLE}] E
                 WHERE E.[WERKS] = S.[ST_CD]
                   AND E.[MAJ_CAT] = M.[MAJ_CAT]
                   AND E.[GEN_ART_NUMBER] = M.[GEN_ART_NUMBER]
                   AND E.[CLR] = M.[CLR]
             )
-        """)
+        """, _msa_pool_params or None)
```

---

## 9. Dossier update (also commit)

Append one bullet under `## Recorded rules` in `frontend/public/docs/manual/listing.md`:

> **2026-10-09** — DISTINCT MSA options now pre-filtered by RUN POOL + `FNL_Q > 0` (`listing.py`, just below the `msa_rdc_filter` block, applied to both `msa_sql` diagnostic and `msa_with_rdc` Part 2 insert). The pool pre-filter from FS-02 (`FNL_Q > 0` gate in `_create_alloc_working`) is pulled forward: options typed to the other pool or carrying zero pool-net stock no longer fan across every store as untouchable `IS_NEW=1` phantoms in Part 2. Guarded on column presence (`ALLOC_TYPE` and `FNL_Q` must exist on `req.msa_table`), so legacy MSA shapes stay byte-identical. **Semantic no-op** — the same rows are dropped downstream by the `_create_alloc_working` pre-filter; this just moves the gate earlier. Measured on live rerun (`20261009_232448_185` vs. hung `20261009_161826_077`): `msa_options` 103,654 → 9,188 (−91.1%), Part 2 rows 38.8M → 3.1M (−91.9%), Part 2 INSERT 98s → 9s, Part 2.5 index 61s → 6s. Diagnostic log suffix `(pool=<FRESH|GRT>, FNL_Q>0)` added when the filter is active. Part 1 untouched. No engine/math change. Owned by `ars_flow`.
