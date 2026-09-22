# Central RDC Pool — Suggested Implementation Plan

**Version:** 1.0
**Date:** 2026-09-21
**Status:** proposal — no code written
**Companion to:** `2026-09-19-central-rdc-pool-allocation-brd-fsd.md` (BRD/FSD v1.2)
**Purpose of this document:** the *how*. The BRD/FSD says what the business wants; this
says which tables to create, which screens to build, which lines to touch, and in what order.

---

## 0. The two rules that govern every decision below

| # | Rule | Enforcement |
|---|---|---|
| **G1** | **`Own` and `Cross` produce byte-identical output.** Not "equivalent" — identical. | Every gate written positively as `rdc_mode == 'all' AND rule_active`. **Never** an `else` on the existing `== 'own'` test, because today's `else` branch covers `all` *and* `cross`. |
| **G2** | **One switch turns the whole feature off.** | Business rule `ALC_RDC_CENTRAL_POOL`, default **INACTIVE**. While inactive, `rdc_mode='all'` behaves exactly as it does today. Required because `ListingPage.jsx:756` already defaults `rdcMode` to `'all'` — without the switch, shipping this changes the default behaviour for every user on day one. |

---

# PART 1 — TABLES

Two new tables, two amended tables, zero primary-key migrations.

House style: idempotent inline `ensure_*_table(conn)` DDL, matching
`pend_alc_service.ensure_pend_alc_table` and `business_rules.ensure_tables`.
No Alembic — `backend/scripts/migrations/` holds only ad-hoc SQL.

## 1.1 NEW — `ARS_RDC_FALLBACK` (master, user-maintained)

Answers one question: *"a store's own warehouse is short — which warehouse do we try next?"*

This replaces the `ALC_RDC_PRIORITY` JSON business rule proposed in FSD §B6.4.1.
A real table is better here because:

- it is **per-RDC geography**, which a single global string cannot express;
- it is **N rows for N warehouses** (4 RDCs → ~12 rows), not 451 rows of per-store master;
- it gets a proper maintenance screen, validation and change audit instead of hand-edited JSON;
- the `'*'` row folds the *global* fallback (for untagged stores) into the **same table**, so
  there is exactly one place to look.

```sql
IF OBJECT_ID('dbo.ARS_RDC_FALLBACK','U') IS NULL
CREATE TABLE dbo.ARS_RDC_FALLBACK (
    ID            INT IDENTITY(1,1) NOT NULL,
    OWN_RDC       NVARCHAR(20)   NOT NULL,   -- store's own RDC; '*' = order for UNTAGGED stores
    FALLBACK_RDC  NVARCHAR(20)   NOT NULL,   -- warehouse to try
    PRIORITY      INT            NOT NULL,   -- 1 = first choice AFTER own
    IS_ACTIVE     BIT            NOT NULL DEFAULT 1,
    TRANSIT_DAYS  FLOAT          NULL,       -- informational; phase-2 freight guardrail
    NOTE          NVARCHAR(200)  NULL,
    UPDATED_BY    NVARCHAR(128)  NULL,
    UPDATED_AT    DATETIME2      NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT PK_ARS_RDC_FALLBACK        PRIMARY KEY CLUSTERED (ID),
    CONSTRAINT UQ_ARS_RDC_FALLBACK_PAIR   UNIQUE (OWN_RDC, FALLBACK_RDC),
    CONSTRAINT UQ_ARS_RDC_FALLBACK_ORDER  UNIQUE (OWN_RDC, PRIORITY),
    CONSTRAINT CK_ARS_RDC_FALLBACK_SELF   CHECK  (OWN_RDC <> FALLBACK_RDC)
);

IF OBJECT_ID('dbo.ARS_RDC_FALLBACK_LOG','U') IS NULL
CREATE TABLE dbo.ARS_RDC_FALLBACK_LOG (
    ID         INT IDENTITY(1,1) PRIMARY KEY,
    OWN_RDC    NVARCHAR(20)  NOT NULL,
    OLD_ORDER  NVARCHAR(400) NULL,           -- 'B>C>D'
    NEW_ORDER  NVARCHAR(400) NULL,
    NOTE       NVARCHAR(400) NULL,
    CHANGED_BY NVARCHAR(128) NULL,
    CHANGED_AT DATETIME2     NOT NULL DEFAULT SYSDATETIME(),
    INDEX IX_ARS_RDC_FALLBACK_LOG (OWN_RDC, CHANGED_AT DESC)
);
```

### Example content (4 warehouses)

| OWN_RDC | FALLBACK_RDC | PRIORITY | TRANSIT_DAYS | NOTE |
|---|---|---|---|---|
| A | B | 1 | 1.5 | same corridor |
| A | C | 2 | 3.0 | |
| A | D | 3 | 4.5 | |
| B | A | 1 | 1.5 | |
| B | D | 2 | 2.0 | |
| B | C | 3 | 5.0 | |
| C | D | 1 | 1.0 | |
| C | A | 2 | 3.0 | |
| C | B | 3 | 5.0 | |
| D | C | 1 | 1.0 | |
| D | B | 2 | 2.0 | |
| D | A | 3 | 4.5 | |
| `*` | A | 1 | | untagged stores: largest hub first |
| `*` | B | 2 | | |
| `*` | C | 3 | | |
| `*` | D | 4 | | |

**Today with 2 RDCs, seed only the two `'*'` rows.** With two warehouses the order after
"own" is forced (there is only one other), so per-RDC rows can express nothing. Populate the
per-RDC rows the day the third RDC goes live.

### Resolution — `preference_order(store)`

```
own = store's RDC tag (from Master_ALC_INPUT_ST_MASTER)

if own is a live RDC code:                       # TIER 1
    order = [own] + [r.FALLBACK_RDC for r in ARS_RDC_FALLBACK
                     where OWN_RDC = own and IS_ACTIVE = 1
                     order by PRIORITY]
    PREF_TIER = '1F' if any rows found else '1'

elif rows exist for OWN_RDC = '*':               # TIER 2  (blank / 'ALL' / unknown tag)
    order = [r.FALLBACK_RDC ... where OWN_RDC = '*' ...]
    PREF_TIER = '2'

else:                                            # TIER 3  (table empty)
    order = all RDCs sorted by available qty desc, per line
    PREF_TIER = '3'

# in every tier: any live RDC missing from `order` is APPENDED alphabetically.
# The table is a PREFERENCE, never a FILTER — no warehouse is excluded by omission.
```

### Maintenance rules

| Situation | Behaviour |
|---|---|
| RDC absent from a store's list | appended at the end, alphabetically |
| No rows at all for an own RDC | falls through to `'*'`, then to tier 3 |
| Row names a decommissioned RDC | ignored — it simply holds no stock |
| New RDC created, no rows added | appended alphabetically — **never silently excluded** |
| `IS_ACTIVE = 0` | row skipped, gap in PRIORITY is fine |
| Rows exist only for `'*'` | every tagged store is own-first then `'*'` order (`PREF_TIER='1'`) |

## 1.2 NEW — `ARS_ALLOC_RDC_SPLIT` (child of the allocation)

One row per **allocation line × source RDC**. Written for *every* line, including
single-source ones, so the picking query is a single clean `GROUP BY`.

A split line must **not** create a second row in `ARS_ALLOC_WORKING` — that table's
one-row-per-`(WERKS, option, VAR_ART, SZ)` grain is relied on by parking, Approve,
alloc review and reporting. Hence a child table.

```sql
IF OBJECT_ID('dbo.ARS_ALLOC_RDC_SPLIT','U') IS NULL
CREATE TABLE dbo.ARS_ALLOC_RDC_SPLIT (
    SESSION_ID      NVARCHAR(50)   NOT NULL,
    WERKS           NVARCHAR(50)   NOT NULL,   -- DESTINATION store
    MAJ_CAT         NVARCHAR(200)  NULL,
    GEN_ART_NUMBER  BIGINT         NULL,
    CLR             NVARCHAR(200)  NULL,
    VAR_ART         BIGINT         NOT NULL,
    SZ              NVARCHAR(50)   NOT NULL,
    SRC_RDC         NVARCHAR(20)   NOT NULL,   -- SOURCING warehouse (ships the box)
    SHIP_QTY        FLOAT          NOT NULL DEFAULT 0,
    HOLD_QTY        FLOAT          NOT NULL DEFAULT 0,
    ALLOC_TYPE      NVARCHAR(10)   NOT NULL DEFAULT '',   -- FRESH / GRT
    STORE_RDC       NVARCHAR(20)   NULL,       -- store's own tag, '' if untagged (audit)
    PREF_TIER       NVARCHAR(2)    NULL,       -- 1 | 1F | 2 | 3
    IS_CROSS        BIT            NOT NULL DEFAULT 0,    -- SRC_RDC <> STORE_RDC
    CREATED_AT      DATETIME       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT PK_ARS_ALLOC_RDC_SPLIT PRIMARY KEY CLUSTERED
        (SESSION_ID, WERKS, VAR_ART, SZ, SRC_RDC, ALLOC_TYPE)
);

CREATE INDEX IX_ARS_ALLOC_RDC_SPLIT_PICK
    ON dbo.ARS_ALLOC_RDC_SPLIT (SESSION_ID, SRC_RDC) INCLUDE (SHIP_QTY, HOLD_QTY);
CREATE INDEX IX_ARS_ALLOC_RDC_SPLIT_PEND
    ON dbo.ARS_ALLOC_RDC_SPLIT (SESSION_ID, WERKS, VAR_ART, SZ);
```

**Why `ALLOC_TYPE` is in the PK:** mirrors the Fresh/GRT widening already applied to
`ARS_NL_TBL_HOLD_TRACKING` (`PK = WERKS, VAR_ART, SZ, ALLOC_TYPE`). A store-size can carry
one FRESH and one GRT line in the same session.

**`IX_..._PEND` exists specifically** to make the `write_pend_alc` join (§2.4) cheap.

### Lifecycle — the FSD says `parked_history.py` needs no change. It does.

| Event | Action on `ARS_ALLOC_RDC_SPLIT` | Where |
|---|---|---|
| Generate | rows written by Part 8.37 | `listing.py` |
| Park (8.4) | **nothing** — rows are already `SESSION_ID`-keyed and durable | — |
| Approve | **nothing** — rows stay as the picking record of the approved run | — |
| Reject | `DELETE WHERE SESSION_ID = :sid` | `parked_history.reject_parked` |
| Revert approval | `DELETE WHERE SESSION_ID = :sid` | `parked_history.revert_approved_to_parked` |
| Purge | TTL sweep on `CREATED_AT`, same window as history | `parked_history.purge_old_history` |
| Post-run sweep | add to `_AFFECTED_TABLES` | `parked_history.py:98` |

## 1.3 AMENDED tables — additive columns only

| Table | Change | `Own` impact |
|---|---|---|
| `ARS_ALLOC_WORKING` | `+ SRC_RDC NVARCHAR(20) NULL`, `+ SRC_SPLIT_CNT INT NULL` | NULL under Own. Existing `RDC` column **keeps meaning "store's RDC"** — unchanged |
| `ARS_ALLOC_PARKED` / `ARS_ALLOC_HISTORY` | inherit both via existing column reconcile | none — automatic |
| `ARS_NL_TBL_HOLD_TRACKING` | **no DDL.** Existing `RDC` column starts carrying the *sourcing* RDC under All RDCs | none — under Own, source == own |
| `ARS_PEND_ALC` | **no DDL.** `RDC` populated from `SRC_RDC` when a split row exists | none — `ISNULL` falls back to today's expression |
| `ARS_RUN_PARAMS_AUDIT` | new rows: `central_pool_active`, `rdc_split_policy`, `rdc_max_split`, `untagged_store_count` | additive |
| `ARS_LISTING` / `ARS_LISTING_WORKING` | **no change** | none |

## 1.4 Business rules to seed (`ARS_BUSINESS_RULES`)

| `rule_key` | `value_type` | default | `is_active` | Purpose |
|---|---|---|---|---|
| **`ALC_RDC_CENTRAL_POOL`** | flag | — | **0** | **G2 master kill switch** |
| `ALC_RDC_SPLIT_POLICY` | choice | `SINGLE_PREFERRED` | 1 | `SPLIT_ALWAYS` / `SINGLE_PREFERRED` / `SINGLE_STRICT` |
| `ALC_RDC_MAX_SPLIT` | number | `2` | 1 | max source RDCs per SHIP line; `1` ≡ `SINGLE_STRICT` |
| **`ALC_RDC_HOLD_SPLIT`** | flag | — | **0** | **holds never split — see §3.3** |
| `ALC_MIN_CROSS_SHIP_QTY` | number | — | 0 | reserved, phase 4 freight guardrail |

`ALC_RDC_PRIORITY` and `ALC_RDC_FALLBACK_ORDER` from FSD v1.2 are **dropped** — replaced by
`ARS_RDC_FALLBACK` (§1.1).

---

# PART 2 — BACKEND CHANGES

Seven files. Line anchors are current as of branch `ars_v3`.

## 2.1 `backend/app/api/v1/endpoints/listing.py`

### (a) Clubbed MSA quantities — Part 3.55, lines 1630-1700

**Today's defect (D-1):** `_rdc_join` is gated on `rdc_mode=='own'`, but `_rdc_select` and
`_rdc_group` group by RDC **unconditionally**. In `all` mode the subquery therefore returns one
row per RDC while the join matches on option only → `UPDATE … FROM` picks **one arbitrarily**.
Neither summed nor own-preferred.

**Dropping only the join does not fix it** — the fan-out remains. All three fragments must move
together:

```python
_club = _central_pool(req, conn)          # rdc_mode=='all' AND ALC_RDC_CENTRAL_POOL active

_rdc_select = "" if _club else f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50)))) AS MSA_RDC"
_rdc_group  = "" if _club else f", LTRIM(RTRIM(CAST([{msa_rdc_col}] AS NVARCHAR(50))))"
_rdc_join   = "" if _club else ("AND L.[RDC] = M.[MSA_RDC]" if _has_msa_rdc and req.rdc_mode == "own" else "")
```

Same shape for `vrdc_select` / `vrdc_group` / `vrdc_join` at lines 1668-1670
(`VAR_COUNT`, `VAR_FNL_COUNT`).

`VAR_FNL_COUNT` must be counted on the **clubbed** figure, not summed per RDC — a size held at
two RDCs is still one size, and this value drives the R07 TBL size-coverage gate.

> **Note the false branch reproduces today's string exactly**, including the `== "own"` test.
> Cross keeps its current (no-join) behaviour untouched — G1.

### (b) Part 3.54 hold read-back — **NO CHANGE**

FSD v1.2 FS-RDC-02 asks for a fix here and says *"Own keeps the per-RDC lookup"*.
**There is no per-RDC lookup.** Line 1586 already reads
`GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR` with no RDC predicate in any mode.

Implementing FS-RDC-02 literally would **add** an RDC filter to the Own branch — a behaviour
change in the one mode that must not change. **FS-RDC-02 is deleted from the plan.**

### (c) NEW Part 8.37 — the RDC split pass

Insert between line 3423 (end of Part 8.36 run-date stamp) and line 3425 (Part 8.4 parking),
so parked → history → Approve inherit `SRC_RDC` with no extra code.

Skipped entirely unless `_central_pool(req, conn)`.
Algorithm in §3.

### (d) Cockpit summary — line 5113 `by_maj_cat_rdc`

**Add** a *pick by `SRC_RDC`* block; **keep** the existing *demand by store's RDC* block.
Replacing rather than adding would silently change a number ops already reads every day.

### (e) `GenerateRequest` — line 80

```python
rdc_split_policy: str = "SINGLE_PREFERRED"   # SPLIT_ALWAYS | SINGLE_PREFERRED | SINGLE_STRICT
rdc_max_split:    int = 2
```
Validator rejects anything else. Both ignored unless `rdc_mode == 'all'`.

## 2.2 `backend/app/services/rule_engine_new.py`

| Line | Change |
|---|---|
| **873-874** | Drop `AND LTRIM(RTRIM(CAST(L.RDC …))) = LTRIM(RTRIM(CAST(V.[RDC] …)))` from the pool-build join, and `GROUP BY` without RDC — **only when clubbing**. This is also what fixes **D-2**: today a store with a blank or `'ALL'` tag matches no `V.RDC` and drops out of the allocation entirely, with no error. |
| **3321-3366** | `RDC_FNL_Q_REM_LIVE` becomes the *clubbed* residual. Acceptable for `NO_POOL_MSA` diagnostics. **Do not** use it for the residual-by-warehouse report — derive that from the split ledger instead (§2.7). |

## 2.3 `backend/app/services/rule_engine_per_opt.py`

Line 63:
```python
POOL_KEYS = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]   # today
POOL_KEYS = [       "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]   # when clubbing
```
Passed in from the orchestrator rather than hard-coded, so Own keeps the 6-key list.

**Nothing else in the engine changes.** The waterfall, eligibility gates, MBQ / MJ_REQ /
secondary-grid caps, `I_ROD` rounds, pack-size rounding, store ranking, dispatch mode and
hold-release retry all operate on the clubbed pool exactly as they operate on a per-RDC pool.

## 2.4 `backend/app/services/pend_alc_service.py` — line 2304 🔴

**The gap that decides whether this feature works at all.**

`ARS_PEND_ALC.RDC` is documented in that file as *"source warehouse (where stock ships from);
used by MSA for FNL_Q deduction"* — and it feeds the **BDC / DO picking file**. Today it is
derived from the store master:

```python
rdc_expr = f"ISNULL(M.[{rdc_col}], H.[WERKS])"     # M = Master_ALC_INPUT_ST_MASTER
```

So "source warehouse" is *defined as* the store's own RDC. Under cross-shipping, without a fix:

- the BDC file tells **RDC-A** to pick stock that was allocated out of **RDC-B**;
- next run MSA deducts the pend from **A's** pool (understated) while **B's** pool still shows
  the shipped stock as available → **double-allocation**.

`ARS_ALLOC_RDC_SPLIT` would be a correct report sitting beside an incorrect pipeline.

**Fix — inert for Own by construction:**

```sql
rdc_expr = ISNULL(SPL.SRC_RDC, ISNULL(M.[RDC], H.[WERKS]))

LEFT JOIN (
    SELECT SESSION_ID, WERKS, VAR_ART, SZ, SRC_RDC
    FROM   ARS_ALLOC_RDC_SPLIT
) SPL ON SPL.SESSION_ID = H.SESSION_ID
     AND SPL.WERKS      = H.WERKS
     AND SPL.VAR_ART    = H.VAR_ART
     AND SPL.SZ         = H.SZ
```

Under Own/Cross no split rows exist → `SPL.SRC_RDC` is NULL → the expression is **textually
identical in effect to today**. The pend grain already contains RDC, so a split line naturally
becomes two pend rows and `GROUP BY {rdc_expr}` needs no other change.

## 2.5 `backend/app/services/msa_service.py` 🔴

`_load_open_holds` (line 94) and `_load_typed_hold` (line 211) both resolve the hold's warehouse
through the store master and **ignore `ARS_NL_TBL_HOLD_TRACKING.RDC` entirely**:

```sql
SELECT S.[RDC] ... FROM ARS_NL_TBL_HOLD_TRACKING H
INNER JOIN <store_master> S ON S.ST_CD = H.WERKS
```

So even after the split pass stamps the sourcing RDC on the hold, next run's MSA reserves it
against the **store's own** warehouse — wrong pool debited, wrong pool credited.

**Fix:**
```sql
COALESCE(NULLIF(H.[RDC], ''), S.[{rdc_col}])  AS RDC
```

Legacy rows have NULL `H.RDC` → today's behaviour. Own-mode rows have `H.RDC == S.RDC` →
identical result. `msa_service.py` must be **added to the impact map** — FSD v1.2 omits it.

## 2.6 `backend/app/services/parked_history.py` 🔴

FSD v1.2 says "no change". Two changes are required:

| Line | Change |
|---|---|
| **~1620** | The approve-time hold stamp reads `MAX(SM.[RDC])` from the store master. Must prefer the split's `SRC_RDC`: `COALESCE(MAX(SPL.SRC_RDC), MAX(SM.[RDC]))`. Own: no split rows → unchanged. |
| **98** (`_AFFECTED_TABLES`) + reject / revert / purge | `ARS_ALLOC_RDC_SPLIT` lifecycle per §1.2. Without it, rejecting a run leaves orphan picking rows that look live. |

## 2.7 Part 8.55 hold release — `listing.py:3556`

Part 8.55 runs **after** parking (8.4). When it releases a hold it must also zero
`HOLD_QTY` on the matching `ARS_ALLOC_RDC_SPLIT` rows — otherwise the picking requirement
still reserves stock the run has given back.

## 2.8 Reports (new queries, no new code paths)

```sql
-- RDC-wise picking requirement
SELECT SRC_RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
       SUM(SHIP_QTY) AS PICK_QTY, SUM(HOLD_QTY) AS HOLD_QTY
FROM   ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid
GROUP  BY SRC_RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ;

-- Store dispatch summary (how many documents each store expects)
SELECT WERKS, SRC_RDC, SUM(SHIP_QTY) AS QTY, COUNT(*) AS LINES
FROM   ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid GROUP BY WERKS, SRC_RDC;

-- Cross-ship exposure (freight monitoring)
SELECT STORE_RDC, SRC_RDC, SUM(SHIP_QTY) AS QTY
FROM   ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid AND IS_CROSS = 1
GROUP  BY STORE_RDC, SRC_RDC;

-- Residual by warehouse (use THIS, not RDC_FNL_Q_REM_LIVE)
SELECT V.RDC, SUM(V.FNL_Q) - ISNULL(SUM(S.TAKEN), 0) AS RESIDUAL
FROM   ARS_MSA_VAR_ART V
LEFT   JOIN (SELECT SRC_RDC, VAR_ART, SZ, SUM(SHIP_QTY + HOLD_QTY) AS TAKEN
             FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid
             GROUP BY SRC_RDC, VAR_ART, SZ) S
       ON S.SRC_RDC = V.RDC AND S.VAR_ART = V.ARTICLE_NUMBER AND S.SZ = V.SZ
GROUP  BY V.RDC;
```

---

# PART 3 — THE SPLIT PASS (Part 8.37)

## 3.1 Inputs and ordering

| Input | Source |
|---|---|
| Allocation lines (`SHIP_QTY`, `HOLD_QTY`) | `ARS_ALLOC_WORKING` |
| Per-RDC availability at option-size grain | `ARS_MSA_VAR_ART`, filtered to the run's `ALLOC_TYPE` |
| Store's own RDC | `ARS_LISTING_WORKING.RDC` |
| Preference order | `ARS_RDC_FALLBACK` (§1.1) |
| Policy / cap | run params, defaulted from business rules |

**Processing order:** `ST_RANK`, then the allocation order within a store — identical to the
waterfall, so the result is deterministic and reproducible.

**One ledger.** A single in-memory balance `rdc_avail[RDC, VAR_ART, SZ]`, initialised from MSA,
decremented by **both** SHIP and HOLD as they are tagged. SHIP and HOLD for a line are tagged
consecutively, **SHIP first**. Two independent passes would both see the full balance and
double-book it.

## 3.2 Algorithm

```
MAX_SPLIT = ALC_RDC_MAX_SPLIT                     # default 2

for line in (ST_RANK, alloc order):               # SHIP first, then HOLD
    remaining = qty
    order     = preference_order(store)           # §1.1
    sources   = 0

    if draw == HOLD:                              # §3.3 — holds never split
        policy_here, cap_here = SINGLE_STRICT, 1
    else:
        policy_here, cap_here = policy, MAX_SPLIT

    # pass 1 — whole-line cover
    if policy_here in (SINGLE_PREFERRED, SINGLE_STRICT):
        for rdc in order:
            if rdc_avail[rdc, var, sz] >= remaining:
                emit(rdc, remaining); rdc_avail[rdc] -= remaining
                remaining = 0; sources = 1; break

    # pass 2 — strict: take the biggest single source, reduce the rest
    if remaining > 0 and policy_here == SINGLE_STRICT:
        best = argmax(rdc_avail[rdc, var, sz] for rdc in order)
        take = min(remaining, rdc_avail[best, var, sz])
        if take > 0:
            emit(best, take); rdc_avail[best] -= take; remaining -= take; sources = 1
        reduce_line(remaining,
                    'RDC_HOLD_SHORT' if draw == HOLD else 'RDC_SINGLE_SHORT')
        remaining = 0

    # pass 3 — capped split walk
    if remaining > 0:
        candidates = own_first(order, then rdc_avail desc)[:cap_here]
        for rdc in candidates:
            take = min(remaining, rdc_avail[rdc, var, sz])
            if take > 0:
                emit(rdc, take); rdc_avail[rdc] -= take
                remaining -= take; sources += 1
            if remaining == 0: break
        if remaining > 0:
            reduce_line(remaining, 'RDC_SPLIT_CAPPED')
            remaining = 0

    assert remaining == 0
    assert sources <= cap_here
    assert all(v >= 0 for v in rdc_avail.values())
```

## 3.3 New rule — **BR-RDC-11: a HOLD is never split**

`PK_ARS_NL_TBL_HOLD_TRACKING = (WERKS, VAR_ART, SZ, ALLOC_TYPE)` — **RDC is not in the key**
(`listing.py:3664`). FSD v1.2 D2 permits a hold split across RDCs; that produces two rows with
the same PK → insert fails or one row is silently lost.

Rather than widen a production PK: **a hold is a physical reservation at one warehouse.**
Assembling one from two is meaningless. Holds run `SINGLE_STRICT` with cap 1; if no single RDC
can cover it the hold is reduced and stamped `RDC_HOLD_SHORT`. A hold is advisory, so reducing
it is safe — unlike reducing a shipment.

This removes the need for any PK migration and simplifies worked example E3 in the FSD.

## 3.4 Line reduction — never silent

1. `SHIP_QTY` / `HOLD_QTY` on `ARS_ALLOC_WORKING` reduced to the tagged quantity.
2. `ALLOC_REMARKS` gains `RDC_SINGLE_SHORT(alloc=A,shipped=B)`, `RDC_SPLIT_CAPPED(…)` or
   `RDC_HOLD_SHORT(…)`.
3. Option-grain `ALLOC_QTY` / `HOLD_QTY` rollup on `ARS_LISTING_WORKING` refreshed, so Part 8.5
   `OPT_STATUS` judges the real shipped quantity.
4. The untagged remainder stays unallocated and appears in the residual report.
5. Count and quantity of reduced lines go to the run log **and** the cockpit.

**Under the default configuration (2 RDCs, `SINGLE_PREFERRED`, cap 2) no SHIP quantity can ever
change — the pass is pure tagging.** Only holds can reduce, and only when neither warehouse can
cover one whole.

---

# PART 4 — UI DESIGN

Three screens. Pattern throughout: **progressive disclosure** — new controls appear only in the
mode they apply to.

## 4.1 Listing cockpit — `frontend/src/pages/ListingPage.jsx:2797`

### `Own` — today's screen plus one advisory line

```
┌ RDC Scope ──────────────────────────────┐
│ [ All RDCs ] [ Own ✓ ] [ Cross ]        │  ← existing 3 buttons, untouched
│ Detected:  (A)  (C)                     │  ← existing read-only pills (autoRdcs)
│ ⚠ 12 of 320 selected stores have no RDC │  ← NEW, text only
│   tag — excluded from this run          │
└─────────────────────────────────────────┘
```

`autoRdcs` (line 1275) uses `.filter(Boolean)`, so untagged stores vanish today with **no
message** — the front-end half of defect D-2. The banner never blocks and never alters the
payload.

> The FSD v1.2 mock shows an `RDC [A ×][C ×]` multi-select here. **That control does not
> exist** — RDCs are auto-derived from the selected stores. The mock above is the real screen.

### `Cross` — frozen, advisory banner only

```
┌ RDC Scope ──────────────────────────────┐
│ [ All RDCs ] [ Own ] [ Cross ✓ ]        │
│ Pull from:  [A] [B]                     │  ← existing toggles, untouched
│ ⚠ 12 stores untagged — excluded         │
└─────────────────────────────────────────┘
```

### `All RDCs` — the only mode with new controls

```
┌ RDC Scope ──────────────────────┐  ┌ RDC Sourcing ────────────────────────────┐
│ [ All RDCs ✓] [ Own ] [ Cross ] │  │ Split policy                             │
│                                 │  │  ( ) Split whenever short                │
│ Pool:   A + B  →  one pool      │  │  (•) Single source preferred    default  │
│ Stores: 320   (12 untagged)     │  │  ( ) Strict single source           ⚠    │
│ ⚠ untagged → fallback order A,B │  │      may reduce an allocated line        │
│                                 │  │ ─────────────────────────────────────────│
│ ⓘ Central pool: OFF             │  │ Max split per line   [ 2 ▾ ]             │
│   ALC_RDC_CENTRAL_POOL          │  │ Holds: always single-source  BR-RDC-11   │
│   → behaves as today            │  │ Fallback order:  Settings → RDC Fallback │
└─────────────────────────────────┘  └──────────────────────────────────────────┘
```

New React state: `rdcSplitPolicy` (default `'SINGLE_PREFERRED'`), `rdcMaxSplit` (default `2`).

Payload assembly at line 1489 gains a **third branch only** — the Own and Cross branches are
not touched:

```js
if (rdcMode === 'own')        { payload.rdc_values = autoRdcs }
else if (rdcMode === 'cross') { payload.cross_from = crossFrom; payload.cross_to = autoRdcs }
else if (rdcMode === 'all')   { payload.rdc_split_policy = rdcSplitPolicy
                                payload.rdc_max_split    = rdcMaxSplit }
```

| Control | Type | Behaviour |
|---|---|---|
| RDC Scope | existing segmented buttons | unchanged; `All RDCs` tooltip becomes *"club all RDC stock, allocate, then split RDC-wise"* |
| Live summary | read-only text | pool composition + store count + untagged count, so `All` is visibly different from `Own` **before** Generate |
| Split policy | radio group, 3 | default `Single source preferred`; `Strict` shows the reduction caption |
| Max split per line | select `1 / 2 / 3 …` | default `2`; `1` displays *"equivalent to strict single source"*; clamped to the live RDC count |
| Central-pool status | read-only badge | reads `ALC_RDC_CENTRAL_POOL`; when OFF says *"behaves as today"* so nobody is surprised |
| Untagged banner | warning text, **all** modes | `All`: *"→ sourcing uses the fallback order"* · `Own`/`Cross`: *"→ excluded from this run"* |

Fallback order is **not** a cockpit control — it is standing configuration that applies to every
run, so it lives on its own screen (§4.3) with the SUPER_ADMIN gate and change audit.

## 4.2 Post-run results block — `All RDCs` only

```
┌ RDC Split ────────────────────────────────────────────────────────┐
│ PICK BY SOURCE RDC       A  1,240        B  3,905                 │
│ Demand by store RDC      A  2,100        B  3,045    ← existing   │
│ Cross-shipped            1,106 pcs (17%) to 214 stores            │
│ Split lines                 38 of 8,421 (0.5%)                    │
│ Reduced lines                0   cap / strict / hold-short        │
│ Untagged sourced         2,180 pcs by fallback order              │
│ Residual by RDC          A  1  ·  B  14                           │
│                                              [ Export picklist ]  │
└───────────────────────────────────────────────────────────────────┘
```

Both RDC rows are shown deliberately — *demand by store's RDC* (existing) and *pick by source
RDC* (new) mean different things, and replacing one with the other would silently change a
number ops reads daily.

## 4.3 NEW page — Settings → RDC Fallback

```
frontend/src/pages/RdcFallbackPage.jsx
route   /settings/rdc-fallback   (ProtectedRoute superadminOnly)
sidebar Settings group, below "Business Rules"
```

Registered exactly like `BusinessRulesPage` (`App.jsx:81` lazy import, `App.jsx:262` route,
`Sidebar.jsx:167` nav entry).

```
┌ RDC Fallback Order ───────────────────────────────────────────────────────┐
│ "When a store's own warehouse is short, which warehouse do we try next?"   │
│                                                                            │
│  Own RDC        Fallback order after own            Transit  Actions       │
│ ─────────────────────────────────────────────────────────────────────────  │
│  A         →    [ B ⇅ ] [ C ⇅ ] [ D ⇅ ]              1.5 / 3 / 4.5   ✎ ↺   │
│  B         →    [ A ⇅ ] [ D ⇅ ] [ C ⇅ ]              1.5 / 2 / 5     ✎ ↺   │
│  C         →    [ D ⇅ ] [ A ⇅ ] [ B ⇅ ]              1 / 3 / 5       ✎ ↺   │
│  D         →    [ C ⇅ ] [ B ⇅ ] [ A ⇅ ]              1 / 2 / 4.5     ✎ ↺   │
│ ─────────────────────────────────────────────────────────────────────────  │
│  ✱ untagged →   [ A ⇅ ] [ B ⇅ ] [ C ⇅ ] [ D ⇅ ]                      ✎ ↺   │
│    stores with a blank or 'ALL' RDC tag use this order                     │
│ ─────────────────────────────────────────────────────────────────────────  │
│  ⓘ Only 2 RDCs are live. The order after "own" is forced, so these rows    │
│    change nothing today. Populate them before the 3rd RDC goes live.       │
│                                                                            │
│  [ Reset to alphabetical ]              [ Discard ]      [ Save changes ]  │
└────────────────────────────────────────────────────────────────────────────┘

┌ Change history ───────────────────────────────────────────────────────────┐
│ 2026-09-21 14:02  akash   B :  A>C>D  →  A>D>C   "Nagpur hub opened"      │
└────────────────────────────────────────────────────────────────────────────┘
```

| Element | Behaviour |
|---|---|
| Chip row | drag to reorder; writes `PRIORITY` 1..n on save |
| Own-RDC chip | not shown — "own" is always implicitly first, and `CK_..._SELF` forbids the row |
| Missing RDC | rendered greyed at the end with *"appended automatically"* — a preference, never a filter |
| `✱ untagged` row | `OWN_RDC = '*'`; always present, seeded alphabetically on first load |
| Transit days | optional numeric, informational until phase 4 |
| `↺` | revert one row to alphabetical |
| Save | one transaction per own-RDC; writes `ARS_RDC_FALLBACK_LOG` with old/new order strings |
| 2-RDC notice | shown while `COUNT(DISTINCT RDC) <= 2`, so nobody wastes time curating a no-op |

### API — `backend/app/api/v1/endpoints/rdc_fallback.py` (new)

| Method | Route | Returns |
|---|---|---|
| `GET` | `/api/v1/rdc-fallback` | `[{own_rdc, order:[{rdc, priority, transit_days}], is_complete}]` + live RDC list |
| `PUT` | `/api/v1/rdc-fallback/{own_rdc}` | replace that RDC's order atomically; writes log |
| `GET` | `/api/v1/rdc-fallback/log` | change history |
| `POST` | `/api/v1/rdc-fallback/reset` | reseed alphabetically |

SUPER_ADMIN only, matching `business_rules.py`.

---

# PART 5 — ROUTE MAP

`■` new/changed · `□` unchanged · `×` does not execute

| # | Step | Site | **All RDCs** | **Own** | **Cross** |
|---|---|---|---|---|---|
| 1 | Store pool | `listing.py:1207` | □ all active stores | □ `RDC IN (rdc_values)` | □ `RDC IN (cross_to)` |
| 2 | MSA option filter | `:1230` | □ no RDC filter | □ `IN (rdc_values)` | □ `IN (cross_from)` |
| 3 | Part 1 grid rows | `:1304` | □ `RDC` = store's RDC | □ | □ |
| 4 | Part 2 MSA-only rows | `:1322` | □ no RDC join | □ `M.RDC = S.RDC` | □ no join |
| 5 | Part 3.5 `ACS_D` / `I_ROD` | `:1490` | □ | □ | □ |
| 6 | Part 3.54 `RL_HOLD_QTY` | `:1541` | □ **already RDC-blind — no change** | □ | □ |
| 7 | Part 3.55 `MSA_FNL_Q`, `VAR_*` | `:1630-1700` | ■ §2.1a club across RDCs | □ per-RDC join kept | □ untouched |
| 8 | Part 3.6 OPT_TYPE | `:1705` | □ same code, clubbed input | □ | □ |
| 9 | Parts 4-7 grid, ranking, `ELIG_FLAG`, growth | | □ | □ | □ |
| 10 | Pool build join | `rule_engine_new.py:873` | ■ §2.2 drop `L.RDC = V.RDC` | □ | □ |
| 11 | `POOL_KEYS` | `rule_engine_per_opt.py:63` | ■ 5-key | □ 6-key | □ 6-key |
| 12 | Waterfall, caps, MBQ, `I_ROD`, rounding, dispatch | | □ **logic byte-identical** | □ | □ |
| 13 | Part 8.35 / 8.36 stamps | `:3360-3423` | □ | □ | □ |
| 14 | **Part 8.37 split pass** | **new `:3424`** | ■ §3 | × | × |
| 15 | Part 8.4 park | `:3425` | □ inherits `SRC_RDC` free | □ | □ |
| 16 | Part 8.5 `OPT_STATUS` | `:3469` | □ reads refreshed rollup | □ | □ |
| 17 | Part 8.55 hold release | `:3556` | ■ §2.7 cascade to split rows | □ | □ |
| 18 | Part 8.6 hold-tracking DDL | `:3633` | □ no DDL change | □ | □ |
| 19 | Cockpit summary | `:5113` | ■ §2.1d add pick-by-source | □ | □ |
| **— approve / downstream: missing from FSD v1.2 —** |
| 20 | Hold RDC stamp at approve | `parked_history.py:1620` | ■ §2.6 prefer `SRC_RDC` | □ | □ |
| 21 | Split-table lifecycle | `parked_history.py` | ■ §1.2 | × | × |
| 22 | `write_pend_alc` → `ARS_PEND_ALC.RDC` | `pend_alc_service.py:2304` | ■ §2.4 🔴 | □ NULL → today | □ same |
| 23 | BDC / DO picking file | `bdc.py` | ■ correct via #22 — **no code change** | □ | □ |
| 24 | Next-run MSA pend deduction | `msa_service.py` | ■ correct via #22 | □ | □ |
| 25 | Next-run MSA hold deduction | `msa_service.py:94, :211` | ■ §2.5 🔴 | □ identical | □ |
| 26 | Listing cockpit | `ListingPage.jsx:2797` | ■ §4.1 sourcing panel | □ + banner | □ + banner |
| 27 | Settings → RDC Fallback | `RdcFallbackPage.jsx` **new** | ■ §4.3 | n/a | n/a |

---

# PART 6 — OWN-MODE PROTECTION

One helper guards all twelve `■` sites:

```python
def _central_pool(req, conn) -> bool:
    return req.rdc_mode == "all" and _rule_active(conn, "ALC_RDC_CENTRAL_POOL")
```

| Guarantee | Mechanism |
|---|---|
| No `else` branch touches Own or Cross | Positive gate only; grep check on `rdc_mode ==` in review |
| Own SQL text byte-identical | False branch retains today's exact strings, including the `== "own"` sub-test |
| Part 8.37 does not run | Early return |
| Downstream fixes degrade to today | `ISNULL` / `COALESCE` with the current expression as fallback — **provable by inspection, not only by test** |
| One-switch abort | `ALC_RDC_CENTRAL_POOL` inactive ⇒ even `rdc_mode='all'` behaves as today |

## Release gate — widened

FSD v1.2's V5 diffs only `ARS_ALLOC_WORKING`. That misses exactly the three tables §2.4-2.6
touch. **V5 must diff four tables:**

`ARS_ALLOC_WORKING` · `ARS_ALLOC_HISTORY` · `ARS_PEND_ALC` · `ARS_NL_TBL_HOLD_TRACKING`

## Verification plan

| # | Check | Gate |
|---|---|---|
| V3 | No RDC over-drawn: `Σ(SHIP+HOLD)` per `(SRC_RDC, VAR_ART, SZ)` ≤ that RDC's MSA `FNL_Q` | **blocking** |
| V4 | Conservation: `Σ split.SHIP = Σ working.SHIP`, same for HOLD | **blocking** |
| V5 | Own unchanged — 4-table row-for-row diff vs parked snapshot | **blocking** |
| V6 | Untagged fallback: same session with tags blanked → identical quantities, `PREF_TIER='2'` | **blocking** |
| V9 | Split cap: N=4 fixture, line needs 3 sources → capped at 2, remainder reduced + stamped | **blocking** |
| V11 | `ARS_RDC_FALLBACK`: full order honoured (`1F`); partial appends the missing RDC; empty table → tier 3; `'*'` row drives untagged | **blocking** |
| **V12** | **Own run with `ALC_RDC_CENTRAL_POOL` ON vs OFF → identical output** | **blocking** |
| **V13** | **Cross run, same** | **blocking** |
| **V14** | **Cross-ship pend lands on `SRC_RDC` in `ARS_PEND_ALC`, and the BDC file names that warehouse** | **blocking** |
| **V15** | **Cross-RDC hold deducts from the sourcing RDC in next run's MSA** | **blocking** |
| V2 | Fill-rate improvement — live parallel run, both modes | business sign-off |
| V7 | `SPLIT_ALWAYS` vs `SINGLE_PREFERRED` → identical per-RDC totals | regression |
| V8 | Determinism: run the pass twice → identical `SRC_RDC` | regression |
| V10 | Cap cannot bind at N=2 → zero reduced SHIP lines | regression |

Unit tests alongside `backend/tests/test_alloc_round_stamp_per_opt.py`.

---

# PART 7 — BUILD ORDER

| Phase | Tasks | Exit criteria |
|---|---|---|
| **0 — Foundation** | `ARS_RDC_FALLBACK` + `_LOG` DDL & seed · `ARS_ALLOC_RDC_SPLIT` DDL · `ARS_ALLOC_WORKING` columns · 5 business rules seeded, `ALC_RDC_CENTRAL_POOL` **OFF** · `rdc_fallback.py` API · `RdcFallbackPage.jsx` | Fallback page saves and audits. **No allocation behaviour changed at all** — safe to deploy on its own |
| **1 — Engine** | §2.1a club MSA · §2.2 pool join · §2.3 `POOL_KEYS` · §2.1c Part 8.37 · §2.7 hold-release cascade | V3, V4, V5, V12, V13 pass; unit tests green |
| **2 — Downstream** 🔴 | §2.4 `write_pend_alc` · §2.5 `msa_service` holds · §2.6 `parked_history` | V14, V15 pass. **Feature is not usable before this phase** |
| **3 — UI + reports** | §4.1 sourcing panel · §4.2 results block · §2.1d cockpit summary · §2.8 four reports | Ops can read the picking requirement end-to-end |
| **4 — Parallel run** | Same live session in `Own` and `All RDCs`, switch still OFF for routine runs | V2 delta accepted by management; warehouse confirms it can execute a 2-source line |
| **5 — Adopt** | `ALC_RDC_CENTRAL_POOL` **ON** | `Own` retained for single-RDC operations |
| **6 — Optional** | `ALC_MIN_CROSS_SHIP_QTY` freight guardrail | freight data shows uneconomic small movements |

**Phase 2 is not optional and not deferrable.** Without it the allocation is right and the
shipment is wrong — the BDC file names the store's own warehouse regardless of where the stock
was actually taken from.

## Post-implementation (per `CLAUDE.md`)

1. Update `frontend/public/docs/manual/listing.md` — FSD section + `## Recorded rules`.
2. Keep `ARS_DATA_DICTIONARY` in sync with the two new tables and four new columns.
3. Re-run manual screenshots (`tools/manual/REFRESH.md`) — the cockpit and the new settings page.
4. Update the extracts in `.claude/agents/ars_flow_kb/` and `docs/RULE_MASTER.md`.

---

# PART 8 — OPEN DECISIONS

| # | Question | Owner | Needed by |
|---|---|---|---|
| O1 | Can picking execute a **two-source line** for one store-size? If no, default moves to `SINGLE_STRICT` | Supply chain | before Phase 1 |
| O2 | Confirm **BR-RDC-11** — a hold is never split, and a hold that no single RDC can cover is reduced | Supply chain | before Phase 1 |
| O3 | Does a cross-ship need an inter-warehouse transfer document, or does the store STO issue directly from the sourcing RDC? | Supply chain / SAP | before Phase 4 |
| O4 | Target date to clean the untagged stores in `Master_ALC_INPUT_ST_MASTER` | Business owner | before Phase 5 |
| O5 | Seed values for the two `'*'` rows in `ARS_RDC_FALLBACK` | Business owner | before Phase 1 test |
