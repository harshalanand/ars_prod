---
title: Pending Allocation (ARS_PEND_ALC) — Preventing Double-Allocation
category: Allocation
order: 55
source: backend/app/services/pend_alc_service.py, backend/app/services/msa_service.py, backend/app/services/parked_history.py, backend/app/api/v1/endpoints/pend_alc.py
last_reviewed: 2026-05-02
---

# Pending Allocation (ARS_PEND_ALC)

> **The core problem this solves:** Once an allocation run is approved, the stock is promised — but SAP doesn't deduct it from the warehouse until a Delivery Order (DO) is generated, which may take 1–5 days. If MSA runs again before the DO is issued, it sees the full stock and allocates the same units to someone else. `ARS_PEND_ALC` closes this gap.

## Live status

<!-- @metric sql="SELECT COUNT(*) FROM ARS_PEND_ALC WHERE IS_CLOSED = 0" label="Open pending rows" -->

<!-- @metric sql="SELECT ISNULL(SUM(PEND_QTY),0) FROM ARS_PEND_ALC WHERE IS_CLOSED = 0" label="Total units still pending DO" -->

<!-- @metric sql="SELECT ISNULL(SUM(DO_QTY),0) FROM ARS_PEND_ALC WHERE LAST_DO_AT >= CAST(GETDATE()-1 AS DATE)" label="Units DO'd in last 24 h" -->

<!-- @metric sql="SELECT COUNT(*) FROM ARS_PEND_ALC WHERE IS_CLOSED = 1" label="Closed rows (fully DO'd)" -->

---

## The complete lifecycle

```
┌──────────────────────────────────────────────────────────────────────────┐
│  PHASE 1 — Allocation Run                                                │
│  Listing & Allocation page → session approved → ARS_ALLOC_PARKED        │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ approve_parked()
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PHASE 2 — Approve Parked                                                │
│  ARS_ALLOC_PARKED → ARS_ALLOC_HISTORY   (atomic copy)                   │
│  ARS_PEND_ALC ← INSERT one row per (RDC, Article)  ◄── NEW              │
│    ALLOC_QTY = sum of approved units, DO_QTY = 0                        │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ PEND_QTY = ALLOC_QTY - DO_QTY
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PHASE 3 — MSA Stock Calculation  (next run, same or next day)           │
│  Step 6: reads ARS_PEND_ALC WHERE IS_CLOSED=0                           │
│  FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)                        │
│  ✔ ARS stock already deducted → no double-allocation                    │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ SAP generates DO (1-5 days later)
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PHASE 4 — Daily DO Entry                                                │
│  User uploads CSV or enters rows in Pending Allocation → Daily DO Entry  │
│  POST /pend-alc/do-update  →  apply_do_deductions()                     │
│    ARS_PEND_ALC.DO_QTY += do_qty                                        │
│    IS_CLOSED = 1  when  DO_QTY >= ALLOC_QTY                             │
│    ARS_NL_TBL_HOLD_TRACKING.HOLD_REM -= do_qty  (for hold articles)    │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ row IS_CLOSED=1 → excluded from PEND
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  PHASE 5 — Next MSA Run                                                  │
│  DO'd articles have IS_CLOSED=1 → PEND_QTY contribution = 0            │
│  FNL_Q rises back to full STK_QTY for those articles                    │
│  → Correct available stock, no phantom deductions                        │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Worked example

### Setup

| Field | Value |
|-------|-------|
| RDC | DC01 |
| Article | 1234567890 |
| Season | AW26 |
| MAJ_CAT | MENS SHIRTS |
| Warehouse stock (STK_QTY) | 200 units |

---

### Step 1 — Before allocation (fresh MSA run)

MSA reads STK_QTY = 200, no pending rows.

```
FNL_Q = max(200 − 0 − 0, 0) = 200 units available
```

ARS_PEND_ALC: **empty**

---

### Step 2 — Allocation run approved

User runs Listing & Allocation for season AW26, approves the parked run.
`approve_parked()` calls `write_pend_alc()` which inserts:

| SESSION_ID | RDC | ARTICLE_NUMBER | ALLOC_QTY | DO_QTY | PEND_QTY | IS_CLOSED |
|------------|-----|----------------|-----------|--------|----------|-----------|
| AW26-001   | DC01 | 1234567890    | 50        | 0      | **50**   | 0 |

---

### Step 3 — MSA runs again (same evening or next morning)

MSA Step 6 reads ARS_PEND_ALC: finds PEND_QTY = 50 for (DC01, 1234567890).

```
FNL_Q = max(200 − 50 − 0, 0) = 150 units available
```

**Without ARS_PEND_ALC:** MSA would see 200 and re-allocate the same 50 units → double-allocation.
**With ARS_PEND_ALC:** MSA correctly sees only 150 remaining.

---

### Step 4a — First DO batch (Day 2): 30 units issued by SAP

User enters in Daily DO Entry: DC01 / 1234567890 / DO_QTY = 30.

| SESSION_ID | RDC | ARTICLE_NUMBER | ALLOC_QTY | DO_QTY | PEND_QTY | IS_CLOSED |
|------------|-----|----------------|-----------|--------|----------|-----------|
| AW26-001   | DC01 | 1234567890    | 50        | **30** | **20**   | 0 |

Next MSA run:
```
FNL_Q = max(200 − 20 − 0, 0) = 180 units available
```
The 30 DO'd units are now real SAP deductions → MSA correctly adds back that headroom.

---

### Step 4b — Second DO batch (Day 3): remaining 20 units issued

User enters: DC01 / 1234567890 / DO_QTY = 20.

| SESSION_ID | RDC | ARTICLE_NUMBER | ALLOC_QTY | DO_QTY | PEND_QTY | IS_CLOSED |
|------------|-----|----------------|-----------|--------|----------|-----------|
| AW26-001   | DC01 | 1234567890    | 50        | **50** | **0**    | **1** |

Row is now **CLOSED**. MSA Step 6 skips IS_CLOSED=1 rows.

Next MSA run:
```
FNL_Q = max(200 − 0 − 0, 0) = 200 units
```
(The 50 units are now reflected in the warehouse system via SAP — STK_QTY will have dropped when the warehouse syncs.)

---

## What happens with HOLD articles?

Articles allocated via NL/TBL hold (HOLD_QTY path) work in parallel. When DO qty is uploaded:

```python
UPDATE ARS_NL_TBL_HOLD_TRACKING
   SET HOLD_REM  = max(HOLD_REM - do_qty, 0),
       IS_CLOSED = CASE WHEN HOLD_REM - do_qty <= 0 THEN 1 ELSE 0 END
WHERE WERKS = rdc AND VAR_ART = article AND IS_CLOSED = 0
```

The same single DO upload closes both PEND_QTY (via ARS_PEND_ALC) and HOLD_QTY (via ARS_NL_TBL_HOLD_TRACKING) simultaneously.

---

## ARS_PEND_ALC table structure

```sql
CREATE TABLE dbo.ARS_PEND_ALC (
    ID             BIGINT IDENTITY(1,1),
    SESSION_ID     NVARCHAR(50)  NOT NULL,   -- allocation session that created this row
    RDC            NVARCHAR(20)  NOT NULL,   -- warehouse / distribution centre code
    ARTICLE_NUMBER NVARCHAR(30)  NOT NULL,   -- variant article (colour + size)
    MAJ_CAT        NVARCHAR(50)  NULL,       -- major category (for reporting)
    ALLOC_QTY      FLOAT         NOT NULL DEFAULT 0,  -- total approved allocation
    DO_QTY         FLOAT         NOT NULL DEFAULT 0,  -- cumulative DO qty received
    PEND_QTY       AS (ALLOC_QTY - DO_QTY)  PERSISTED,  -- what MSA deducts
    APPROVED_AT    DATETIME      NOT NULL DEFAULT GETDATE(),
    LAST_DO_AT     DATETIME      NULL,       -- timestamp of most recent DO entry
    IS_CLOSED      BIT           NOT NULL DEFAULT 0,  -- 1 when DO_QTY >= ALLOC_QTY
    CONSTRAINT PK_ARS_PEND_ALC PRIMARY KEY (ID)
);
```

`PEND_QTY` is a **persisted computed column** — the database calculates it automatically. Application code never writes it directly.

---

## MSA formula (Step 6 → Step 7)

```
Step 6  reads ARS_PEND_ALC WHERE IS_CLOSED=0 AND PEND_QTY>0
        grouped by (RDC, ARTICLE_NUMBER) → sum(PEND_QTY) per article per RDC

Step 6.5 reads ARS_NL_TBL_HOLD_TRACKING WHERE IS_CLOSED=0 AND HOLD_REM>0
          joined to store master → HOLD_QTY per (RDC, ARTICLE)

Step 7  FNL_Q = max(STK_QTY − PEND_QTY − HOLD_QTY, 0)
```

`MASTER_ALC_PEND` (legacy SAP-sourced table) is **no longer used**. `ARS_PEND_ALC` is the single source of truth for all ARS-driven pending deductions.

---

## Navigation

| Page | Path | Purpose |
|------|------|---------|
| Pending Allocation → Overview | `/pend-alc/overview` | See open sessions, MAJ_CAT breakdown, totals |
| Pending Allocation → Daily DO Entry | `/pend-alc/do-entry` | Upload or manually enter DO quantities |
| Reports → Pending Allocation | `/reports/pend-alc` | Cross-session pending report |

---

## Checklist for daily operations

- [ ] Run MSA each morning before allocations begin
- [ ] After SAP generates DOs (usually 14:00–16:00), open Daily DO Entry
- [ ] Upload DO CSV or enter rows manually (RDC, Article, DO_QTY)
- [ ] Verify PEND_QTY drops to 0 for fully-covered articles
- [ ] If a session has been pending > 5 days with no DO, escalate to SAP team
- [ ] Run MSA again after DO entry to get corrected FNL_Q before next allocation
