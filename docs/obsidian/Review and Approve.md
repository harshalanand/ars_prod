---
title: Review and Approve
tags: [ars, review, approve]
updated: 2026-07-17
---

# Review and Approve

Stage 6 of the [[Pipeline Overview|pipeline]] — the human gate between "the engine produced numbers" and "these numbers are official." A [[Listing|Generate]] run must never silently overwrite the official allocation.

## State machine
- A completed run lands **PARKED** — not final.
- While a parked run awaits review, **Generate is blocked** for everyone (unless an admin enables multiple parked runs in Settings → Application). Prevents two runs racing into the same drop+recreate tables.
- **Approve** → promotes the working tables into their `*_HISTORY` counterparts; the run becomes the official source the dashboard, [[Pending Allocation and Hold|Hold & Pending]] operate on. Also fires report events `listing.approved` + `pendalc.approved`.
- **Reject** → parked rows discarded (kept until TTL purge); nothing downstream changes.
- Approve/Reject is auditable (who + when). Approve is **idempotent** — a second call returns `{already_approved:true}`.

## What promotes on approve
The [[Data Model|snapshot targets]] (`parked_history._SNAPSHOT_TARGETS`) are **4**: `alloc` (`ARS_ALLOC_WORKING`), `listing_working`, `listing`, `msa_total`. *(The module docstring claims 6 incl. MSA GEN_ART/VAR_ART — those are NOT parked/promoted; see [[Known Risks and Doc Drift]].)* Plus `write_pend_alc` and hold Step A/B — all under one applock.

## Validate-before-approve checklist
| Check | Healthy sign |
|-------|--------------|
| Coverage | stores covered ≈ stores selected |
| Volume | total Alloc Qty within normal range of last cycle |
| Skips | dominated by `MBQ_CAP_*` (expected budget caps), not `POOL_EMPTY` |
| Balance | allocation by RDC/HUB not starving one RDC |
| New items | NEW % matches how much fresh product actually launched |

## Skip-reason quick reference
`MBQ_CAP_MJ` (category budget satisfied — expected) · `MBQ_CAP_FAB/_MVGR` (a sec-cap fence stopped it) · `POOL_EMPTY` (WH out of free stock — recheck MSA/pending) · `R07_SIZE_RATIO_LIVE` (too few sizes to list a TBL) · `R09_HEADROOM_TRIVIAL` · `SEC_CAP_*`. See [[Rule Engine (per_opt)]] for the full taxonomy.

## Key columns
`ALLOC_STATUS` (ALLOCATED/PARTIAL/SKIPPED/INELIGIBLE) · `SKIP_REASON` · `SHIP_QTY`/`HOLD_QTY` — all on `ARS_ALLOC_WORKING`.

Frontend: `/alc-review` (AlcReviewPage) + parked-runs UI. Full column reference in `ARS_DATA_DICTIONARY` (`/data-dictionary`).

## Cross-links
[[Listing]] (approve endpoint) · [[Pending Allocation and Hold]] (what approve commits) · [[Report Generation Hub]] (on-approve events).
