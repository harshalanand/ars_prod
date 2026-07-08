# CONT Fallback Ladder — Final Rules (2026-07-02, extended 2026-07-03)

> **2026-07-03 update.** Rules 1–5 below still hold for MAJ_CATs where
> `ARS_GRID_HIERARCHY.SZ_APPLICABLE = 'Y'` (or unknown). For MAJ_CATs where
> `SZ_APPLICABLE = 'N'` (size-agnostic categories like belts, wallets,
> one-size accessories), a new operator-selectable fallback is available —
> see **Rules 6–7** at the bottom. Default is still STRICT (no change to
> production behaviour unless explicitly enabled).


Governs how the rule engine fills the size contribution % (`CONT`) column in
`ARS_ALLOC_WORKING` from `Master_CONT_SZ` during Stage B.

Source: [rule_engine_new.py:815-854](../backend/app/services/rule_engine_new.py#L815-L854)
Function: `_stage_b_fill_cont(conn, alloc_table, cont_table)`
Callers (all four route through the same function):
- `rule_engine_new.py:159` (main SQL)
- `rule_engine_pandas.py:598`
- `rule_engine_parallel_sql.py:361`
- `rule_engine_parallel_python.py:198`
- `rule_engine_per_opt.py` — no independent CONT path, reuses the above

---

## The 5 Rules

### Rule 1 — Lookup grain

`Master_CONT_SZ` joins on **`(ST_CD, MAJ_CAT, SZ)`** only.

- `GEN_ART_NUMBER` is **NOT** part of the key.
- `CLR` is **NOT** part of the key.
- `CONT` is a **MAJ_CAT-level size curve per site**, shared across all articles and colors within that MAJ_CAT at that site.

**Example.** Every article and color inside `M_K_SHIRT_HS` at store `HJ24` sees the same size curve (S=0.1098, M=0.3394, L=0.2548, XL=0.2009, 2XL=0.0951).

---

### Rule 2 — Fallback ladder is exactly 2 steps

| Step | Source | Applies when |
|------|--------|--------------|
| 1 | Site row (`ST_CD = WERKS`) per (MAJ_CAT, SZ) | Always attempted |
| 2 | Corporate default (`ST_CD = 'CO'`) per (MAJ_CAT, SZ) | Only when the site row for that (WERKS, MAJ_CAT) group is **entirely empty** — every SZ ended Step 1 with `CONT = 0` or `NULL` |

There is no Step 3.

**Example — Step 1 hits.**
Site `HJ24 / M_K_SHIRT_HS` has real cont% for S, M, L, XL, 2XL. Step 1 writes those values per size. Step 2 is skipped for the group.

**Example — Step 2 hits.**
Site `AB01 / M_K_SHIRT_HS` has zero rows in `Master_CONT_SZ`. All sizes end Step 1 with `CONT = NULL`. Group is empty → Step 2 fills each SZ from the corresponding `ST_CD='CO'` row.

---

### Rule 3 — No uniform 1/N fallback

The uniform fallback `CONT = ROUND(1.0 / COUNT(DISTINCT SZ), 4)` has been **removed**. Sizes that don't match anywhere keep `CONT = 0`.

Downstream: `SZ_MBQ = ROUND(OPT_MBQ × CONT, 0)` produces 0, and the min-1 guard doesn't rescue it (see Rule 5). The size gets no allocation.

**Example.**
3XL is missing from `HJ24 / M_K_SHIRT_HS` site row. 3XL is present in `'CO' / M_K_SHIRT_HS` but stored as `CONT = 0.0`. Since the site curve for HJ24 has non-zero CONT for other sizes, Step 2 does not run. 3XL stays at `CONT = 0` → `SZ_MBQ = 0` → 3XL gets no allocation.

---

### Rule 4 — Group gate for Step 2

Step 2 is gated by a CTE at the **(WERKS, MAJ_CAT) group** level:

```sql
;WITH EmptyGroups AS (
    SELECT WERKS, MAJ_CAT
    FROM [alloc_table]
    GROUP BY WERKS, MAJ_CAT
    HAVING MAX(ISNULL(CONT, 0)) = 0
)
```

**Interpretation.** If any SZ in the group has `CONT > 0` after Step 1, the entire group is excluded from Step 2. No per-size cherry-picking from the 'CO' curve onto a site that already provides a curve. The site's curve, however incomplete, is treated as the authoritative statement for that (WERKS, MAJ_CAT).

**Example — group has non-zero CONT.**
HJ24 / M_K_SHIRT_HS site row covers 5 of 6 sizes. `MAX(ISNULL(CONT,0))` for the group = 0.3394 > 0 → group is NOT in `EmptyGroups` → Step 2 skips all rows in this group, including the 3XL row that is `NULL`.

**Example — group entirely empty.**
AB01 / M_K_SHIRT_HS site row has zero rows in `Master_CONT_SZ`. Every SZ in the alloc table for this group has `CONT = NULL` after Step 1. `MAX(ISNULL(CONT,0))` = 0 → group IS in `EmptyGroups` → Step 2 fills each SZ from 'CO'.

---

### Rule 5 — SZ_MBQ min-1 guard requires CONT > 0

Location: [rule_engine_new.py:864-881](../backend/app/services/rule_engine_new.py#L864-L881)

```sql
SZ_MBQ = CASE
    WHEN ISNULL(CONT, 0) > 0
         AND ISNULL(OPT_MBQ, 0) > 0
         AND ROUND(ISNULL(OPT_MBQ, 0) * ISNULL(CONT, 0), 0) = 0
        THEN 1
    ELSE ROUND(ISNULL(OPT_MBQ, 0) * ISNULL(CONT, 0), 0)
END
```

The min-1 guard (force `SZ_MBQ = 1` when rounding produces 0) only fires when **both** `CONT > 0` AND `OPT_MBQ > 0`. A size at `CONT = 0` is never lifted to 1 — it stays at 0.

**Example.**
3XL at HJ24 has `CONT = 0` after the new ladder. Even though `OPT_MBQ = 23`, the guard does not fire because `CONT = 0`. `SZ_MBQ = ROUND(23 × 0, 0) = 0`. 3XL is excluded from allocation.

---

## Full Worked Example

**Target OPT.** `WERKS=HJ24 / MAJ_CAT=M_K_SHIRT_HS / GEN_ART=1110114512 / CLR=BRW / OPT_MBQ=23`

**Master_CONT_SZ evidence (verified against live DB).**

Site row (`ST_CD='HJ24'`):

| SZ | CONT |
|----|------|
| S | 0.1098 |
| M | 0.3394 |
| L | 0.2548 |
| XL | 0.2009 |
| 2XL | 0.0951 |
| **3XL** | **(missing)** |
| Σ | 1.0000 |

'CO' row (`ST_CD='CO'`):

| SZ | CONT |
|----|------|
| XS | 0.0000 |
| S | 0.0686 |
| M | 0.2451 |
| L | 0.2941 |
| XL | 0.2549 |
| 2XL | 0.1373 |
| 3XL | 0.0000 |
| A | 0.0000 |

**Ladder walkthrough.**

- Step 1 fills S, M, L, XL, 2XL from HJ24 site row. 3XL stays `NULL`.
- Group check: `MAX(ISNULL(CONT,0))` for HJ24 / M_K_SHIRT_HS = 0.3394 > 0 → group NOT empty.
- Step 2 skipped for this group.
- Rule 3: no uniform fallback exists.
- 3XL final `CONT = 0`.
- Rule 5: `CONT = 0` → min-1 guard does not fire → `SZ_MBQ = 0`.

**Old vs New comparison for this OPT.**

| SZ | Old FINAL | New FINAL | Old SZ_MBQ | New SZ_MBQ | Delta |
|----|-----------|-----------|------------|------------|-------|
| S | 0.1098 | 0.1098 | 3 | 3 | 0 |
| M | 0.3394 | 0.3394 | 8 | 8 | 0 |
| L | 0.2548 | 0.2548 | 6 | 6 | 0 |
| XL | 0.2009 | 0.2009 | 5 | 5 | 0 |
| 2XL | 0.0951 | 0.0951 | 2 | 2 | 0 |
| **3XL** | **0.1667** (uniform) | **0.0000** | **4** | **0** | **−4** |
| SUM | 1.1667 | 1.0000 | 28 | 24 | −4 |

Total distributed moves from 28 (17% over OPT_MBQ) to 24 (rounding of the true curve).

---

## Coverage Matrix

| Engine | CONT-fill path | Affected by this change |
|--------|----------------|-------------------------|
| `rule_engine_new.py` (main SQL) | own `_stage_b_fill_cont` | Yes — direct edit |
| `rule_engine_pandas.py` | delegates to `rne._stage_b_fill_cont` | Yes |
| `rule_engine_parallel_sql.py` | delegates to `rne._stage_b_fill_cont` | Yes |
| `rule_engine_parallel_python.py` | delegates to `rne._stage_b_fill_cont` | Yes |
| `rule_engine_per_opt.py` | no independent CONT logic | Yes (reuses above) |

---

## What the code now looks like

```python
def _stage_b_fill_cont(conn, alloc_table, cont_table):
    # 2-step CONT ladder (uniform 1/N fallback removed 2026-07-02):
    #   Step 1 - Site row (per WERKS+MAJ_CAT+SZ).
    #   Step 2 - 'CO' default, but ONLY for (WERKS, MAJ_CAT) groups whose site
    #            curve is entirely missing/zero. If Step 1 wrote any non-zero
    #            CONT for the group, Step 2 skips it. Sizes that don't match
    #            anywhere keep CONT=0 -> SZ_MBQ=0 -> no allocation.
    if not _exists(conn, cont_table):
        return

    # Step 1 - Site row
    _run(conn, f"""
        UPDATE A SET A.CONT = TRY_CAST(M.CONT AS FLOAT)
        FROM [{alloc_table}] A
        INNER JOIN [{cont_table}] M WITH (NOLOCK)
            ON LTRIM(RTRIM(CAST(M.ST_CD   AS NVARCHAR(50))))  = LTRIM(RTRIM(CAST(A.WERKS AS NVARCHAR(50))))
           AND LTRIM(RTRIM(CAST(M.MAJ_CAT AS NVARCHAR(200)))) = A.MAJ_CAT
           AND LTRIM(RTRIM(CAST(M.SZ      AS NVARCHAR(200)))) = LTRIM(RTRIM(CAST(A.SZ AS NVARCHAR(200))))
    """)

    # Step 2 - 'CO' default, group-gated by "site curve entirely empty".
    _run(conn, f"""
        ;WITH EmptyGroups AS (
            SELECT WERKS, MAJ_CAT
            FROM [{alloc_table}]
            GROUP BY WERKS, MAJ_CAT
            HAVING MAX(ISNULL(CONT, 0)) = 0
        )
        UPDATE A SET A.CONT = TRY_CAST(M.CONT AS FLOAT)
        FROM [{alloc_table}] A
        INNER JOIN EmptyGroups E
            ON A.WERKS = E.WERKS AND A.MAJ_CAT = E.MAJ_CAT
        INNER JOIN [{cont_table}] M WITH (NOLOCK)
            ON LTRIM(RTRIM(CAST(M.ST_CD AS NVARCHAR(50))))    = 'CO'
           AND LTRIM(RTRIM(CAST(M.MAJ_CAT AS NVARCHAR(200)))) = A.MAJ_CAT
           AND LTRIM(RTRIM(CAST(M.SZ AS NVARCHAR(200))))      = LTRIM(RTRIM(CAST(A.SZ AS NVARCHAR(200))))
        WHERE ISNULL(A.CONT, 0) = 0
    """)
```

---

## Rationale

- **Site's curve is authoritative.** If a store has explicitly listed cont% for its size range, that's the buyer's intent for that store. Filling missing sizes from a corporate default overrides that intent for sizes the buyer chose not to stock.
- **Uniform 1/N over-allocates.** In the example above, HJ24's site curve already sums to 1.0000. Adding 0.1667 for 3XL via uniform fallback inflates the effective distribution to 1.1667 (17% overshoot vs. OPT_MBQ).
- **Missing = intentional.** A size absent from `Master_CONT_SZ` for a store is treated as "not stocked at this store", not as "unknown, please guess".
- **CO is a true fallback.** Corporate default is only used when the store has provided no curve at all — the traditional "no data, use defaults" semantics.

---

## Rule 6 — SZ_APPLICABLE='N' Fallback (2026-07-03, revised same day)

For MAJ_CATs where `ARS_GRID_HIERARCHY.SZ_APPLICABLE = 'N'` (size-agnostic categories: belts, wallets, one-size accessories, cosmetics, etc.), a fallback ALWAYS fires after Steps 1 + 2 so that these MAJ_CATs never leave 100% of dispatch unallocated. The operator picks the split method from the Listing page radio, but the "no fallback" option was removed — a one-size product with no Master_CONT_SZ curve should not be silently dropped from allocation.

| Mode | What runs after Steps 1 + 2 | Applied to rows |
|------|------------------------------|-----------------|
| `P4_UNIFORM` (**default**) | `CONT = ROUND(1.0 / COUNT(DISTINCT SZ per OPT), 4)` | Only rows where `SZ_APPLICABLE='N'` AND `CONT` still `0` after Steps 1 + 2 |
| `P3_FNL_Q` | `CONT = ROUND(FNL_Q / SUM(FNL_Q per OPT), 4)` | Only rows where `SZ_APPLICABLE='N'` AND `CONT` still `0` after Steps 1 + 2, AND `SUM(FNL_Q) > 0` |
| `STRICT` | *(deprecated)* — skip P3/P4 entirely | Handled backend-side for API back-compat only; UI no longer exposes this option |

Location: `_stage_b_fill_cont(conn, alloc_table, cont_table, mode)` in [rule_engine_new.py](../backend/app/services/rule_engine_new.py). All four allocator entry points (`rule_engine_new`, `rule_engine_pandas`, `rule_engine_parallel_sql`, `rule_engine_parallel_python`) accept and forward `cont_fallback_mode`. `rule_engine_per_opt` reuses the same function via delegation.

**MAJ_CATs where `SZ_APPLICABLE='Y'` or NULL (missing from ARS_GRID_HIERARCHY) are never touched by P3 or P4.** Rule 3 above still holds for them — the 2026-07-02 semantics.

**Option Y (chosen 2026-07-03):** if the operator selected `P3_FNL_Q` and an OPT has `SUM(FNL_Q) = 0`, P3 does not fire for that OPT (no fallback to P4). Rows stay at `CONT = 0`.

---

## Rule 7 — Renormalize to Σ=1 (never overshoot)

When Rule 6 fires (i.e. mode is `P3_FNL_Q` or `P4_UNIFORM`), a final UPDATE renormalizes CONT within each SZ_APPLICABLE='N' OPT group so `SUM(CONT) = 1.0000`.

```sql
;WITH OptSum AS (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
           SUM(ISNULL(CONT, 0)) AS cont_sum
    FROM [alloc_table] A
    LEFT JOIN ARS_GRID_HIERARCHY GH ON GH.MAJ_CAT = A.MAJ_CAT
    WHERE ISNULL(GH.SZ_APPLICABLE, 'Y') = 'N'
    GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
    HAVING SUM(ISNULL(CONT, 0)) > 0
       AND ABS(SUM(ISNULL(CONT, 0)) - 1.0) > 0.0001
)
UPDATE A SET A.CONT = ROUND(A.CONT / OS.cont_sum, 4) FROM ... INNER JOIN OptSum OS ...
```

**Guarantees total dispatch never exceeds OPT_MBQ.** For OPTs where Step 1 (site) partially covered and P3/P4 added the rest, existing site CONT values are scaled down proportionally. For OPTs where Steps 1 + 2 covered nothing (typical for a truly one-size MAJ_CAT with no Master_CONT_SZ row), P3/P4 already produces `Σ = 1.0` by construction, so the renormalize is a no-op.

The `ABS(... - 1.0) > 0.0001` filter avoids touching groups that already sum to 1.0000 exactly.

---

## Worked example — Rule 6 + Rule 7 in action

MAJ_CAT `FW_M_BELT` (SZ_APPLICABLE='N'), OPT `HB11 / GEN_ART=X / CLR=Y`, `OPT_MBQ=20`, 4 sizes, no rows in Master_CONT_SZ for this MAJ_CAT.

| Step | S | M | L | XL | Σ |
|------|---|---|---|----|---|
| After Step 1 (site) | 0 | 0 | 0 | 0 | 0.00 |
| After Step 2 (CO)   | 0 | 0 | 0 | 0 | 0.00 |
| After Rule 6 (P4_UNIFORM) | 0.25 | 0.25 | 0.25 | 0.25 | 1.00 ✅ |
| After Rule 7 (renormalize — no-op, already 1.00) | 0.25 | 0.25 | 0.25 | 0.25 | 1.00 |
| **SZ_MBQ = 20 × CONT** | **5** | **5** | **5** | **5** | **20 = OPT_MBQ** |

Contrast: MAJ_CAT `M_K_SHIRT_HS` (SZ_APPLICABLE='Y'), 3XL missing — Rule 6 is skipped by the `SZ_APPLICABLE = 'N'` gate, 3XL stays at 0, Rule 3's original semantics preserved.

---

## UI

Listing page → settings panel → **Sizing** group → **SZ='N' fill** radio (2 options: `Uniform 1/N`, `Based on Stock`). Persisted in `AppSettings` as `listing.cont_fallback_mode`. Default `P4_UNIFORM`. Every run is audited into `ARS_RUN_PARAMS_AUDIT` under `(ALLOCATION, cont_fallback_mode)` so historic runs can be traced back to the mode active at the time.
