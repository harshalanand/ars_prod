# Dev-server task: make option eligibility I_ROD-aware (`OPT_REQ_ROD`)

Paste everything below into a Claude Code session on the development server.
The full specification is `docs/superpowers/specs/2026-08-13-irod-eligibility-fsd.md`
(**BR-16 / FS-10**) — read it before touching code; this prompt is the executable summary.

---

## Before you start — confirm you are NOT on production

This change is to be developed and validated on a **development copy**, never directly on
`HOPC866` / `Rep_Data` production.

```powershell
cd <repo>\backend
@'
from app.database.session import get_data_engine
from sqlalchemy import text
with get_data_engine().connect() as c:
    print(c.execute(text("SELECT @@SERVERNAME, DB_NAME(), SYSDATETIME()")).fetchone())
'@ | .\venv\Scripts\python.exe -
```

Stop and ask if that prints the production server/database. The target DB is set by
`backend/.env` → `DB_SERVER`, `DB_NAME` (system DB), `DATA_DB_NAME` (data DB). Point those
at the dev copy before running anything.

**No migration script is needed.** `ARS_LISTING` is dropped and recreated on every
generate, the new column is added by an idempotent `ALTER TABLE … ADD` inside Part 4c, and
`_reconcile_history_columns()` in `parked_history.py` back-fills the parked/history tables
on first approve. A dev copy that is behind on schema will catch up by itself.

---

## Context

Options are rejected as "no demand" while the allocation engine would have shipped them.

Live example — session `20260812_131757_589`, `WERKS=HS11`,
`GEN_ART_NUMBER=1241092810`, `CLR=SKY_BLU`:

```
OPT_TYPE = RL     OPT_MBQ = 9     STK_TTL = 12    I_ROD = 2    MSA_FNL_Q = 612
OPT_REQ_WH = MAX(0, 9 - 12) = 0
  -> ELIG_REASON = NO_DEMAND, LISTED_REASON = R05_REQ_POS;, ALLOC_STATUS = INELIGIBLE
  -> 0 rows in ARS_ALLOC_HISTORY
```

`I_ROD = 2` means the store is entitled to two rounds — target `2 × 9 = 18` against stock
of 12, a real shortfall of 6 units with 612 available in the RDC. But `OPT_REQ` /
`OPT_REQ_WH` (`listing.py:2231-2238`, `:2344-2350`) are single-round figures with no
`I_ROD` term, and both eligibility gates test `OPT_REQ_WH`.

`I_ROD` is applied only inside the engine, at size grain
(`rule_engine_per_opt.py:835-841`). The engine never saw this option.

**The engine needs no change.** Its band mask is `OPT_TYPE == ot AND I_ROD >= r AND
ALLOC_STATUS not in ('SKIPPED','INELIGIBLE')` (`rule_engine_per_opt.py:795-799`) — no
`SZ_REQ` filter — and a size that receives nothing in round 1 only gets an audit remark,
never `SKIPPED` (`:1504-1518`). Once admitted, this row ships 6 units in round 2.

---

## Changes to make

### 1. Compute `OPT_REQ_ROD` — `backend/app/api/v1/endpoints/listing.py`

Insert directly after the `OPT_REQ_WH` UPDATE that ends at **line 2350**, inside Part 4c.
Add the column with the same idempotent `try/except ALTER` pattern already used for
`OPT_MBQ_WH` / `OPT_REQ_WH` at `:2242-2246`.

```sql
UPDATE [{LISTING_TABLE}]
SET [OPT_REQ_ROD] = CASE
    WHEN (CASE WHEN ISNULL([OPT_TYPE],'') = 'TBL'
               THEN ISNULL([OPT_MBQ_WH],0) + (ISNULL(NULLIF([I_ROD],0),1) - 1) * ISNULL([OPT_MBQ],0)
               ELSE ISNULL(NULLIF([I_ROD],0),1) * ISNULL([OPT_MBQ],0)
          END - ISNULL([STK_TTL],0)) > 0
    THEN ROUND(CASE WHEN ISNULL([OPT_TYPE],'') = 'TBL'
                    THEN ISNULL([OPT_MBQ_WH],0) + (ISNULL(NULLIF([I_ROD],0),1) - 1) * ISNULL([OPT_MBQ],0)
                    ELSE ISNULL(NULLIF([I_ROD],0),1) * ISNULL([OPT_MBQ],0)
               END - ISNULL([STK_TTL],0), 0)
    ELSE 0 END
```

Three things that are **not** negotiable in that formula:

- **TBL counts the hold buffer once** — `MBQ_WH + (I_ROD−1) × MBQ`, mirroring
  `rule_engine_per_opt.py:837`. `I_ROD × OPT_MBQ_WH` would multiply `hold_days` by the
  round count and inflate every TBL option.
- **`NULLIF(I_ROD,0)`** — 6,651 R05-blocked rows in the reference run carry `I_ROD = 0`.
- **Clamp at 0**, `ROUND(…, 0)` — consistent with the two existing columns.

Placement matters: `OPT_MBQ`, `OPT_MBQ_WH`, `OPT_TYPE` (Part 3.6) and `I_ROD` (Part 3.5a)
must all be populated, which is true from `:2350` onward.

Also extend the log line at `:2373` so the run log records the new column.

### 2. Carry the column downstream — `listing.py:49-68`

Add `"OPT_REQ_ROD"` to `_FINAL_KEEP_COLS`. **Required** — the `_FINAL_KEEP_SUFFIX =
{"_REQ"}` pattern does not match a name ending `_ROD`, so without this the column never
reaches `ARS_LISTING_WORKING` and both gates read NULL.

Add it to the export column lists at `:5546` and `:5748`.

### 3. Feature flag

- `rule_engine_new.py`, flags block at `:31-40`: add `RULE_R05_USE_IROD = False`.
- `listing.py:75` `GenerateRequest`: add `use_irod_eligibility: bool = False`.
- Thread it to the engine with the other rule params at `listing.py:3249-3277`.
- **The `/retry-failed` path needs nothing.** It reuses the existing
  `ARS_ALLOC_WORKING` and does not re-run Stage A/B (see the comment at
  `listing.py:3908-3909`), so R05 is never re-evaluated there. Verified during
  implementation on 2026-08-13.

**Both gates must read the same switch.** If Layer 1 admits and Layer 2 rejects (or the
reverse), rows are silently dropped at Part 7, which filters `WHERE ELIG_FLAG = 1` when
`shift_all_to_working` is off.

### 4. Gate 1 — `listing.py:2784`

```python
gate_demand = "ISNULL([OPT_REQ_WH], 0) >= 1" if _has_req else "1=1"
```

becomes: use `OPT_REQ_ROD` when the flag is on **and** the column exists
(`_has_req_rod`, detected the same way as `_has_req` at `:2771`); otherwise keep
`OPT_REQ_WH`; otherwise `"1=1"`.

### 5. Gate 2 — `rule_engine_new.py:342`

```python
pieces.append("CASE WHEN ISNULL(TRY_CAST([OPT_REQ_WH] AS FLOAT),0) < 1 THEN 'R05_REQ_POS;' ELSE '' END")
```

Select the column by the same rule, checking existence on `working_table` via `_cols`.
**Do not rename the reason code** — `R05_REQ_POS` and `NO_DEMAND` stay exactly as they are;
only the arithmetic behind them changes. Dashboards and saved queries depend on the strings.

### 6. Gate 3 — `listing.py:4660`

The `/listing-build` endpoint filters `ISNULL([OPT_REQ_WH],0) >= :min_req`. Switch it the
same way, or it re-drops every row the new gate admits.

### 7. `NO_REQ` label alignment

`SZ_REQ` stays 1-round (`rule_engine_new.py:1026-1030`). Without this fix a newly admitted
option that ships nothing because the pool is empty gets labelled `NO_REQ` instead of
`NO_POOL_MSA`, sending reviewers after demand when the problem is supply.

In `rule_engine_pandas.py:1162-1163` and `rule_engine_new.py:2733-2734`, change the
`NO_REQ` branch from `ISNULL(SZ_REQ,0) <= 0` to the I_ROD target already used by the
`ALREADY_STOCKED` branch immediately above it. `NO_REQ` becomes unreachable by
construction. Label-only — no quantity changes.

---

## Do NOT change

| Item | Why |
|---|---|
| `OPT_REQ`, `OPT_REQ_WH` formulas | Feed excess arithmetic, `OPT_PRIORITY_RANK` ordering (`rule_engine_new.py:539`, `:633`), dashboards, exports. Changing them reorders dispatch and rewrites published reports. |
| `MJ_REQ`, `MJ_MBQ`, R09 headroom, `*_mj_req_cap_pct`, sec-caps | Stay 1-round by decision. The newly admitted options compete inside today's MAJ_CAT budget. |
| `rule_engine_per_opt.py` | Correct as-is. |
| `SZ_REQ` / `SZ_REQ_WH` values | Only the `NO_REQ` label moves (item 7). |
| Reason-code strings | `NO_DEMAND`, `R05_REQ_POS` unchanged. |

Do not "also fix" anything else you notice in these files. Raise it separately.

---

## Verification

### Static — no generate required

Run against an existing `ARS_LISTING` after the Part 4c change has produced the column once:

```sql
SELECT OPT_TYPE, ISNULL(NULLIF(I_ROD,0),1) AS i_rod,
       SUM(CASE WHEN ISNULL(OPT_REQ_WH,0)  >= 1 THEN 1 ELSE 0 END) AS admitted_today,
       SUM(CASE WHEN ISNULL(OPT_REQ_ROD,0) >= 1 THEN 1 ELSE 0 END) AS admitted_after
FROM ARS_LISTING WITH (NOLOCK)
GROUP BY OPT_TYPE, ISNULL(NULLIF(I_ROD,0),1)
ORDER BY OPT_TYPE, i_rod;
```

Expect identical counts at `i_rod <= 1`, `admitted_after >= admitted_today` everywhere,
and **zero** rows where `OPT_REQ_WH >= 1` but `OPT_REQ_ROD = 0`.

### Paired runs — same parameters, flag off then on

1. `ELIG_FLAG = 1` count rises by roughly 5% and the delta is **all `OPT_TYPE = 'RL'`**.
   On the reference run: 15,145 newly admitted, 15,012 with `MSA_FNL_Q > 0`, RL 15,145 /
   TBC 0 / TBL 0. TBL and TBC never hit this gate — their stock always clears one round.
2. Per `(WERKS, MAJ_CAT)`: `SUM(ALLOC_QTY) <= rl_mj_req_cap_pct% × MJ_REQ` still holds for
   every store. The MAJ_CAT budget did not move, so nothing may breach it.
3. No row carries `SKIP_REASON = 'NO_REQ'`.
4. Runtime up ≈ 5% (reference run: 3,387.5 s, 316,211 OPTs, 647,052 alloc rows,
   2.05 sizes/OPT → expect roughly +3 minutes).

### Acceptance criteria

| # | Criterion |
|---|---|
| AC-1 | Flag off ⇒ results identical to a pre-change run on the same inputs. |
| AC-2 | `HS11 / 1241092810 / SKY_BLU` → `OPT_REQ_ROD = 6`, `ELIG_FLAG = 1`, `ALLOC_STATUS = ALLOCATED`, `ALLOC_QTY = 6` (subject to `PAK_SZ` rounding). |
| AC-3 | No option with `STK_TTL >= I_ROD × OPT_MBQ` is admitted. |
| AC-4 | A TBL row with `I_ROD >= 2` gives `OPT_REQ_ROD = OPT_MBQ_WH + (I_ROD−1) × OPT_MBQ − STK_TTL` — `hold_days` counted once. |
| AC-5 | `OPT_REQ`, `OPT_REQ_WH`, `MJ_REQ`, `SZ_REQ`, `OPT_PRIORITY_RANK` numerically unchanged across the paired runs. |
| AC-6 | No `(WERKS, MAJ_CAT)` exceeds its `MJ_REQ` cap. |

---

## Rollback

Set `use_irod_eligibility = False` / `RULE_R05_USE_IROD = False` and re-run. The column is
still written, no gate reads it, behaviour reverts exactly. `OPT_REQ_ROD` is additive and
nullable — no schema rollback.

---

## On completion

Update, in the same change:

- `frontend/public/docs/manual/listing.md` — FSD section and `## Recorded rules`.
- `backend/app/docs/BRD_ARS_V2.md` — the R05 row at `:378`.
- `ARS_DATA_DICTIONARY` — seed row for `OPT_REQ_ROD`, alongside the
  `OPT_MBQ_WH / OPT_REQ_WH` entry at `data_dictionary.py:173-174`.

Report back with: the static-check table, the paired-run comparison, and the AC-2 row from
`ARS_ALLOC_WORKING`.
