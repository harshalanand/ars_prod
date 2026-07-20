# Review Results — ARS Manual

## BRD — Why this exists

A Generate run must never silently overwrite the official allocation. Review is the human gate between "the engine produced numbers" and "these numbers are official." It lets a planner inspect a run, compare it to expectations, and consciously **approve** (promote to history) or **reject** (discard) it.

Used by planners and ops leads after every Listing & Allocation run. The business outcome: no bad allocation reaches SAP / the DO desk without a person having looked at it.

## FSD — How it works

### Inputs & outputs
- Reads the parked working tables produced by a run: `ARS_LISTING_WORKING`, `ARS_ALLOC_WORKING` (+ 3 more moved on approve).
- On **Approve**, the 5 working tables are promoted into their history counterparts; the run becomes the official source the dashboard, Hold, and Pending Allocation operate on.
- On **Reject**, parked rows are discarded; nothing downstream changes.

### Rules & invariants
- A completed run lands in **PARKED** state — not final until approved.
- While a parked run awaits review, **Generate is blocked** for everyone unless an admin enables multiple parked runs (Settings → Application). This prevents two people racing runs into the same tables.
- Approve/Reject is auditable — who and when is recorded.

### Validation gates — validate first, then create
Before approving, validate the run against these checks (Alloc Review + Parked Runs surface the data):
| Check | Healthy sign |
|---|---|
| Coverage | stores covered ≈ stores selected |
| Volume | Total Alloc Qty within normal range of last cycle |
| Skips | dominated by `MBQ_CAP_*` (expected budget caps), not `POOL_EMPTY` |
| Balance | Allocation-by-RDC / HUB not starving one RDC |
| New items | NEW % matches how much fresh product actually launched |

Only after these pass should you Approve (create history).

### Skip-reason quick reference
| Reason | Meaning | Usual action |
|---|---|---|
| `MBQ_CAP_MJ` | category budget already satisfied | expected |
| `MBQ_CAP_FAB` / `_MVGR` | a sec-cap fence stopped it | check grid if unexpected |
| `POOL_EMPTY` | warehouse ran out of free stock | recheck MSA / pending |
| `R07_SIZE_RATIO_LIVE` | too few sizes to list a TBL | lower Size Cov % only if intentional |

### Key columns
| Column | Meaning | Source |
|---|---|---|
| `ALLOC_STATUS` | ALLOCATED / PARTIAL / SKIPPED / INELIGIBLE | `ARS_ALLOC_WORKING` |
| `SKIP_REASON` | why a row shipped nothing | `ARS_ALLOC_WORKING` |
| `SHIP_QTY` / `HOLD_QTY` | pieces shipped / reserved | `ARS_ALLOC_WORKING` |

See the [Data Dictionary](/data-dictionary) for every column.

## Recorded rules
<!-- dated appendable rules -->
