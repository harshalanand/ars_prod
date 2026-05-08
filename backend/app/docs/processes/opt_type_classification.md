---
title: OPT_TYPE — Why an Article Gets Tagged MIX / RL / TBC / TBL
category: Listing
order: 22
source: backend/app/api/v1/endpoints/listing.py :: _classify_opt_type (Part 3.6)
last_reviewed: 2026-04-20
---

# OPT_TYPE — The Four-Tag System

> **═══ USER GUIDE ═══**
> Read sections 1–5 to understand what each tag means in plain language.

## Live tag distribution

<!-- @metric format="table" sql="SELECT ISNULL(OPT_TYPE,'(null)') AS Tag, COUNT(*) AS Rows, COUNT(DISTINCT WERKS) AS Stores FROM ARS_LISTING GROUP BY OPT_TYPE ORDER BY Rows DESC" label="Current OPT_TYPE split across ARS_LISTING" -->

<!-- @metric format="table" sql="SELECT IS_NEW, OPT_TYPE AS Tag, COUNT(*) AS Rows FROM ARS_LISTING GROUP BY IS_NEW, OPT_TYPE ORDER BY IS_NEW, Rows DESC" label="Tag split by IS_NEW (0 = existing article, 1 = new listing)" -->

## In plain English

Every `(store × option)` row in the listing gets exactly one of four tags. Think of the tag as ARS asking: *"What is the story of this article at this store, right now?"*

| Tag | Story |
|---|---|
| **MIX** | *"This article isn't worth replenishing here — either stock is low and nothing's coming, or the colour has too few sizes to be a real option."* → Treated as clearance; aggregated into one MIX line per store × category. |
| **RL** | *"This article is alive and stock is adequate — just top it up to target."* → Replenishment. |
| **TBC** | *"This article has some stock, but below target, and the warehouse can supply more."* → To-Be-Checked — partial top-up. |
| **TBL** | *"This store has nothing, but the warehouse has stock."* → To-Be-Listed — a fresh launch for this store. |

**Why it matters:** the allocator processes RL first, TBC second, TBL third, and ignores MIX. So the tag controls **whether** a row gets stock, not just how much.

## Where and when this happens

It's **Part 3.6** of the Listing Generation pipeline. You don't run it separately. If you see the tags look wrong in the output, come to this doc to learn what the rule is and what input data would change them.

## The decision tree (plain English)

ARS asks these questions **in order**. The first one that is "yes" decides the tag.

```
1. Is the store almost out AND the warehouse has nothing?
   STK_TTL  <  (threshold × ACS_D)     AND   MSA_FNL_Q = 0
   → MIX   "Nothing to do — discontinue for this store."

2. Does the option have too few filled colour-sizes?
   (VAR_FNL_COUNT / VAR_COUNT) < threshold   OR   VAR_FNL_COUNT < min_size_count
   → MIX   "Not a real option — not enough sizes to sell."

3. Is the store well-stocked (≥ threshold of daily sale × avg)?
   STK_TTL  ≥  (threshold × ACS_D)
   → RL    "Top up to target, everyone's happy."

4. Does the store have some stock, but below target, and warehouse has stock?
   0 < STK_TTL < (threshold × ACS_D)   AND   MSA_FNL_Q > 0
   → TBC   "Check and top up."

5. Is the store out, but warehouse has stock?
   STK_TTL ≤ 0   AND   MSA_FNL_Q > 0
   → TBL   "Launch here."

Safety-net catches (edge cases with NULL / zero numbers):
 6. MSA=0 and STK=0        → MIX
 7. MSA=0 and STK>0        → RL
 8. MSA>0 and STK>0        → TBC
 9. MSA>0 and STK≤0        → TBL
 10. Otherwise              → MIX
```

`threshold` = `Stock%` you set on the Listing page (default 0.6 = 60%).
`ACS_D` = Average Daily Sale for this store × category. If 0 or missing → fallback to `default_acs_d` (default 18).

## A worked example

Setting `Stock% = 0.6`, `default_acs_d = 18`, `MinSz = 3`.

| Store | STK_TTL | ACS_D | threshold × ACS_D | MSA_FNL_Q | VAR_COUNT | VAR_FNL_COUNT | **Tag** | Why |
|---|---|---|---|---|---|---|---|---|
| HN14 | 15 | 20 | 12 | 50 | 5 | 5 | **RL** | Adequate stock (15 ≥ 12) |
| HN21 | 5 | 20 | 12 | 50 | 5 | 5 | **TBC** | Some stock (0 < 5 < 12), warehouse available |
| HN35 | 0 | 20 | 12 | 50 | 5 | 5 | **TBL** | Empty, warehouse available |
| HN41 | 5 | 20 | 12 | 0 | 5 | 0 | **MIX** | Low stock AND nothing in warehouse |
| HN55 | 15 | 20 | 12 | 50 | 5 | 2 | **MIX** | Only 2 of 5 sizes filled (< 60%) |
| HN60 | 15 | 20 | 12 | 50 | 5 | 2 (MinSz=3) | **MIX** | Only 2 sizes, below MinSz=3 |
| HN70 | 15 | 0 | 10.8 (fallback 18×0.6) | 50 | 5 | 5 | **RL** | ACS_D=0 → fallback 18 kicks in |

## What each tag triggers downstream

| Tag | What happens next |
|---|---|
| **MIX** | Part 3.7 rolls up all MIX rows to **max 1 row per (store × MAJ_CAT)**, summing numeric columns. Allocator sets `ALLOC_FLAG=0` for them → they don't get stock. |
| **RL** | Allocator gives them stock first (OPT_TYPE priority #1). No warehouse hold. |
| **TBC** | Allocator gives them stock second. No warehouse hold. |
| **TBL** | Allocator gives them stock third. A hold buffer (`HOLD_QTY`) is reserved at the warehouse. |
| **NL** | A post-allocation tag for TBL rows. Once a TBL ships, it is sometimes relabelled `NL` (new-listed) in the `FINAL_OPT_TYPE` column. |

## Settings that change the split

| You want to… | Change |
|---|---|
| Send more stock as RL, less as TBC | **Lower** `Stock%` (e.g., 0.5) — more stores clear the adequate bar. |
| Be stricter about adequacy (more TBC / TBL) | **Raise** `Stock%` (e.g., 0.7 or 0.8). |
| Tag sparse colours as MIX (stop shipping them) | **Raise** `MinSz` (e.g., 4). |
| Allow thin colours through | Set `MinSz = 0` (switch off the MinSz gate). |
| Protect stores with no sales history | **Lower** `default_acs_d` (e.g., 12). Under-shipping those stores is safer than over-shipping. |

## Common questions (FAQ)

**Q: Why did my slow store get tagged MIX for every article?**
Probably `ACS_D` is very low or 0, so the "adequate" threshold is tiny. Either the store has no sales history (new store) or the cascade table is stale. Either way, the fallback `default_acs_d` should kick in; if it isn't, the cascade tables (`ARS_CALC_ST_MAJ_CAT`) may be older than the request.

**Q: Why is a hot new launch showing MIX at some stores?**
Check `VAR_FNL_COUNT` for that option at that store. If the warehouse only has 1-2 sizes left (e.g., only XL), the MinSz rule kicks in and flags it as MIX. This is correct — a 1-size ship is usually a bad experience.

**Q: The tag changed between two runs without any data upload. Why?**
Somebody changed `Stock%`, `MinSz`, or `ACS_D` between runs. The classification is deterministic for a given set of inputs — identical inputs = identical tags.

**Q: Can a MIX row ever "come back" to RL in a later step?**
No. The MIX tag is final once Part 3.6 runs. If you want to revisit it, run Listing Generation again with different settings.

**Q: What's the difference between MIX and NL?**
MIX = *we won't ship this*. NL = *we did ship this, and it was a new listing*. NL is a post-allocation label on `FINAL_OPT_TYPE`, not the pre-allocation tag.

## Troubleshooting — "I see X"

| You see | What it means | Fix |
|---|---|---|
| `untagged > 0` in the Part 3.6 log | One of the 10 CASE branches didn't cover some rows — means NULL or unexpected values upstream | Look for NULL in `STK_TTL`, `ACS_D`, `MSA_FNL_Q`. Usually an upload dropped those. |
| All rows tagged MIX | Classification wasn't run or `MSA_FNL_Q` = 0 everywhere | Re-run MSA then re-run Listing. |
| Everything tagged RL (no TBC/TBL) | `Stock%` too low OR `default_acs_d` too high → every store looks adequate | Raise `Stock%` to 0.7+. |
| Everything tagged TBL | No store stock at all — did you load `ET_STORE_STOCK`? | Upload store stock, rerun grids, rerun listing. |

## Verification

```sql
-- Tag distribution
SELECT OPT_TYPE, COUNT(*) rows_, COUNT(DISTINCT WERKS) stores_
FROM ARS_LISTING
GROUP BY OPT_TYPE
ORDER BY rows_ DESC;

-- Check a specific store × article decision
SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
       STK_TTL, ACS_D, MSA_FNL_Q,
       VAR_COUNT, VAR_FNL_COUNT,
       OPT_TYPE
FROM ARS_LISTING
WHERE WERKS='HN14' AND GEN_ART_NUMBER=1116111940 AND CLR='LT_PST';

-- Count of MIX rows broken by reason (rough heuristic)
SELECT
  CASE
    WHEN MSA_FNL_Q=0 AND STK_TTL = 0 THEN 'safety: nothing anywhere'
    WHEN MSA_FNL_Q=0                  THEN 'warehouse dry'
    WHEN VAR_COUNT>0 AND (CAST(VAR_FNL_COUNT AS FLOAT)/VAR_COUNT < 0.6)
                                      THEN 'sparse sizes'
    ELSE 'other'
  END AS reason,
  COUNT(*) AS rows_
FROM ARS_LISTING
WHERE OPT_TYPE='MIX'
GROUP BY
  CASE
    WHEN MSA_FNL_Q=0 AND STK_TTL = 0 THEN 'safety: nothing anywhere'
    WHEN MSA_FNL_Q=0                  THEN 'warehouse dry'
    WHEN VAR_COUNT>0 AND (CAST(VAR_FNL_COUNT AS FLOAT)/VAR_COUNT < 0.6)
                                      THEN 'sparse sizes'
    ELSE 'other'
  END;
```

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

Function: `_classify_opt_type()` inside `listing.py :: generate_listing` (around line 803–841).

The implementation is a single SQL `UPDATE` with a 10-branch `CASE` expression. The default `ACS_D` fallback is done inline via `ISNULL(NULLIF([ACS_D], 0), {default_acs})`. The `MinSz` rule is conditionally appended to the CASE SQL based on `req.min_size_count > 0` — at 0 it is removed altogether.

## How to update this doc

Update when a branch is added to the CASE, when new MIX criteria are introduced, or when the fallback logic changes. Bump `last_reviewed`.
