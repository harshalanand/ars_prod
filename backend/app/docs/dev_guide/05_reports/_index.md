# Reports, Data Validation & Allocations — Developer Reference

This note covers three sidebar sections that share output rather than
input — they are how the rest of the org *reads* what ARS produced.

---

## A. Reports → Pending Allocation

`frontend/src/pages/PendAlcReportPage.jsx`

### What it shows

Every row in `ARS_pend_alc` (pending allocations) joined with
`vw_master_product` for human-readable descriptions. This is the
warehouse's pick list — what's been allocated but not yet shipped.

### Default columns

`RDC, ST_CD, MATNR, QTY, MAJ_CAT, DIV, SEG, GEN_ART_NUMBER, CLR`

Toggle others on/off via column-visibility dropdown.

### Files

| File | Role |
|---|---|
| `frontend/src/pages/PendAlcReportPage.jsx` | UI — ag-Grid with filters and global search |
| `app/api/v1/endpoints/reports.py` | Routes — `/pend-alc`, `/pend-alc/distinct/{column}`, `/pend-alc/download` |

### Performance

- Default display **caps at 2000 rows** for browser responsiveness.
- Global search is O(n × m) — every row × every column. For >100k rows, switch to server-side filtering.
- Add a covering index: `CREATE INDEX IX_pend_alc_RDC_ST_MATNR ON ARS_pend_alc(RDC, ST_CD, MATNR)`.

### Common change: add a new visible column

1. The endpoint `GET /pend-alc` returns all columns by default.
2. Frontend `PendAlcReportPage.jsx` → `defaultVisibleColumns` array — add the column name.
3. The frontend column-picker auto-discovers any column the API returned.

### Example: download a filtered subset

```bash
curl "$API/api/v1/reports/pend-alc/download?WERKS=DH24&format=csv" \
  -H "Authorization: Bearer $TOKEN" -o pending_DH24.csv
```

---

## B. Data Validation

Two pages that catch bad data **before** allocations run on it.

### B.1 Store SLOC Validation — `frontend/src/pages/StoreStockPage.jsx`

#### What it does

Manage which SLOCs are active for each store. The catch: when ops adds
a new store or SLOC in SAP, it shows up in `ET_STORE_STOCK` but ARS
doesn't know what to do with it. This page lets you configure new SLOCs.

#### Files

| File | Role |
|---|---|
| `frontend/src/pages/StoreStockPage.jsx` | UI |
| `app/api/v1/endpoints/sloc_validation.py` | Routes |
| `app/services/sloc_validation_service.py` | Sync logic |

#### Workflow

```
Click Sync → backend scans ET_STORE_STOCK for SLOCs not in
             ARS_STORE_SLOC_SETTINGS → inserts pending rows
                       │
                       ▼
User toggles Active/Inactive, sets KPI label per SLOC
                       │
                       ▼
Save → MSA + Grid Builder pick up changes on next run
```

#### Common change: auto-classify new SLOCs

`sloc_validation_service.py` → during `sync_new_slocs`, look up the
SLOC in a config map (e.g. `V*` = `STK`, `H*` = `HOLD`) and pre-fill
the KPI label. Cuts manual work.

### B.2 Data Checklist — `frontend/src/pages/DataChecklistPage.jsx`

#### What it does

Per-table validation: freshness, missing required fields, referential
integrity. Green/yellow/red per check.

#### Files

| File | Role |
|---|---|
| `app/api/v1/endpoints/checklist.py` | Routes |
| `app/services/checklist_service.py` | Rule engine |
| `app/docs/checklist_rules/*.json` (if used) | Rule definitions |

#### Adding a new rule

1. Define the rule:
   ```json
   {
     "id": "msa_total_stock_not_negative",
     "table": "ARS_MSA_TOTAL",
     "type": "value_check",
     "sql": "SELECT COUNT(*) FROM ARS_MSA_TOTAL WHERE STK_TOTAL < 0",
     "expect": "= 0",
     "severity": "error"
   }
   ```
2. Drop into the rules folder or insert into the rules table.
3. Click Run on the Data Checklist page; the rule appears.

#### Performance

- **No incremental check** — re-running validates everything.
- For tables with `updated_at`, add an `incremental_only` flag and check rows where `updated_at > last_run_at`.

---

## C. Allocations

Allocations is where everything you've prepared actually becomes a
shipment instruction. It's the most business-critical section.

### C.1 Pages

| Page | File | Purpose |
|---|---|---|
| Allocations list | `AllocationsPage.jsx` | Browse historical allocations |
| New allocation | `NewAllocationPage.jsx` | Create one (manual / rule-based) |
| Allocation detail | `AllocationDetailPage.jsx` | View & override one allocation |

### C.2 Two engines

There are two implementations on the backend:

| Engine | File | Status |
|---|---|---|
| Allocations v1 | `app/api/v1/endpoints/allocations.py` + `app/services/allocation_engine.py` | Original, simpler |
| Allocations v2 (score-based, replaces 8-level Excel waterfall) | `app/api/v1/endpoints/allocation_engine.py` | Newer, more capable |

The frontend currently calls v1. Migration to v2 is in flight; both
write to the same `ARS_ALLOCATION_MASTER` + `ARS_ALLOCATION_DETAIL`
tables, so reading code is unaffected.

### C.3 Allocation types

| Type | Logic |
|---|---|
| **store_grade** | Distribute by A/B/C/D grade ratios |
| **size_curve** | Apply size × colour grid distribution |
| **stock_based** | Allocate proportional to current store stock |
| **sales_based** | Allocate proportional to recent sales |
| **manual** | User specifies quantities directly |

### C.4 Run flow

```
POST /allocations/run
   ↓
allocation_engine.run_allocation(spec)
   ├── pull warehouse stock (ARS_WAREHOUSE_STOCK)
   ├── pull sales history if sales-based
   ├── apply rule type (one of the five above)
   ├── enforce min/max constraints per store
   ├── enforce warehouse capacity (iteratively)
   ├── apply manual overrides
   └── write ARS_ALLOCATION_MASTER + DETAIL + AUDIT
```

### C.5 Example: create an allocation via API

```bash
curl -X POST "$API/api/v1/allocations/run" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "store_grade",
    "products": ["1234567","1234568"],
    "stores": ["DH24","DH25","DH26"],
    "grade_ratios": {"A": 4, "B": 2, "C": 1, "D": 1},
    "min_per_store": 1,
    "max_per_store": 12
  }'
```

Returns `allocation_code`. Detail at `GET /allocations/{code}`.

### C.6 Override flow

```
User opens AllocationDetailPage → edits a quantity inline
                ↓
   POST /allocations/{code}/override { store, article, qty }
                ↓
   allocation_engine.apply_override(...) writes:
       - ARS_ALLOCATION_DETAIL.qty (new value)
       - ARS_ALLOCATION_AUDIT (who, when, old, new)
```

### C.7 Performance

- **Size-curve allocation** across 300 stores × 50k SKUs is O(n × m) —
  vectorise with NumPy/pandas, not Python loops. ~30× speed-up.
- **Warehouse capacity iteration** — when a store exceeds capacity, the
  current code recalculates the entire allocation. Better: redistribute
  only the overflow.
- **Manual override per row** is slow over many edits. Batch with
  MERGE for >100 overrides.

### C.8 Common change: add a new allocation type

1. Backend: in `allocation_engine.py`, add a function `_allocate_<type>(spec)`.
2. Register it in the `_ALLOCATION_TYPES` dispatch dict.
3. Frontend: add the option to `NewAllocationPage.jsx`'s type dropdown
   and any rule-specific config inputs.
4. The result table format is identical — no schema change needed.

### C.9 Audit

Every allocation run, every override, every cancel produces a row in
`ARS_ALLOCATION_AUDIT`. **Never** modify allocations directly via SQL —
always go through the engine, otherwise the audit chain breaks and ops
loses traceability.

---

## Cross-cutting tips

- **Reports are read-heavy** — RCSI on the data DB ensures these don't block uploads.
- **Validation should be cheap** — don't run it inside every API handler; run it as a scheduled job.
- **Allocations are write-heavy + audit-heavy** — keep the audit table indexed on `(allocation_code, created_at)` for fast drill-downs.
