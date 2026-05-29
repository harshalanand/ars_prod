---
title: BDC Creation — The Warehouse Export
category: Data Prep
order: 60
source: backend/app/api/v1/endpoints/bdc.py
last_reviewed: 2026-04-20
---

# BDC Creation

> **═══ USER GUIDE ═══**

## Current BDC state (live)

<!-- @metric sql="SELECT COUNT(*) FROM ARS_HOLD_ARTICLE_BDC" label="Articles currently on hold" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_DIVISION_DELETE_BDC" label="(Store, Division) exclusion pairs" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC" label="(Store, MAJ_CAT) exclusion pairs" -->

## In plain English

After ARS decides what each store should get, **someone has to tell the warehouse**. BDC (Bulk Delivery Confirmation) is the file format the warehouse and SAP team use to actually pick and ship. BDC Creation takes ARS's allocation output and turns it into a spreadsheet the warehouse can upload straight into SAP.

Think of it as the **last-mile translator**: ARS speaks in "options", warehouses speak in "variants with quantities per store". BDC does the conversion, applies hold/exclusion lists, and hands you a file you email to the warehouse or push into SAP.

## When to use this

Run BDC:

- **After** a successful Listing Generation (BDC reads from the alloc output).
- **After** reviewing the allocation and confirming the store-level numbers look sane.
- **Once per cycle** — typically the same day as the allocation run or the next morning.

**Don't run** if:
- Allocation hasn't finished (BDC will export zeros or last run's numbers).
- Hold or exclusion lists are outdated — you'll ship to banned destinations.

## Before you start

| Check | How |
|---|---|
| Allocation data present | `ARS_ALLOC_WORKING` has `SHIP_QTY > 0` rows |
| Hold list up to date | `ARS_HOLD_ARTICLE_BDC` has the current hold VAR_ARTs |
| Division deletes set | `ARS_DIVISION_DELETE_BDC` has stores that don't carry certain divisions |
| MAJ_CAT deletes set | `ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC` has `(store, MAJ_CAT)` pairs to exclude |

## Step-by-step: how to run it

### 1. Go to BDC Creation
Sidebar → **Data Preparation** → **BDC Creation**. URL `/bdc`.

### 2. Pick source
Two options:
- **Use current allocation** — reads from `ARS_ALLOC_WORKING`.
- **Upload a file** — if you have an external allocation file (e.g., from a partner system).

### 3. Set filters (optional)
- **RDC** — restrict to one warehouse if you're running multiple in a day.
- **Date** — the target dispatch date.
- **DIV** — filter by division if you only want Men / Women / Kids.

### 4. Click Process
The 6-step pipeline runs:

```
1. Aggregate input → (VAR_ART, ST_CD, RDC) with SUM(ALLOC_QTY), carry PEND_QTY
2. Join vw_master_product → GEN_ART, DIV, MAJ_CAT, CLR
3. Remove rows where VAR_ART is in ARS_HOLD_ARTICLE_BDC      (hold articles)
4. Remove KIDS rows for stores in ARS_DIVISION_DELETE_BDC    (division bans)
5. Remove (ST_CD, MAJ_CAT) matches in ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC
6. Emit BDC format
```

Result downloads as a CSV or Excel.

### 5. Review the file
It will have these columns:

```
VAR_ART, ST_CD, RDC, DIV, MAJ_CAT, GEN_ART_NUMBER, CLR,
TOTAL_QTY, PEND_QTY, WITHOUT_PENDING
```

`WITHOUT_PENDING = TOTAL_QTY - PEND_QTY` — this is what the warehouse **actually picks and ships** this cycle. `PEND_QTY` represents units already in transit from the previous cycle.

### 6. (Optional) Push to SAP / master
If you tick **"Upsert to ARS_ALLOCATION_MASTER"**, BDC also writes rows into that master table for downstream tracking and reporting.

## The 3 exclusion lists — what they're for

| List | Purpose | Example |
|---|---|---|
| `ARS_HOLD_ARTICLE_BDC` | Articles on hold (quality, legal, stock-freeze) — never ship these. | `VAR_ART 987654 on hold due to labelling issue` |
| `ARS_DIVISION_DELETE_BDC` | Stores that don't carry certain divisions — e.g., adult-only stores. | `HN14 doesn't stock KIDS` |
| `ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC` | Finer-grained: specific (store, MAJ_CAT) combinations to skip. | `HN21 stops receiving M_TEES_HS from next month` |

Keep these lists current — they are the safety net between ARS's allocation and a physical shipment.

## A worked example

ARS allocated `VAR_ART=123456` for store `HN14` at quantity `12`. `PEND_QTY` from last cycle is `3`.

| Step | Outcome |
|---|---|
| 1 — Aggregate | Row: `VAR_ART=123456, ST_CD=HN14, RDC=DW01, ALLOC_QTY=12, PEND_QTY=3` |
| 2 — Master join | Add: `DIV=MEN, MAJ_CAT=M_TEES_HS, GEN_ART=1116111940, CLR=LT_PST` |
| 3 — Hold check | `123456` not in `ARS_HOLD_ARTICLE_BDC` → keep |
| 4 — Division check | `HN14` doesn't exclude `MEN` → keep |
| 5 — MAJ_CAT check | `(HN14, M_TEES_HS)` not excluded → keep |
| 6 — Emit | `TOTAL_QTY=12, PEND_QTY=3, WITHOUT_PENDING=9` |

Warehouse sees: *"Pick 9 new units of `123456` for `HN14` at `DW01`."*

## Common questions (FAQ)

**Q: Why is `WITHOUT_PENDING` negative sometimes?**
Because last cycle's pending (`PEND_QTY`) was higher than this cycle's total. It means the earlier allocation was reduced or cancelled. Negative values should be treated as "0 new units to ship" — the warehouse is still ahead. Contact the dev team if you see this recurring; could indicate a drift bug.

**Q: A store is supposed to stop receiving KIDS but still gets some. Why?**
Either `ARS_DIVISION_DELETE_BDC` is missing the `(store, KIDS)` row, or the article's DIV in `vw_master_product` isn't actually `KIDS`. Check both.

**Q: An article on hold still appears in BDC. Why?**
Either `ARS_HOLD_ARTICLE_BDC` hasn't been updated, or the hold list uses a different key (e.g., `GEN_ART_NUMBER` instead of `VAR_ART`). The filter uses `VAR_ART`. Update the list.

**Q: I need to re-run only for one store. Can I?**
Yes — filter by ST_CD in the Process form. The output file will have only that store.

**Q: How do I compare this cycle to last cycle?**
`ARS_ALLOCATION_MASTER` keeps history if you tick "Upsert to master". Query by `DOC_DATE` to see week-over-week trends.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| File downloads empty | Allocation had no rows or filters too strict | Verify `ARS_ALLOC_WORKING` has `SHIP_QTY>0`; relax your RDC/DIV filter. |
| Big drop in row count | Exclusion lists too broad | Review the 3 lists for accidental blanket rules. |
| Warehouse rejects file | Column headers renamed downstream | Export without post-processing; names must match SAP spec exactly. |
| "Master join" step loses many rows | `vw_master_product` missing entries for new articles | Refresh master product data, then re-run BDC. |

## Verification

```sql
-- Rows in the exclusion lists
SELECT COUNT(*) FROM ARS_HOLD_ARTICLE_BDC;
SELECT COUNT(*) FROM ARS_DIVISION_DELETE_BDC;
SELECT COUNT(*) FROM ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC;

-- After BDC upsert, what's committed for today?
SELECT SUM(WITHOUT_PENDING) AS to_ship,
       SUM(PEND_QTY)        AS already_dispatched,
       COUNT(DISTINCT ST_CD)  AS stores
FROM ARS_ALLOCATION_MASTER
WHERE DOC_DATE = CAST(GETDATE() AS DATE);

-- Stores that got zero (should be short list)
SELECT DISTINCT am.ST_CD FROM ARS_ALLOCATION_MASTER am
WHERE DOC_DATE = CAST(GETDATE() AS DATE)
GROUP BY am.ST_CD
HAVING SUM(WITHOUT_PENDING) = 0;
```

## Settings you can change

| Setting | Effect |
|---|---|
| RDC filter | Run BDC for one warehouse at a time |
| DIV filter | Run BDC for one division only |
| "Upsert to master" | Persist results in `ARS_ALLOCATION_MASTER` for history |
| Exclusion lists (`ARS_HOLD_ARTICLE_BDC`, etc.) | Edit via Data Management → Tables |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Endpoint: `POST /api/v1/bdc/process` — `bdc.py`.
- 6-step pipeline documented above.
- Output format matches the SAP upload template — don't rename columns downstream.
- Permission required: `BDC_VIEW` / `BDC_EXECUTE` (depending on operation).

## How to update this doc

Update when a new exclusion list is introduced, a new column is added to output, or the SAP-format header changes. Bump `last_reviewed`.
