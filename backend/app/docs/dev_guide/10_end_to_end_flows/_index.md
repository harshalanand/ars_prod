# End-to-End Process Flows

This section walks the complete journey of data through ARS — from the
moment SAP drops a stock file at 2 AM to the moment a warehouse worker
picks the last carton at 4 PM. Read these in order; each phase consumes
the output of the previous one.

> **How to use this section**
>
> Every phase below is its own page. Each page includes a flowchart,
> step-by-step breakdown, concrete data examples, and "what to check
> when something goes wrong." If you're new to ARS, read them in
> sequence. If you're debugging a specific issue, jump to the phase
> that matches your symptom.

---

## The complete daily flow at a glance

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       DAILY REPLENISHMENT FLOW                            │
│                                                                            │
│  02:00 ─────────► 06:00 ─────────► 09:00 ─────────► 12:00 ─────────► 17:00│
│   night          early morn       working hours    midday        late afternoon│
└──────────────────────────────────────────────────────────────────────────┘

  PHASE 1                              SAP RFC pipeline
  Data Ingestion         ┌──────────► nightly drops files
                         │            ET_STORE_STOCK
   SAP ──► RFC ──► ARS ──┤            ET_MSA_STK
                         │            MASTER_ALC_PEND
                         │            Trend_Sales_*
                         └────────►   vw_master_product

           │
           ▼

  PHASE 2                       Data Checklist
  Validation              Run rules → green/red per table
                          Detects: stale data, missing fields,
                                   referential integrity gaps

           │ (all green)
           ▼

  PHASE 3                MSA Stock Calculation
  MSA Calculation        9-step algorithm
                         Filter → Normalize → Pivot → Merge →
                         Compute FNL_Q → Generate Variants → Aggregate
                         OUT: ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART

           │
           ▼

  PHASE 4                Grid Building
  Grids                  Build pivot grids (one per definition)
                         CREATE → TRUNCATE → SELECT…PIVOT INTO #stage →
                         Chunked INSERT → Post-pivot lookups → Build PK
                         OUT: ARS_GRID_* (one table per grid, 1k–10M rows)

           │
           ▼

  PHASE 5                Listing Generation
  Listing                Apply eligibility rules + ranking
                         OUT: ARS_LISTING_MASTER, PER_OPT_SALE

           │
           ▼

  PHASE 6                Contribution % Execute
  Contribution           Compute per-article share of category sales
                         Apply SSN mappings + fallback chain
                         OUT: ARS_CONTRIB_RESULTS_<jobid>

           │
           ▼

  PHASE 7                Allocation Engine
  Allocation             Pull warehouse stock → apply rule
                          (grade / size-curve / stock / sales / manual)
                          → enforce constraints → write outputs
                         OUT: ARS_ALLOCATION_MASTER + DETAIL + AUDIT

           │
           ▼

  PHASE 8                Review & Override (optional)
  Review                 Planner inspects allocation
                         Override outliers → audit trail
                         OUT: updated ARS_ALLOCATION_DETAIL + AUDIT rows

           │
           ▼

  PHASE 9                Warehouse Dispatch
  Dispatch               Pending Allocation report → pick / pack / ship
                         Updates ARS_pend_alc as items leave warehouse

           │
           ▼

  PHASE 10               Audit & Reporting
  Audit                  audit_log accumulated through every phase
                         Trends Dashboard recap
                         Daily summaries archived
```

---

## Phase responsibilities at a glance

| # | Phase | Owner | Trigger | Duration | Output |
|---|---|---|---|---|---|
| 1 | Data Ingestion | SAP / RFC pipeline | Cron 02:00 | 30-90 min | Source tables refreshed |
| 2 | Validation | Data team | Manual or 06:30 cron | 5 min | Green checklist |
| 3 | MSA Calculation | Planning | Manual after validation | 5-15 min | 3 MSA tables |
| 4 | Grid Building | Planning | Manual after MSA | 10-40 min | N grid tables |
| 5 | Listing Generation | Planning | Manual after grids | 1-2 min | LISTING + PER_OPT_SALE |
| 6 | Contribution % | Planning | Manual after listing | 15-60 min | RESULTS table |
| 7 | Allocation Run | Allocation team | Manual after contrib | 2-30 min | ALLOC tables |
| 8 | Review & Override | Allocation team | After allocation | 10-30 min | Override rows + audit |
| 9 | Dispatch | Operations | Continuous | hours-days | Stock leaves warehouse |
| 10 | Audit | All | Continuous | always-on | audit_log rows |

---

## Conventions used on phase pages

Each phase page follows this structure so you can scan quickly:

1. **Goal** — one sentence
2. **Inputs** — what must be ready before this phase starts
3. **Outputs** — what this phase produces (consumed by next phase)
4. **Flowchart** — ASCII diagram of the steps
5. **Step-by-step breakdown** — numbered, each with:
   - What happens
   - Where in code
   - Example data / SQL / curl
   - Logic explained (the *why*)
6. **What can go wrong** — symptoms + fixes
7. **Performance benchmarks** — time / size expectations

---

## Reading order

Newcomers: read every phase 1 → 10 in sequence. Skim where details
overlap with notes you already read.

Debuggers: jump to the phase matching your symptom. Each page lists
common failures and how to diagnose them.

Architects: read Phase 1, Phase 7, and Phase 10. Those three define
the system's contracts (input data, decision output, audit trail).

---

## Where to fix what

| Symptom | Phase to check |
|---|---|
| "Yesterday's stock is wrong" | Phase 1 — RFC pipeline |
| "MSA returns empty" | Phase 2 (validation), Phase 3 (algorithm) |
| "Grid says ARS_GRID_M3_VAR_ART has 0 rows" | Phase 4 |
| "Article isn't showing up at this store" | Phase 5 — listing eligibility |
| "Allocation distributed weirdly" | Phase 6 (contribution), Phase 7 (rule choice) |
| "Allocation says 100 but warehouse only has 50" | Phase 7 — capacity constraint |
| "Override didn't stick" | Phase 8 — audit row missing? |
| "Pending allocation report shows yesterday's data" | Phase 9 — refresh / archival |
| "Don't know who changed this row" | Phase 10 — audit_log query |
