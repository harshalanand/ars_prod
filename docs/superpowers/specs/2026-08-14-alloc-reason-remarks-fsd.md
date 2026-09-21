# FSD — ALLOC_REASON / SKIP_REASON / ALLOC_REMARKS coherence (FS-12)

| | |
|---|---|
| **Document** | Functional Specification |
| **Module** | Allocation engine — `rule_engine_per_opt` (band), `rule_engine_pandas` (finalise), `rule_engine_new._stage_d_reflect` (roll-up), `listing.py` (eligibility + hold release) |
| **Rule ID** | **FS-12** |
| **Date** | 2026-08-14 |
| **Status** | As-is documented / to-be proposed — **not implemented** |
| **Evidence run** | `20260812_131757_589` |
| **Worked example** | `WERKS=HK27`, `MAJ_CAT=LS_BABY_WIPES`, `GEN_ART_NUMBER=1240058982`, `CLR=A_MIX`, `OPT_TYPE=TBC` |
| **Scope** | Audit labels only — no quantity, pool, or allocation decision changes |

---

## 1. Purpose

Four columns explain *why* an option did or did not ship:

| Column | Table | Grain |
|---|---|---|
| `SKIP_REASON` | `ARS_ALLOC_WORKING` | size — (WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ) |
| `ALLOC_REASON` | `ARS_ALLOC_WORKING` | size |
| `ALLOC_REMARKS` | `ARS_ALLOC_WORKING` | size |
| `ALLOC_REASON` | `ARS_LISTING_WORKING` | OPT — (WERKS, MAJ_CAT, GEN_ART, CLR) |
| `ALLOC_REMARKS` | `ARS_LISTING_WORKING` | OPT |

`ARS_LISTING_WORKING` is meant to be the OPT-grain roll-up of `ARS_ALLOC_WORKING`. It is
not. For run `20260812_131757_589`, **234,550 of 284,603 zero-allocation options (82.4 %)
carry an OPT-grain reason that contradicts their own size rows.**

This document states the complete as-is write logic for all four columns, isolates why the
two tables disagree, and specifies the corrected logic.

---

## 2. As-is — `ARS_ALLOC_WORKING` (size grain)

Writers in execution order. `→` means the value is **replaced**; `+=` means **appended**.

### 2.0 Seed (Stage B)

Alloc rows are created from `ARS_LISTING_WORKING` rows with `LISTED_FLAG = 1` and
`ELIG_FLAG = 1`.

```
ALLOC_STATUS  → 'PENDING'
SKIP_REASON   → ''          (rule_engine_pandas.py:1474-1477)
ALLOC_REASON  → NULL        (rule_engine_new.py:3427-3435, column default)
ALLOC_REMARKS → NULL
```

### 2.1 Stage C — per-OPT band loop

`rule_engine_per_opt._run_band_per_opt`, the only live band since 2026-07-10. Loop order is
`OPT_TYPE ∈ (RL, TBC, TBL)` × `round r = 1 … max(I_ROD)` × rank band × option.

Two stamping helpers, with **different precedence rules** — this is the origin of RC-4
below:

| Helper | `SKIP_REASON` | `ALLOC_REMARKS` |
|---|---|---|
| `_mark_opt_skip` ([rule_engine_per_opt.py:103-129](backend/app/services/rule_engine_per_opt.py#L103-L129)) | written **only if currently empty** — first-writer-wins | `+= ' <REASON>(<detail>);'` — every writer lands |
| `_mark_opt_skip_sec_cap` ([:132-246](backend/app/services/rule_engine_per_opt.py#L132-L246)) | written **only if currently empty** | **→ replaced** (pool was never touched, so no waterfall narrative is worth keeping) |

Skip sites, in the order they are evaluated inside one option's turn:

| # | Site | `SKIP_REASON` | `ALLOC_REMARKS` token |
|---|---|---|---|
| 5b | R07 live size-ratio ([:924](backend/app/services/rule_engine_per_opt.py#L924)) | `R07_SIZE_RATIO_LIVE` | `R07_SIZE_RATIO_LIVE(sizes_with_pool=a/b,ratio=x,thr=y,min=z)` |
| 5b2 | TBL MJ_REQ gate ([:949](backend/app/services/rule_engine_per_opt.py#L949)) | `TBL_MJ_REQ_GATE_FAIL` | `TBL_MJ_REQ_GATE_FAIL(opt_mbq=…,cap_rem=…,cap_pct=…,factor=…,threshold=…,allowance=…)` |
| 5c | TBL MBQ pre-check ([:1059](backend/app/services/rule_engine_per_opt.py#L1059)) | `MBQ_CAP_TBL` | `MBQ_CAP_TBL(pre-check:intended=…,mj_req_rem=…,overshoot=…>0.5*intended=…)` |
| 5c | RL/TBC cap exhausted ([:1096](backend/app/services/rule_engine_per_opt.py#L1096)) | `MBQ_CAP_<RL\|TBC>` | `MBQ_CAP_<ot>(pre-check:cap_rem=0,need=…)` |
| 5c | RL/TBC COMPLETE overshoot ([:1128](backend/app/services/rule_engine_per_opt.py#L1128)) | `MBQ_CAP_<RL\|TBC>` | `MBQ_CAP_<ot>(complete-mode:need=…,cap_rem=…,overshoot=…>0.5*need=…)` |
| 5c | sec/primary cap block ([:193-246](backend/app/services/rule_engine_per_opt.py#L193-L246)) | `SEC_CAP_PRE_<grid>(cap=N%[,cont=N%])` or `PRIMARY_CAP_PRE_<grid>(…)` | **replaced** with `SKIPPED by sec-cap \| grid=… \| … \| intended_ship=N -> final_ship=0 \| pool_untouched=true` |
| 5c | sec-cap hard block ([:177-192](backend/app/services/rule_engine_per_opt.py#L177-L192)) | `SEC_CAP_GRID_NULL[<grid>],SEC_CAP_MBQ_ZERO[<grid>]` (comma-joined) | **replaced** with `SKIPPED by sec-cap hard-block \| …` |

Admit-path stamps (option ships; status untouched):

| Site | `ALLOC_REMARKS` |
|---|---|
| MBQ overshoot admit ([:1071](backend/app/services/rule_engine_per_opt.py#L1071)) | `+= ' MBQ_CAP_OVERSHOOT(TBL,intended=…,…)'` |
| sec-cap overshoot admit ([:249-292](backend/app/services/rule_engine_per_opt.py#L249-L292)) | `+= ' SEC_CAP_OVERSHOOT(grid=…, cap=N%[,cont=N%], reason=overshoot(n) <= 0.5xintended(m)=t, …);'` |

Per-size stamps, step 5j ([:1510-1638](backend/app/services/rule_engine_per_opt.py#L1510-L1638)) — these run
for **every** size of an admitted option:

```
moved = (round_ship + round_hold + from_hold) > 0

moved      → += ' B[<ot>.r<r>.rk<rank>] ship=N hold=N[ from_hold=N][ partial(need=N,pool=N)];'
not moved
  live_pool == 0 → += ' POOL_EMPTY(FNL_Q=N,live_pool=0,consumed_prior=N);'
  live_pool > 0  → (nothing — the row is silent here and is labelled later by §2.3)

pak > 1, RL/TBC:
  gated to zero          → += ' PAK_SZ_GATE(req=N,pak=P);'
  rounded, fully served  → += ' PAK_SZ_ROUND(from=N,to=M,pak=P);'
  rounded, short         → += ' PAK_SZ_ROUND(from=N,to=M,pak=P,short=<cap|pool|ceiling>=N);'
pak > 1, TBL:
  → ' PAK_SZ_GATE(…)' / ' PAK_SZ_SHIP(…[,loose|,short])' / ' PAK_SZ_HOLD(…[,gated|,loose])'
```

`SKIP_REASON` is **not** written by step 5j. A pak-gated size gets a `PAK_SZ_GATE` remark
and no reason code.

### 2.2 Between rounds — revalidation

`rule_engine_pandas._pre_band_check` ([:2318-2339](backend/app/services/rule_engine_pandas.py#L2318-L2339)) and
`_revalidate_after_band` ([:2556-2646](backend/app/services/rule_engine_pandas.py#L2556-L2646)) append to the
**listing** row and propagate to alloc rows:

```
ALLOC_REMARKS += ' SKIP_MJ_EXHAUSTED(mj_rem=N);'
              += ' SKIP_PRI_BROKEN(pri=N);'
              += ' SKIP_STORE_BROKEN(mj_rem=N);'
```

Propagation ([:2247-2255](backend/app/services/rule_engine_pandas.py#L2247-L2255),
[:2636-2646](backend/app/services/rule_engine_pandas.py#L2636-L2646)):

```python
alloc_df.loc[no_reason, 'SKIP_REASON'] = reason_vals[no_reason].fillna('REVALIDATION_SKIP')
```

`reason_vals` is the **whole remark string**, not a code. Observed values in
`ARS_ALLOC_HISTORY` therefore include the leading space and trailing semicolon:

```
SKIP_REASON = ' SKIP_PRI_BROKEN(pri=50.0);'
```

The sequential-engine equivalent adds `CROSS_SKIP_<ot>_MSA`, `CROSS_SKIP_<ot>_PRI`,
`CROSS_SKIP_<ot>_STORE_BROKEN` ([rule_engine_new.py:2530-2591](backend/app/services/rule_engine_new.py#L2530-L2591))
and `R09_HEADROOM_TRIVIAL` ([:446-600](backend/app/services/rule_engine_new.py#L446-L600)).

### 2.3 Post-loop finalise

`rule_engine_pandas` ([:1062-1182](backend/app/services/rule_engine_pandas.py#L1062-L1182)), in order:

1. `_stage_d_apply_pak_sz_rounding` — **retired 2026-08-01**, no-op.
2. `_stage_c_apply_opt_mj_req_gate` → `SKIP_REASON = '<OPT>_MJ_REQ_GATE_FAIL'` or
   `'<OPT>_MJ_REQ_POST_WINNER'`.
3. Zero `HOLD_QTY` on skipped rows with no ship; reset `POOL_CONSUMED` on fully-zeroed rows.
4. `ALLOC_QTY = SHIP_QTY`.
5. Status + catch-all reason ([:1125-1182](backend/app/services/rule_engine_pandas.py#L1125-L1182)):

```sql
ALLOC_STATUS = CASE
    WHEN SHIP+HOLD > 0 AND SHIP+HOLD >= target THEN 'ALLOCATED'
    WHEN SHIP+HOLD > 0                         THEN 'PARTIAL'
    ELSE 'SKIPPED' END
-- target: TBL → SZ_MBQ_WH + (I_ROD-1)*SZ_MBQ - SZ_STK
--         else → I_ROD*SZ_MBQ - SZ_STK

SKIP_REASON = CASE
    WHEN ISNULL(SKIP_REASON,'') <> ''      THEN SKIP_REASON      -- preserve
    WHEN SHIP=0 AND HOLD=0 AND target <= 0 THEN 'ALREADY_STOCKED'
    WHEN SHIP=0 AND HOLD=0 AND target <= 0 THEN 'NO_REQ'         -- ← duplicate predicate
    WHEN SHIP=0 AND HOLD=0                 THEN 'NO_POOL_MSA'
    ELSE SKIP_REASON END
```

The `NO_REQ` arm repeats the `ALREADY_STOCKED` predicate verbatim and is therefore
unreachable from this statement — acknowledged in the comment at
[rule_engine_new.py:2762-2773](backend/app/services/rule_engine_new.py#L2762-L2773). 210 `NO_REQ`
rows nonetheless exist in the evidence run; the surviving writer has not been identified
and is out of scope for this document.

6. Sec-cap post-pass — safety net, runs only when the per-OPT spec build failed
   ([rule_engine_new.py:4023-4060](backend/app/services/rule_engine_new.py#L4023-L4060)); same
   `SEC_CAP_PRE_*` / `PRIMARY_CAP_PRE_*` codes, remark **replaced** for rows that had shipped.
7. `_classify_alloc_reason` — **a stub that does nothing**
   ([rule_engine_new.py:3391-3406](backend/app/services/rule_engine_new.py#L3391-L3406)).
   `ARS_ALLOC_WORKING.ALLOC_REASON` is therefore **NULL on every row of every run.**
8. `_stage_d_reflect` — §3.
9. `_snapshot_live_pool_to_alloc` ([:3319-3368](backend/app/services/rule_engine_new.py#L3319-L3368)) —
   `+= ' LIVE_POOL=N;'` on rows where `SKIP_REASON = 'NO_POOL_MSA'` exactly.

### 2.4 Post-allocation — hold release (Part 8.55)

[listing.py:3596-3613](backend/app/api/v1/endpoints/listing.py#L3596-L3613), for options whose
`OPT_STATUS = 'MIX'`:

```
ALLOC_REMARKS += ';HOLD_RELEASED_NOT_COVERED(N)'
```

Applied to `ARS_ALLOC_WORKING` **and** this session's `ARS_ALLOC_PARKED` rows.

---

## 3. As-is — `ARS_LISTING_WORKING` (OPT grain)

### 3.1 Pre-allocation eligibility

| Column | Writer | Values |
|---|---|---|
| `ELIG_REASON` / `ELIG_FLAG` | [listing.py:2861-2876](backend/app/api/v1/endpoints/listing.py#L2861-L2876) | `NOT_LISTED`, `NO_STOCK`, `NO_DEMAND`, `NO_DISPLAY`, `TBL_SIZE_LT_60`, `OK` |
| `LISTED_REASON` / `LISTED_FLAG` | [rule_engine_new.py:339-435](backend/app/services/rule_engine_new.py#L339-L435) | concatenation of `R01_LISTING;` `R02_NOT_MIX;` `R04_MSA_POS;` `R05_REQ_POS;` `R06_PRI_100;` `R07_VAR_RATIO_TBL;` `R09_HEADROOM_TRIVIAL;` — empty string ⇒ `LISTED_FLAG = 1` |

`ARS_LISTING_WORKING` is created as `SELECT … INTO` from `ARS_LISTING`, filtered
`WHERE ELIG_FLAG = 1` unless business rule `LST_AUDIT_ALL_TO_WORKING` is on
([listing.py:2895-2940](backend/app/api/v1/endpoints/listing.py#L2895-L2940)).

### 3.2 During allocation

The revalidation appends of §2.2 land here first (`SKIP_MJ_EXHAUSTED`, `SKIP_PRI_BROKEN`,
`SKIP_STORE_BROKEN`, `CROSS_SKIP_*`), then propagate down.

### 3.3 `_stage_d_reflect` — four statements

[rule_engine_new.py:3085-3316](backend/app/services/rule_engine_new.py#L3085-L3316).

**(a) Roll up quantities and rewrite the remark** ([:3113-3180](backend/app/services/rule_engine_new.py#L3113-L3180)) —
for every `LISTED_FLAG = 1` row:

```sql
ALLOC_QTY     = SUM(SHIP_QTY)      -- over the option's size rows
HOLD_QTY      = SUM(HOLD_QTY)
FROM_HOLD_QTY = SUM(FROM_HOLD_QTY)
ALLOC_SEQ     = ROW_NUMBER() OVER (PARTITION BY MAJ_CAT
                  ORDER BY OPT_TYPE(RL,TBC,else), MIN(ALLOC_ROUND), ST_RANK, OPT_PRIORITY_RANK)
ALLOC_STATUS  = CASE WHEN ship+hold = 0        THEN 'NOT_ALLOCATED'
                     WHEN filled_rows < sz_rows THEN 'PARTIAL'
                     ELSE 'ALLOCATED' END
ALLOC_REMARKS = CONCAT(
                  CASE WHEN LEN(prior) > 0 THEN prior + ' | ' ELSE '' END,
                  'ship=N; hold=N; sizes=a/b; seq=N')
```

**(b) Roll up `ALLOC_REASON` from size grain** ([:3213-3247](backend/app/services/rule_engine_new.py#L3213-L3247)):

```sql
ALLOC_REASON = CASE
    WHEN sz_full = sz_total AND sz_total > 0        THEN 'SHIPPED_FULL'
    WHEN sz_full + sz_partial > 0 AND sz_unmet > 0  THEN 'SHIPPED_PARTIAL'
    WHEN sz_blocked = sz_total AND distinct_blocks = 1 THEN first_block
    WHEN sz_blocked = sz_total AND distinct_blocks > 1 THEN 'BLOCKED_MIXED'
    ELSE W.ALLOC_REASON END
```

Every counter reads `ARS_ALLOC_WORKING.ALLOC_REASON`, which §2.3 step 7 leaves NULL. So
`sz_full = sz_partial = sz_blocked = 0` always, every branch falls to `ELSE`, and the
statement is **inert**. `SHIPPED_FULL`, `SHIPPED_PARTIAL` and `BLOCKED_MIXED` are never
produced by any run.

**(c) Ineligible rows** ([:3251-3260](backend/app/services/rule_engine_new.py#L3251-L3260)) — `LISTED_FLAG = 0`:

```sql
ALLOC_STATUS = 'INELIGIBLE'
ALLOC_REASON = 'INELIGIBLE_' + LEFT(ISNULL(NULLIF(LISTED_REASON,''),'STAGE_A'), 50)
```

**(d) Zero-allocation listed rows** ([:3272-3316](backend/app/services/rule_engine_new.py#L3272-L3316)) —
`LISTED_FLAG = 1 AND ALLOC_QTY = 0 AND HOLD_QTY = 0`. **This is the statement that breaks.**

```sql
;WITH sec_blocked AS (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR_J,
           MAX(SKIP_REASON) AS sec_skip, MAX(ALLOC_REMARKS) AS sec_remark
    FROM alloc_table
    WHERE SKIP_REASON LIKE 'SEC_CAP_PRE_%'          -- ← whitelist
       OR SKIP_REASON LIKE 'PRIMARY_CAP_PRE_%'      -- ← whitelist
    GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'')
)
UPDATE W SET
    ALLOC_STATUS  = 'NOT_ALLOCATED',
    ALLOC_REASON  = ISNULL(W.ALLOC_REASON,
        CASE WHEN B.sec_skip IS NOT NULL         THEN B.sec_skip
             WHEN ISNULL(W.OPT_REQ, 0) <= 0      THEN 'BLOCKED_NO_REQ'
             ELSE                                     'BLOCKED_NO_POOL_MSA' END),
    ALLOC_REMARKS = CASE
        WHEN B.sec_skip IS NOT NULL THEN ISNULL(B.sec_remark,'')     -- replace
        ELSE ISNULL(W.ALLOC_REMARKS,'') +
             CASE WHEN ISNULL(W.OPT_REQ,0) <= 0
                  THEN ' NO_REQ(OPT_REQ=…, OPT_MBQ=…)'
                  ELSE ' NO_POOL_MSA(OPT_REQ=…, MSA_FNL_Q=…, MSA_FNL_Q_REM=…)' END
    END
FROM working_table W LEFT JOIN sec_blocked B ON …
```

---

## 4. The mismatch

### 4.1 Worked example

`SESSION_ID='20260812_131757_589'`, `WERKS='HK27'`, `GEN_ART_NUMBER='1240058982'`.

`ARS_ALLOC_HISTORY` — two size rows, identical verdict:

| VAR_ART | SZ | SZ_MBQ | SZ_STK | I_ROD | PAK_SZ | FNL_Q | FNL_Q_REM | SHIP | ALLOC_STATUS | SKIP_REASON |
|---|---|---|---|---|---|---|---|---|---|---|
| …001 | A | 7 | 2 | 3 | 40 | 7 | 7 | 0 | SKIPPED | `MBQ_CAP_TBC` |
| …002 | A | 7 | 0 | 3 | 40 | 1134 | 1094 | 0 | SKIPPED | `MBQ_CAP_TBC` |

```
ALLOC_REMARKS = ' PAK_SZ_GATE(req=5,pak=40); PAK_SZ_GATE(req=12,pak=40);
                  MBQ_CAP_TBC(complete-mode:need=40,cap_rem=15,overshoot=25>0.5*need=20);'
ALLOC_REASON  = NULL
```

`ARS_LISTING_WORKING_HISTORY` — the same option:

```
ALLOC_STATUS  = NOT_ALLOCATED
ALLOC_REASON  = BLOCKED_NO_POOL_MSA
ALLOC_REMARKS = 'ship=0; hold=0; sizes=0/2; seq=104
                 NO_POOL_MSA(OPT_REQ=11, MSA_FNL_Q=1141, MSA_FNL_Q_REM=1141)'
```

The option was blocked by the **TBC MBQ cap** — the store's remaining cap budget was 15
against a pak-rounded need of 40, an overshoot of 25 which exceeds the 0.5 × need = 20
allowance. The pool was never the constraint: `MSA_FNL_Q_REM = 1141` on the option row and
`FNL_Q_REM = 1094` on the size row. The listing table nevertheless reports an empty pool.

### 4.2 Root causes

**RC-1 — the `sec_blocked` whitelist covers 2 of 12 reason families.**
§3.3(d) recognises only `SEC_CAP_PRE_%` and `PRIMARY_CAP_PRE_%`. Every other
`SKIP_REASON` falls into the `ELSE` arm, which tests `OPT_REQ` and — because a listed
option almost always has `OPT_REQ > 0` — emits `BLOCKED_NO_POOL_MSA` regardless of the
actual cause.

Measured over `20260812_131757_589`, joining alloc→listing at OPT grain, restricted to
`ALLOC_QTY = 0 AND HOLD_QTY = 0`:

| Alloc-grain `SKIP_REASON` family | → `BLOCKED_NO_POOL_MSA` | → carried through | total |
|---|---:|---:|---:|
| `R07_SIZE_RATIO_LIVE` | 104,176 | 0 | 104,176 |
| `ALREADY_STOCKED` | 44,085 | 0 | 44,085 |
| `TBL_MJ_REQ_GATE_FAIL` | 43,806 | 0 | 43,806 |
| `SKIP_PRI_BROKEN(…)` | 36,465 | 0 | 36,465 |
| `SEC_CAP_PRE_*` | 0 | 27,103 | 27,103 |
| `NO_POOL_MSA` *(correct)* | 21,849 | 0 | 21,849 |
| `SEC_CAP_GRID_NULL[…]` / `SEC_CAP_MBQ_ZERO[…]` | 3,463 | 0 | 3,463 |
| `MBQ_CAP_RL` / `MBQ_CAP_TBC` / `MBQ_CAP_TBL` | 1,464 | 0 | 1,464 |
| `PRIMARY_CAP_PRE_*` | 0 | 1,101 | 1,101 |
| `SKIP_MJ_EXHAUSTED(…)` | 881 | 0 | 881 |
| `NO_REQ` | 210 | 0 | 210 |
| **Total** | **256,399** | **28,204** | **284,603** |

**234,550 options (82.4 %) are mislabelled.** Only 21,849 (7.7 %) genuinely ran out of pool.
Note the sec-cap hard-block family is *also* missed: `SEC_CAP_MBQ_ZERO[…]` does not match
`SEC_CAP_PRE_%`.

**RC-2 — `_classify_alloc_reason` is a stub, so the designed roll-up is dead.**
`ARS_ALLOC_WORKING.ALLOC_REASON` is NULL on every row, so §3.3(b) can never fire and
§3.3(d)'s `ISNULL(W.ALLOC_REASON, …)` guard always falls through to the default. The
intended path (size reason → OPT reason) does not exist; the fallback *is* the whole
mechanism.

**RC-3 — the two grains test different quantities.**
The listing side decides `NO_REQ` vs `NO_POOL_MSA` from OPT-grain `OPT_REQ`; the alloc side
decides `ALREADY_STOCKED` vs `NO_POOL_MSA` from size-grain `I_ROD × SZ_MBQ − SZ_STK`
(TBL: `SZ_MBQ_WH + (I_ROD−1) × SZ_MBQ − SZ_STK`). The 44,085 `ALREADY_STOCKED` options
have per-size demand of zero and OPT-grain `OPT_REQ > 0`, so the two tables reach opposite
conclusions from the same run.

**RC-4 — `SKIP_REASON` and `ALLOC_REMARKS` use opposite precedence on the same row.**
`_mark_opt_skip` writes `SKIP_REASON` first-writer-wins but appends to `ALLOC_REMARKS`. In
the worked example the remark opens with `PAK_SZ_GATE(req=5,pak=40)` from step 5j of an
earlier round while `SKIP_REASON` is `MBQ_CAP_TBC`. A reader who trusts the remark's first
token gets a different answer from one who trusts the code.

**RC-5 — `SKIP_REASON` is not a closed code domain.**
The revalidation propagation (§2.2) assigns the formatted remark string, so values such as
`' SKIP_PRI_BROKEN(pri=50.0);'` are stored — with leading whitespace and trailing
semicolon. `GROUP BY SKIP_REASON` therefore splits one cause across one bucket per distinct
numeric value.

**RC-6 — parameters are embedded in `ALLOC_REASON`, destroying its cardinality.**
The 28,204 correctly-propagated options produce ~450 distinct `ALLOC_REASON` values such as
`SEC_CAP_PRE_MJ_RNG_SEG(cap=130%,cont=31%)` because the cap and contribution percentages are
inside the code. `ALLOC_REASON` is `NVARCHAR(80)`; the longest observed hard-block string,
`SEC_CAP_GRID_NULL[MJ_M_YARN_02],SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]`, is 62 characters and would
overflow once a `BLOCKED_` prefix is added.

**RC-7 — the reason is appended after a restatement of the zeros.**
§3.3(a) writes `ship=0; hold=0; sizes=0/2; seq=104`, then §3.3(d) appends the cause to it.
Every zero-allocation remark opens by repeating that nothing shipped before saying why.

**RC-8 — prefix convention is inconsistent.**
§3.3(d) assigns `B.sec_skip` verbatim, so `ALLOC_REASON` holds bare
`SEC_CAP_PRE_MJ_FIT(cap=130%)` alongside prefixed `BLOCKED_NO_POOL_MSA`,
`BLOCKED_NO_REQ`, and `INELIGIBLE_R05_REQ_POS;`. Three prefix conventions coexist in one
column.

---

## 5. To-be

### 5.1 One classifier, two grains

Add a single pure function mapping any `SKIP_REASON` (raw, including the RC-5 whitespace and
embedded parameters) to a canonical family. Both grains use it, so they cannot diverge.

```
_reason_family(skip_reason) -- applied to LTRIM(RTRIM(skip_reason))

  matches                                            → family              rank
  'SEC_CAP_GRID_NULL%' or '%SEC_CAP_MBQ_ZERO%'       → SEC_CAP_HARDBLOCK    1
  'PRIMARY_CAP_PRE_%'                                → PRIMARY_CAP          2
  'SEC_CAP_PRE_%'                                    → SEC_CAP              3
  'MBQ_CAP_%'                                        → MBQ_CAP              4
  '%MJ_REQ_GATE_FAIL' / '%MJ_REQ_POST_WINNER'
     / '%MJ_REQ_CAP'                                 → MJ_REQ_GATE          5
  'R09_HEADROOM_TRIVIAL%'                            → R09_HEADROOM         6
  'R07_SIZE_RATIO_LIVE%'                             → R07_SIZE_RATIO       7
  '%SKIP_MJ_EXHAUSTED%'                              → MJ_EXHAUSTED         8
  '%SKIP_PRI_BROKEN%'                                → PRI_BROKEN           8
  '%SKIP_STORE_BROKEN%'                              → STORE_BROKEN         8
  'CROSS_SKIP_%' / 'REVALIDATION_SKIP%'              → REVALIDATION         8
  'PAK_SZ_BELOW_HALF%'                               → PAK_SZ_GATE          9
  'NO_POOL_MSA%'                                     → NO_POOL_MSA         10
  'ALREADY_STOCKED%'                                 → ALREADY_STOCKED     11
  'NO_REQ%'                                          → NO_REQ              12
  anything else (non-empty)                          → OTHER               13
```

Rank orders **decisiveness**, lowest wins. Ranks 1–8 are option-grain decisions: the gate
stopped the whole option, so it is the story regardless of what individual sizes show.
Ranks 9–12 are size-grain outcomes; among these a supply shortfall (`NO_POOL_MSA`) outranks
`ALREADY_STOCKED`, because if any size had demand and got nothing, "already stocked" does
not explain the option.

### 5.2 Fill `ARS_ALLOC_WORKING.ALLOC_REASON`

Replace the `_classify_alloc_reason` stub with a real per-size classification, so §3.3(b)
becomes live and §3.3(d) reverts to being a genuine fallback:

```sql
UPDATE alloc_table SET ALLOC_REASON = CASE
    WHEN ISNULL(SHIP_QTY,0) + ISNULL(HOLD_QTY,0) >= target AND ISNULL(SHIP_QTY,0) > 0
         THEN 'SHIPPED_FULL'
    WHEN ISNULL(SHIP_QTY,0) > 0
         THEN 'SHIPPED_PARTIAL'
    ELSE 'BLOCKED_' + _reason_family(SKIP_REASON) END
-- target as defined in §2.3 step 5
```

### 5.3 Rewrite §3.3(d)

Replace the two-family whitelist with a rank-ordered pick over all families. The decisive
alloc row supplies **both** the reason and the narrative, so the two tables quote the same
sentence.

```sql
;WITH ranked AS (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR_J,
           SKIP_REASON, ALLOC_REMARKS,
           _reason_family(SKIP_REASON) AS fam,
           _reason_rank(SKIP_REASON)   AS rnk,
           ROW_NUMBER() OVER (
               PARTITION BY WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'')
               ORDER BY _reason_rank(SKIP_REASON), SZ) AS ord
    FROM alloc_table
    WHERE ISNULL(SKIP_REASON,'') <> ''
), decisive AS (
    SELECT * FROM ranked WHERE ord = 1
)
UPDATE W SET
    W.ALLOC_STATUS  = 'NOT_ALLOCATED',
    W.ALLOC_PHASE   = ISNULL(W.ALLOC_PHASE, 'MAIN'),
    W.ALLOC_REASON  = 'BLOCKED_' + ISNULL(D.fam, 'NO_POOL_MSA'),
    W.ALLOC_REMARKS = 'ship=0; hold=0; sizes=0/' + CAST(sz_rows AS NVARCHAR(10))
                    + '; seq=' + CAST(W.ALLOC_SEQ AS NVARCHAR(10))
                    + ' | ' + LTRIM(ISNULL(D.ALLOC_REMARKS, ''))
FROM working_table W
LEFT JOIN decisive D
    ON D.WERKS = W.WERKS AND D.MAJ_CAT = W.MAJ_CAT
   AND D.GEN_ART_NUMBER = W.GEN_ART_NUMBER AND D.CLR_J = ISNULL(W.CLR,'')
WHERE W.LISTED_FLAG = 1
  AND ISNULL(W.ALLOC_QTY,0) = 0 AND ISNULL(W.HOLD_QTY,0) = 0
```

`_reason_family` / `_reason_rank` are inlined `CASE` expressions or a scalar UDF; the
`ARS_LISTING_WORKING` roll-up and the `ARS_ALLOC_WORKING` classifier must call the same one.

The worked example then reads:

```
ARS_ALLOC_WORKING   SKIP_REASON  = MBQ_CAP_TBC
                    ALLOC_REASON = BLOCKED_MBQ_CAP
ARS_LISTING_WORKING ALLOC_REASON = BLOCKED_MBQ_CAP
                    ALLOC_REMARKS= 'ship=0; hold=0; sizes=0/2; seq=104 |
                                    PAK_SZ_GATE(req=5,pak=40); PAK_SZ_GATE(req=12,pak=40);
                                    MBQ_CAP_TBC(complete-mode:need=40,cap_rem=15,
                                    overshoot=25>0.5*need=20);'
```

### 5.4 Supporting corrections

| ID | Change | Fixes |
|---|---|---|
| C-1 | Revalidation propagation writes the **code** (`SKIP_PRI_BROKEN`), not the formatted remark; the parameterised text goes to `ALLOC_REMARKS` only. [rule_engine_pandas.py:2253](backend/app/services/rule_engine_pandas.py#L2253), [:2644](backend/app/services/rule_engine_pandas.py#L2644) | RC-5 |
| C-2 | `ALLOC_REASON` carries the family code only. Grid names, `cap=`, `cont=` move to `ALLOC_REMARKS`. Widen to `NVARCHAR(120)`. | RC-6, RC-8 |
| C-3 | Single prefix convention: `SHIPPED_*` \| `BLOCKED_<FAMILY>` \| `INELIGIBLE_<RULE>`. Strip the trailing `;` from `LISTED_REASON` before prefixing. | RC-8 |
| C-4 | `_mark_opt_skip` writes `SKIP_REASON` **last-writer-wins by rank**, matching §5.1, so the code and the remark's decisive token agree. | RC-4 |
| C-5 | Delete the unreachable `NO_REQ` arm at [rule_engine_pandas.py:1174-1178](backend/app/services/rule_engine_pandas.py#L1174-L1178) and [rule_engine_new.py:2774-2778](backend/app/services/rule_engine_new.py#L2774-L2778), or repair its predicate to test `SZ_REQ`. Identify the writer producing the 210 observed `NO_REQ` rows first. | §2.3 |
| C-6 | `_snapshot_live_pool_to_alloc` matches `SKIP_REASON LIKE 'NO_POOL_MSA%'` instead of `= 'NO_POOL_MSA'`. [rule_engine_new.py:3367](backend/app/services/rule_engine_new.py#L3367) | RC-5 |

### 5.5 Out of scope

No change to `SHIP_QTY`, `HOLD_QTY`, `ALLOC_QTY`, `POOL_CONSUMED`, `FNL_Q_REM`,
`ALLOC_ROUND`, `ALLOC_WAVE`, `ALLOC_SEQ`, or any gate predicate. Labels only. Re-running an
approved session must produce identical quantities.

---

## 6. Verification

**V-1 — no option-grain reason may contradict its size rows.** Must return 0.

```sql
;WITH A AS (
  SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'') CLR_J,
         MIN(CASE WHEN ISNULL(SKIP_REASON,'')<>'' THEN SKIP_REASON END) AS alloc_skip
  FROM ARS_ALLOC_WORKING
  GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'')
)
SELECT COUNT(*)
FROM ARS_LISTING_WORKING W
JOIN A ON A.WERKS=W.WERKS AND A.MAJ_CAT=W.MAJ_CAT
      AND A.GEN_ART_NUMBER=W.GEN_ART_NUMBER AND A.CLR_J=ISNULL(W.CLR,'')
WHERE ISNULL(W.ALLOC_QTY,0)=0 AND ISNULL(W.HOLD_QTY,0)=0
  AND W.ALLOC_REASON <> 'BLOCKED_' + dbo.ars_reason_family(A.alloc_skip)
```

**V-2 — `BLOCKED_NO_POOL_MSA` must imply an exhausted pool.** Must return 0.

```sql
SELECT COUNT(*) FROM ARS_LISTING_WORKING
WHERE ALLOC_REASON = 'BLOCKED_NO_POOL_MSA' AND ISNULL(MSA_FNL_Q_REM,0) > 0
```

Against `20260812_131757_589` this returns a large positive number today; the worked example
alone shows `MSA_FNL_Q_REM = 1141`.

**V-3 — `ARS_ALLOC_WORKING.ALLOC_REASON` must be non-NULL for every row.** Currently 100 %
NULL (RC-2).

**V-4 — `ALLOC_REASON` cardinality.** `SELECT COUNT(DISTINCT ALLOC_REASON)` should be
under ~30 (family codes) rather than the ~450 observed today.

---

## 7. Files

| File | Role |
|---|---|
| [rule_engine_per_opt.py](backend/app/services/rule_engine_per_opt.py) | §2.1 — band skip/admit stamps |
| [rule_engine_pandas.py](backend/app/services/rule_engine_pandas.py) | §2.2 revalidation, §2.3 finalise |
| [rule_engine_new.py](backend/app/services/rule_engine_new.py) | §2.3 gates, `_classify_alloc_reason` stub, `_stage_d_reflect` |
| [listing.py](backend/app/api/v1/endpoints/listing.py) | §3.1 eligibility, §2.4 hold release |
| [frontend/public/docs/manual/listing.md](frontend/public/docs/manual/listing.md) | dossier — update FSD + Recorded rules on implementation |
| [data_dictionary.py](backend/app/api/v1/endpoints/data_dictionary.py) | `ARS_DATA_DICTIONARY` entries for the four columns |
