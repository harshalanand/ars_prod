# Getting Started — ARS Manual

## BRD — Why this exists

ARS V2 Retail Auto Replenishment decides *what stock should go to which store, and how many pieces of each size* — automatically, across 320+ stores and 240+ major categories. It replaces the old 20-machine Excel process.

This "Getting Started" section orients a brand-new operator, planner, or admin: how to log in, read the dashboard, and find the pipeline. Everything else in this manual assumes you can reach the screens described here.

The whole system is one pipeline run in order: **MSA Stock → Grid Builder → Merge Rules → Listing & Allocation → Review → Hold → Pending Allocation**. Each has its own section in this manual with a business rationale (BRD), the exact rules and formulas (FSD), a click-by-click walkthrough, and a screenshot gallery.

## FSD — How it works

### The three words to know
- **OPT (option)** — one *style + colour* of an article in one store. The unit ARS decides about. Every OPT gets exactly one **OPT_TYPE** per run: **RL** (refill live), **TBC** (to be continued), **TBL** (to be listed) — mutually exclusive at `(WERKS, MAJ_CAT, GEN_ART, CLR)` grain.
- **MBQ (minimum budget quantity)** — how many pieces a store should hold at a given grain. `MBQ = 0` in a secondary grid means "no fence here", not "ship zero".
- **MAJ_CAT (major category)** — the planning unit (e.g. `M_TEES_HS`). Stores, budgets, and runs are organised per MAJ_CAT.

### Where each step lives (sidebar)
| Step | Sidebar location | Output tables |
|---|---|---|
| 1 · MSA Stock | Listing & Alloc → MSA Stock Calculation | `ARS_MSA_TOTAL/GEN_ART/VAR_ART` |
| 2 · Grid Builder | Listing & Alloc → Grid Builder | `ARS_GRID_*` |
| 3 · Merge Rules | Listing & Alloc → Merge Rules | `ARS_GRID_HIERARCHY` merge cols |
| 4 · Listing & Alloc | Listing & Alloc → Listing | `ARS_LISTING`, `ARS_ALLOC_WORKING` |
| 5 · Review | Alloc Review + Parked Runs | history tables |
| 6 · Hold | Reports → Hold Dashboard | hold columns |
| 7 · Pending Allocation | Pending Allocation | PEND lifecycle tables |

### Session & access
- Login is username + password; your RBAC role decides which menus appear.
- The session timer in the top bar shows elapsed login time; sessions expire.

## Recorded rules
<!-- dated appendable rules -->
