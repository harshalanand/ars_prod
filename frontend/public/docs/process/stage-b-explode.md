# Stage B — Explode Listed OPTs to Variant × Size

> **Where we are:** Stage A wrote `ARS_LISTED_OPT` (one row per qualified OPT). Stage B "explodes" each OPT into one row per **size variant** that has stock, computes the per-size target (`SZ_MBQ`) and requirement (`SZ_REQ`), and writes `ARS_ALLOC_WORKING` — the table the waterfall (Stage C) actually walks.

**Table flow:** `ARS_LISTED_OPT` → **`ARS_ALLOC_WORKING`** (this stage) → waterfall.

**Code:** delegated to `rule_engine_new.py`; dispatch at `rule_engine_pandas.py:544-556`, which runs:

```mermaid
flowchart LR
  B1[B1 explode<br/>OPT × VAR_ART × SZ] --> B2[B2 fill_cont<br/>CONT size-mix]
  B2 --> B3[B3 fill_targets<br/>SZ_MBQ / SZ_REQ]
  B3 --> B4[B4 indexes]
```

---

## B1 — `_stage_b_explode` (`rule_engine_new.py:723-810`)

**What it does (layman):** for each listed OPT, join to its in-stock sizes in `ARS_MSA_VAR_ART`, producing one alloc row per `(WERKS, GEN_ART, CLR, VAR_ART, SZ)`. Initialise all the running accumulators to 0.

**Join (`:784-792`)** — on `MAJ_CAT`, `GEN_ART_NUMBER` (cast BIGINT), `CLR`, `RDC`, all type-normalised to dodge nvarchar/numeric mismatches; `WITH (NOLOCK)`.

**Filters that drop rows here:**
1. `TRY_CAST(V.FNL_Q AS FLOAT) > 0` — only sizes with positive MSA pool (`:793`).
2. **R06 mirror** (`:797-799`): `OPT_TYPE NOT IN (enforced) OR PRI_CT% = 100`.
3. **MJ_REQ gate** (`:805-806`): `ISNULL(MJ_REQ,0) >= 0.5 * ISNULL(NULLIF(ACS_D,0), 18.0)` — note ACS_D defaults to **18.0** here.
4. Optional `OPT_TYPE IN (...)` subset.

**Initialised columns (`:762-781`):** `FNL_Q = FNL_Q_REM = V.FNL_Q`; `CONT/SZ_MBQ/SZ_REQ = NULL`; `SZ_STK = 0`; `POOL_CONSUMED/SHIP_QTY/HOLD_QTY/ALLOC_QTY/ROUND_SHIP/ROUND_HOLD = 0`; `ALLOC_STATUS='PENDING'`.

**Sec-cap propagation:** `mp_cols = _collect_grid_extra_cols(...)` (`:743`) injects the grid extras (`FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`) — continuing the listing→listed→alloc chain.

> **⚠ Change note — ACS_D default mismatch:** the MJ_REQ gate here defaults ACS_D to **18.0** (`:806`), but Stage A's R09 defaults it to **1** (`rule_engine_new.py:371`). A zero-ACS_D OPT survives Stage A cheaply (`0.5×1=0.5`) but faces a steep `0.5×18=9` gate here. If unintended, reconcile.

---

## B2 — `_stage_b_fill_cont` (`rule_engine_new.py:813-842`) — CONT resolution

**What it does (layman):** resolves `CONT` = the share of an OPT's quantity that each size should get (the size curve). Three-level fallback:

```sql
-- LEVEL 1 — store-level (:815): ST_CD = WERKS
UPDATE A SET A.CONT = M.CONT FROM alloc A JOIN Master_CONT_SZ M
  ON M.ST_CD = A.WERKS AND M.MAJ_CAT = A.MAJ_CAT AND M.SZ = A.SZ

-- LEVEL 2 — company-level (:823): ST_CD = literal 'CO', only WHERE A.CONT IS NULL
UPDATE A SET A.CONT = M.CONT FROM alloc A JOIN Master_CONT_SZ M
  ON M.ST_CD = 'CO' AND M.MAJ_CAT = A.MAJ_CAT AND M.SZ = A.SZ
  WHERE A.CONT IS NULL

-- LEVEL 3 — uniform 1/N (:833): WHERE ISNULL(A.CONT,0) = 0
UPDATE A SET A.CONT = ROUND(1.0 / sz_cnt, 4) ...
```

The real key is **`(WERKS/ST_CD, MAJ_CAT, SZ)`** — **NOT** GEN_ART/CLR. Company level is the literal string `ST_CD = 'CO'`. There is **no FNL_Q-ratio fallback** in the live code.

> **⚠ Change notes (high value):**
> - **Stale doc:** `contribution_percentage.md:110-120` describes a GEN_ART / `WERKS IS NULL` fallback for a function `_enrich_size_cont`. The live code keys on `(ST_CD, MAJ_CAT, SZ)` with company = `'CO'` and **no GEN_ART**. If you debug a CONT mismatch from that doc, you'll look at the wrong keys.
> - **CONT=0 gets overwritten:** level-3 uses `WHERE ISNULL(A.CONT,0)=0`, so a `Master_CONT_SZ` row that explicitly stores `CONT=0` is treated as "unset" and replaced by `1/N`. Latent bug if genuine zero-contribution sizes must ship zero.

---

## B3 — `_stage_b_fill_targets` (`rule_engine_new.py:845-884`) — SZ_MBQ / SZ_REQ

**What it does (layman):** split the OPT's `OPT_MBQ` across sizes using `CONT`, then subtract size stock to get the per-size requirement.

**SZ_MBQ with floor-of-1 (`:862-879`):**
```sql
SZ_MBQ = CASE
  WHEN CONT > 0 AND OPT_MBQ > 0 AND ROUND(OPT_MBQ * CONT, 0) = 0
      THEN 1                                  -- floor: never round a real demand to 0
  ELSE ROUND(OPT_MBQ * CONT, 0)
END
SZ_MBQ_WH = (same, with OPT_MBQ_WH)
```

**SZ_REQ (`:880-884`):**
```sql
SZ_REQ    = max(0, SZ_MBQ    - SZ_STK)
SZ_REQ_WH = max(0, SZ_MBQ_WH - SZ_STK)
```

**Worked example** — OPT `1116111940` @ store HN14, `OPT_MBQ = 10`, store-level CONT S=0.10 / M=0.40 / L=0.30, on-hand SZ_STK S=0 / M=2 / L=5:

| SZ | CONT | OPT_MBQ×CONT | ROUND | floor-of-1? | SZ_MBQ | SZ_STK | SZ_REQ |
|---|---|---|---|---|---|---|---|
| S | 0.10 | 1.0 | 1 | no | 1 | 0 | **1** |
| M | 0.40 | 4.0 | 4 | no | 4 | 2 | **2** |
| L | 0.30 | 3.0 | 3 | no | 3 | 5 | **0** (over-stocked) |

Floor-of-1 in action: a tiny size `XS` with CONT=0.03 → `10×0.03=0.3 → ROUND=0`, but CONT>0 and OPT_MBQ>0 → **SZ_MBQ forced to 1**. WH variant: `OPT_MBQ_WH=18`, M → `ROUND(18×0.40)=7`, SZ_REQ_WH(M)=`max(0,7−2)=5`.

> **⚠ Change notes:**
> - `SZ_REQ_WH` subtracts the **store-grain** `SZ_STK`, not a warehouse-grain stock — conflates the two grains if a true WH requirement is ever needed.
> - The SZ_STK pull is "best-effort, silently skip if variant key not obvious" (`:850`) — a renamed key column silently leaves `SZ_STK=0` and **inflates SZ_REQ** with no log. Add a warning.
> - Don't remove the floor-of-1, or thin sizes vanish.

---

## B4 — `_stage_b_indexes` (`rule_engine_new.py:887-913`)

Builds 3 indexes on `ARS_ALLOC_WORKING` for the Stage C walk / pool / MAJ_CAT scans (best-effort, try/except). The clustered `walk` index mirrors the waterfall order `(OPT_TYPE, OPT_PRIORITY_RANK, WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)`. Performance only — safe to tune.

---

## How Stage B output loads into the waterfall

`_load_tables` (`rule_engine_pandas.py:1034-1187`) reads `ARS_ALLOC_WORKING` into a DataFrame with a **deterministic ORDER BY** so pandas tie-breaks are reproducible:

```sql
ORDER BY MAJ_CAT, RDC, GEN_ART_NUMBER, ISNULL(CLR,''), VAR_ART, SZ,
         OPT_PRIORITY_RANK, ISNULL(ST_RANK,999999), WERKS
```

It also: resets `ALLOC_STATUS` to `PENDING` (except `INELIGIBLE`), zeroes accumulators, and seeds `MJ_REQ_REM` from `MJ_REQ` (NULL → falls back to `MJ_REQ`, **not 0** — `:1144-1155`). The waterfall then sorts pool rows by `POOL_KEYS + [OPT_PRIORITY_RANK, _st_rank_fill, WERKS]`, mirroring the SQL exactly.

---

## Change / upgrade summary for Stage B

| # | Finding | Action |
|---|---|---|
| 1 | Stale CONT doc (GEN_ART vs ST_CD keys) | Fix `contribution_percentage.md` |
| 2 | ACS_D default 18 (B1 gate) vs 1 (A R09) | Reconcile to one value / setting |
| 3 | CONT=0 overwritten by 1/N | Use `IS NULL` not `ISNULL(,0)=0` if zero is valid |
| 4 | Silent SZ_STK skip inflates SZ_REQ | Add warning log |
| 5 | `SZ_REQ_WH` uses store stock | Document or split grains |

---

**Next:** [Stage C — The Waterfall](/process/stage-c-waterfall) · **Prev:** [Stage A — Rule & Rank](/process/stage-a-rank)
