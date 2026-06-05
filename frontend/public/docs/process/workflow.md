# Full Workflow Chart

> The complete journey of a piece of stock — from SAP nightly dump to a store-bound DO.

---

## End-to-end pipeline

```mermaid
flowchart TD
  subgraph S1["1 — RAW UPLOAD"]
    U1[SAP nightly dump<br/>stock + sales]
    U2[Planner MBQ sheet]
    U3[Master tables<br/>retail_gen_article<br/>retail_variant_article]
  end

  subgraph S2["2 — MSA STOCK CALC (9 steps)"]
    M1[Filter SLOC] --> M2[Normalize cols]
    M2 --> M3[Fill dims<br/>WERKS / MAJ_CAT / GEN_ART]
    M3 --> M4[Segment APP / GM]
    M4 --> M5[Pivot by SLOC]
    M5 --> M6[Merge MASTER_ALC_PEND]
    M6 --> M7["FNL_Q = max(STK − PEND, 0)"]
    M7 --> M8[Generate color variants]
    M8 --> M9[Aggregate → 3 tables]
  end

  subgraph S3["3 — LISTING"]
    L1[Read FNL_Q + MBQ] --> L2[Detect OPT grain<br/>WERKS+MAJ_CAT+GEN_ART+CLR]
    L2 --> L3[Assign OPT_TYPE<br/>RL / TBC / TBL]
    L3 --> L4[Filter by Cont_presets]
    L4 --> L5[Write listed_opt table]
  end

  subgraph S4["4 — PRIMARY CAP (MJ-grid)"]
    P1[Apply MJ_MBQ × cap%] --> P2[Apply growth %]
    P2 --> P3["Sequential gate<br/>RL → TBC → TBL"]
    P3 --> P4["req_rem walk<br/>skip if &lt; 0.5×OPT_MBQ"]
  end

  subgraph S5["5 — SECONDARY CAP"]
    C1[Compute *_MBQ per sec-grid<br/>FAB / MACRO / MICRO / M_VND_CD / RNG_SEG]
    C1 --> C2{"sec_qty / sec_MBQ<br/>≤ 1.30?"}
    C2 -- yes --> C3[Allow]
    C2 -- no --> C4[Block]
    C1 --> C5["If *_MBQ = 0<br/>→ no constraint"]
  end

  subgraph S6["6 — ALLOCATION"]
    A1[Walk WERKS × MAJ_CAT × OPT] --> A2["Subtract PEND<br/>FNL_Q = max(STK−PEND, 0)"]
    A2 --> A3[Split into variants by CLR]
    A3 --> A4[Write alloc_header]
    A3 --> A5[Write alloc_detail]
  end

  subgraph S7["7 — PENDING / DO LIFECYCLE"]
    D1[alloc_detail → pend_alc] --> D2[DO entry by store]
    D2 --> D3[Reconciliation]
    D3 --> D4[Cleared OR back to pend]
  end

  S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7
  S6 --> EXP[Export → SAP]
  S6 --> REV[Alloc Review<br/>session-wise archive]
```

---

## Where each stage runs in code

| Stage | File | Function |
|---|---|---|
| MSA | `backend/app/services/msa_service.py` | `run_msa()` (9 steps) |
| Listing | `backend/app/services/listing_allocator.py` | `list_opts()` |
| Listing API | `backend/app/api/v1/endpoints/listing.py` | POST `/listing/run` |
| Primary cap + sec-cap + alloc | `backend/app/services/rule_engine_pandas.py` | `run_rules()` |
| Archive | `backend/app/services/parked_history.py` | session snapshot |

> **Pandas is the production default** — `rule_engine_pandas.py` is the live path. The other engines (`rule_engine_new.py`, `rule_engine_parallel_sql.py`, `rule_engine_parallel_python.py`) exist for benchmarking only.

---

## How a single OPT flows through

```mermaid
sequenceDiagram
  participant UI as Listing UI
  participant API as listing.py
  participant LST as listing_allocator
  participant RUL as rule_engine_pandas
  participant DB as SQL Server (HOPC560)

  UI->>API: POST /listing/run<br/>(cap%, growth%, mode=pandas)
  API->>DB: read MSA + Cont_presets + MBQ
  API->>LST: list_opts(stores, majcats)
  LST-->>API: listed_opt table (RL/TBC/TBL flagged)
  API->>RUL: run_rules(listed_opt, caps, growth)
  RUL->>RUL: 1. apply primary cap (MJ_MBQ × cap%)
  RUL->>RUL: 2. sequential req_rem gate
  RUL->>RUL: 3. sec-cap check (1.30× rule)
  RUL->>RUL: 4. split into variants
  RUL->>DB: write alloc_header + alloc_detail
  RUL-->>API: summary counts
  API-->>UI: 200 OK + run_id
```

---

## Read-this-next

- **[Listing Process](/process/listing)** — what makes an OPT eligible
- **[Primary & Sec-Cap](/process/sec-cap)** — math behind the caps
- **[Allocation Process](/process/allocation)** — how `alloc_detail` is built
