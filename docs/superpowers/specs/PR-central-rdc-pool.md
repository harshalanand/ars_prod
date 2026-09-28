# PR ready to open — copy/paste

**Open:** https://github.com/harshalanand/ars_prod/pull/new/feat/central-rdc-pool

**Base:** `ars_v3`  ·  **Compare:** `feat/central-rdc-pool`  ·  **Commit:** `32f0b96`

> Base `ars_v3`, not `ars_v2_prod` — `ars_v3` is this branch's parent and is only
> 1 commit ahead of prod, so targeting prod directly would drag that commit in too.

---

## Title

```
Add central RDC pool allocation (switch-gated, INACTIVE by default)
```

---

## Body — paste everything below the line

---

Under **RDC Scope = All RDCs**, club stock across all warehouses into one pool, allocate against it with **completely unchanged rules**, then tag which warehouse physically ships each line (new Part 8.37 split pass).

**Merging this changes nothing.** Everything is gated on `rdc_mode == 'all'` **AND** business rule `ALC_RDC_CENTRAL_POOL`, which is seeded **INACTIVE**. Turning it off reverts the feature with no deploy, no migration and no data repair. `Own` and `Cross` can never satisfy the mode test.

### Why

Measured on the live master, 2026-09-21:

| | |
|---|---|
| Stocked options at exactly **one** warehouse | **11,303 of 12,647 (89 %)** |
| Listing rows with zero stock at the store's own warehouse while the other holds some | **403,732 of 3,382,403 (11.9 %)** |
| Sessions ever run in `All RDCs` | **0 of 293** — so defect D-1 was latent, not observed |

### The design decision that matters

**`RDC` is never redefined** (BR-RDC-11). It stays the *store's* warehouse; `SRC_RDC` is the *sourcing* one. An `Own`/`Cross` run writes neither, and every consumer reads `ISNULL(SRC_RDC, RDC)` — so **no backfill** of the 17.9 M `ARS_PEND_ALC` / 2.75 M hold / 6.56 M snapshot rows was required, and no column carries two meanings.

### Engine changes (all mode-gated)

- **Part 3.55** clubs `MSA_FNL_Q` / `VAR_COUNT` / `VAR_FNL_COUNT`. All **three** SQL fragments move together — dropping only the join leaves the per-RDC fan-out and D-1 intact. `VAR_COUNT` needs a **two-level** aggregate or a size held at both warehouses counts twice (would have inflated **34.3 %** of options and let the R07 size gate pass on a fiction).
- **`_stage_b_explode`** joins a pre-aggregated MSA sub-query rather than simply dropping `L.RDC = V.RDC`, which would multiply alloc rows per warehouse and break the one-row-per-store-size grain. This is also what fixes **D-2**: a blank/`'ALL'` tag matched no pool key and silently received zero.
- **`POOL_KEYS[0]`** becomes a derived `POOL_RDC` (`'*'` when clubbing). Leaving `RDC` in the key would give two stores of different warehouses two pool groups **each holding the full clubbed quantity**.
- **`SRC_RDC` / `SRC_SPLIT_CNT`** are emitted inside the `SELECT..INTO`, **not** by `ALTER` — `ARS_ALLOC_WORKING` is dropped and rebuilt every run, so an ALTER would be wiped.
- The mode flag is resolved **once** at run start and threaded down. The rules cache has a 30 s TTL, so per-site reads could produce a half-clubbed run.

### Split pass (Part 8.37)

Single-threaded after the MAJ_CAT workers, before parking. Walks lines in waterfall order against **one shared ledger** that SHIP and HOLD both consume.

**A HOLD is never split** (BR-RDC-12) — the hold tracker's PK `(WERKS, VAR_ART, SZ, ALLOC_TYPE)` cannot express two sources. A short hold takes the largest single warehouse and is reduced with `RDC_HOLD_SHORT`.

**No silent truncation:** every reduced line is stamped in `ALLOC_REMARKS`, rolled up so Part 8.5 `OPT_STATUS` judges the real quantity, and counted in the log and cockpit.

### Downstream — the half that decides whether the right warehouse actually ships

- Approve reads **`ARS_ALLOC_RDC_SPLIT`**, not `ARS_ALLOC_HISTORY`, whose `SRC_RDC` is the literal `'MULTI'` on a split line
- the `ARS_PEND_ALC` idempotency guard and `GROUP BY` gained `SRC_RDC`, or a split line's second row was **silently dropped**
- the per-source quantity comes from the split row — the join multiplies the history row once per warehouse (**6 allocated pieces became 12 of pending** before this was caught)
- `msa_service`'s two hold loaders ignored the hold row's warehouse and read the store master, so adding the column alone would have changed nothing for MSA
- split rows are deleted on reject and on revert, and TTL-purged

### UI

RDC Sourcing panel (rendered only when clubbing is live), untagged-store banner in **all three** modes, post-run RDC SPLIT block, five report endpoints. Alloc Review's `RDC` pivot renamed **`Store RDC`** — after clubbing, it and the picklist answer different questions. `ALC_MULTI_PARKED` is refused while clubbing is active: two parked runs would draw the same clubbed stock, an isolation `Own` had only by accident.

### Verification

34 unit tests (`backend/tests/test_rdc_split_pass.py`) plus live-data checks:

| Check | Result |
|---|---|
| V3 no warehouse over-drawn | zero negative balances |
| V4 conservation | **271,869 tagged + 225,002 reduced = 496,871 allocated** |
| Stock conserved through the clubbed sub-query | **6,409,069 → 6,409,069**, no fan-out |
| V13 legacy hold fallback | **234,285 of 234,285** rows resolve identically to today |
| V20 split line → reservations | 2 rows, DH24 2 + DW01 4, total 6 |
| V21 performance | **1.1 s per 10k lines** (budget 15 s) |

Backend suite 43 pass · frontend builds clean.

### ⚠️ Not done — please read before merging

1. **V19 (Cross unchanged) has no baseline.** Every Cross session was purged by the 30-day history TTL (last run 2026-07-25). **One fresh Cross run must be captured before cutover** — Step 2 edits the `else` branch Cross shares with `all`, so that is precisely where a regression would hide.
2. **No full Generate has run on this code.** Every SQL piece is validated against live data, but the first real run is still the proof.
3. Screenshots and the release note are **deferred to cutover** — with the switch off, a user sees no change, so a capture today would photograph the old screen.

### Found but deliberately NOT fixed here

`alloc_pool` prefers the hold row's own `RDC`; `msa_service` ignores it and reads the store master. For any store re-tagged after its hold was created they credit **different warehouses** — currently **234,285 of 234,285 open hold rows (346,258 pcs, 412 stores)**. Aligning them would move the MSA deduction for every open hold, and check V13 exists to prove central pooling changed nothing else. Recorded in `.claude/agents/ars_flow_kb/listing.md`; **needs its own ticket**.

### Spec

`docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md` (+ runbook, worked examples, Step 0 findings)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
