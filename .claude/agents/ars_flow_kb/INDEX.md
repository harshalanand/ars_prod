# ars_flow knowledge base — index

This directory is the single source of truth for ARS listing/allocation rules, invariants, and gotchas. The `ars_flow` subagent reads the relevant file(s) on every invocation and appends new rules as the user states them.

## Files

| Area | File | Owns |
|---|---|---|
| MSA Stock Calculation | [msa.md](msa.md) | 9-step MSA flow, FNL_Q math, MASTER_ALC_PEND merge, output tables |
| Grid Builder | [grid.md](grid.md) | Primary vs sec-cap grids, MJ_RNG_SEG, MJ_FAB, MJ_MICRO_MVGR |
| Listing | [listing.md](listing.md) | listing → listed → alloc pipeline, sec-cap propagation |
| Merge Rules | [merge_rules.md](merge_rules.md) | OPT_TYPE classification (RL/TBC/TBL), rule engine entry points |
| Hold Process / Manage | [hold.md](hold.md) | Park/unpark lifecycle, parked_history, hold dashboard |
| Pending Allocation | [pend_alc.md](pend_alc.md) | Pend lifecycle, alloc_queue, cancellation, revert |

## Cross-cutting invariants (live in [agents/ars_flow.md](../ars_flow.md))

1. OPT uniqueness
2. Growth at MJ+grid only
3. MBQ sparseness (0 = no constraint)
4. Sec-cap grid extras must propagate
5. ACS_D ≠ daily sale
6. RNG_SEG = MRP tier

## How to use

- The ars_flow agent reads this index plus the area-specific file(s) before answering.
- Rules are appended to the `## Recorded rules` section of each file as the user states them. One bullet per rule, dated `YYYY-MM-DD`, with a `Why:` clause.
- If a rule spans multiple areas, it gets appended to each file with a `see also` cross-link.
- Do not delete or rewrite existing entries — only add. If a rule becomes obsolete, mark it `~~superseded~~` and add the replacement.
