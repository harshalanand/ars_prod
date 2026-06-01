# Stage D — Finalize (PAK rounding, gates, sec-cap, status)

> **Where we are:** the waterfall produced raw `SHIP_QTY`/`HOLD_QTY` per size. Stage D rounds them to carton sizes, applies the post-waterfall caps, runs the secondary-grid 130% cap, and classifies every row's final `ALLOC_STATUS` + `SKIP_REASON`.

**Code:** `rule_engine_pandas.py:831-1003` (the finalise block) + helpers in `rule_engine_new.py`.

---

## The finalise order (this order matters)

| # | Step | Where |
|---|---|---|
| 0 | PAK_SZ rounding in-memory (per MAJ_CAT, during write-back) | `_apply_pak_sz_rounding_df` (2320) |
| 1 | Ensure `ALLOC_REMARKS` col | `:836` |
| 2 | **PAK_SZ rounding** (SQL safety-net) | `_stage_d_apply_pak_sz_rounding` (382) |
| 3 | **OPT-grain MJ_REQ gate** | `_stage_c_apply_opt_mj_req_gate` (rne:1921) |
| 4 | Zero stranded HOLD on skipped rows | `:860` |
| 5 | Reset `POOL_CONSUMED` on cancelled rows | `:869` |
| 6 | **Recompute `FNL_Q_REM`** | `:876` |
| 7 | **`ALLOC_QTY = SHIP_QTY`** | `:895` |
| 8 | **`ALLOC_STATUS` + `SKIP_REASON` classification** | `:896` |
| 9 | **Secondary-grid 130% cap** (toggle) | `_apply_sec_grid_cap_pre_gate` (rne:3307) |
| 10 | Refund `MJ_REQ_REM` from final SHIP | `:983` |
| 11 | Per-row reason classifier | `_classify_alloc_reason` — **stub no-op** |
| 12 | Reflect totals to `ARS_LISTING_WORKING` | `_stage_d_reflect` |

> **PAK runs before the MJ_REQ gate** because the gate sums OPT-grain SHIP/HOLD to validate each OPT — sizes must be in final pak-aligned form first. **Status SQL (8) runs before sec-cap (9)**, so sec-cap re-stamps `SKIPPED` on the OPTs it blocks.

---

## Step 2 — PAK_SZ rounding

**Layman:** ship quantities must be whole multiples of the carton size (`PAK_SZ`). Half-up rounding; anything below half a pack ships zero.

```python
# half-up: floor((req + 0.5*pak) / pak) * pak
rounded = floor((SHIP_QTY + 0.5*PAK_SZ) / PAK_SZ) * PAK_SZ
gate    = SHIP_QTY < 0.5 * PAK_SZ
SHIP_QTY = 0 if gate else rounded
# pool refund/charge: POOL_CONSUMED -= (old_ship - new_ship), clamp ≥0
```

| Example (PAK_SZ=6) | Result |
|---|---|
| `5 → floor((5+3)/6)*6 = 6` | rounds up (5 ≥ 3) |
| `11 → 12` | rounds up |
| `2 → 2 < 3` | **zeroed**, `SKIP_REASON = PAK_SZ_BELOW_HALF` |

- `PAK_SZ` NULL/0 → coerced to 1 (no-op rounding).
- Two implementations (in-memory `_apply_pak_sz_rounding_df` + SQL safety-net) — both use the identical `floor((req+0.5*pak)/pak)*pak` formula. They must stay bit-identical.
- The band trace token's last `sh=N` is rewritten via `_rewrite_remarks_after_pak` (regex `_BAND_TRACE_RE`), then a `PAK_SZ_ROUND(from=…,to=…)` or `PAK_SZ_GATE(...)` marker appended.

> **Change note:** the half-up `0.5` is hardcoded in **three** places (in-memory ×2, SQL ×1). To make it a setting you must change all three together or the paths diverge.

---

## Step 3 — OPT-grain MJ_REQ gate

**Layman:** a post-waterfall backstop. Per `(WERKS, MAJ_CAT)`, walk OPTs in priority order tracking a budget `req_rem` that starts at `MJ_REQ`. **Full-OPT-or-skip** — an OPT keeps all its SHIP/HOLD or is zeroed entirely.

> **🔴 Docstring is stale.** The docstring says all OPT_TYPEs are gated; the code **gates TBL only**. RL/TBC always ship and merely decrement `req_rem`:
> ```python
> if ot != 'TBL':
>     req_rem -= opt_ship
>     continue        # RL/TBC never gated — they're mandatory replenishment
> ```

The TBL gate test:
```python
budget         = req_rem × (cap_pct / 100)
gate_threshold = 0.5 × opt_mbq           # mbq_gate_factor = 0.5
if budget >= gate_threshold:  ship in full; req_rem -= opt_ship
else:                         SKIP → TBL_MJ_REQ_GATE_FAIL(req_rem,opt_mbq,gate_factor,cap_pct)
```

Properties:
- `req_rem` decreases by `opt_ship` only (not hold).
- **Overshoot allowed** — a shipping OPT can drive `req_rem` negative; later OPTs then fail (budget negative). No clamp by design.
- **Multiple OPTs ship** — the loop never breaks on a skip; a smaller-`OPT_MBQ` TBL can still pass after a larger one failed.
- **First-shipment floor-escape:** if nothing shipped yet in the slice, `req_rem > 0`, and this OPT has the smallest TBL `OPT_MBQ` in the slice, it's admitted despite a breach — prevents a slice from zeroing every TBL when `MJ_REQ` is small.
- `cap_pct = 0` (TBL) → always skip with `TBL_MJ_REQ_GATE_DISABLED`.

**Knobs:** only `tbl_mj_req_cap_pct` has per-row effect (default 100). `rl/tbc_mj_req_cap_pct` only matter for the "all caps ≤ 0 → whole gate is a no-op" early-exit.

> **🔴 Dead code:** `_stage_c_apply_opt_mj_req_cap` (rne:1787) and `_stage_c_apply_tbl_mj_req_cap` (rne:1916) implement a *different* (per-row partial-trim) model. **No caller invokes them.** Delete to avoid confusion with the live gate.

---

## Step 8 — ALLOC_STATUS + SKIP_REASON

**The per-size target** (what counts as fully allocated):
```sql
TBL:     target = max(SZ_MBQ_WH + (I_ROD−1)·SZ_MBQ − SZ_STK, 0)   -- WH buffer once + later rounds
RL/TBC:  target = max(I_ROD·SZ_MBQ − SZ_STK, 0)                    -- rounds × MBQ, no buffer
```

```sql
ALLOC_STATUS = CASE
  WHEN SHIP+HOLD > 0 AND SHIP+HOLD >= target THEN 'ALLOCATED'
  WHEN SHIP+HOLD > 0                          THEN 'PARTIAL'
  ELSE 'SKIPPED' END
```

**SKIP_REASON precedence** (preserve-guard first — critical):
```sql
SKIP_REASON = CASE
  WHEN SKIP_REASON <> ''                            THEN SKIP_REASON     -- preserve gate/cap/PAK reason
  WHEN SHIP=0 AND HOLD=0 AND target <= 0            THEN 'ALREADY_STOCKED'
  WHEN SHIP=0 AND HOLD=0 AND SZ_REQ <= 0            THEN 'NO_REQ'
  WHEN SHIP=0 AND HOLD=0                            THEN 'NO_POOL_MSA'
  ELSE SKIP_REASON END
```

The first arm preserves any reason already set by PAK (`PAK_SZ_*`), the gate (`*_MJ_REQ_GATE_*`), or sec-cap (`SEC_CAP_PRE_*`). Without it the catch-all `NO_POOL_MSA` would stomp the true cause.

---

## Step 9 — Secondary-grid 130% cap

**Layman:** for each Secondary grid, block whole OPTs so the running shipped qty in any `(WERKS, MAJ_CAT, <grid extras>)` bucket stays under `130% × MBQ`. Block-or-ship per OPT, walked in priority order, so lower-priority OPTs get blocked first.

```python
cap_factor = ARS_GRID_BUILDER.sec_cap_pct / 100   # default SEC_CAP_DEFAULT_PCT = 130.0
budget     = MAX(MBQ_<grid>) × cap_factor          # per bucket
# grid only participates if ARS_GRID_BUILDER.sec_cap_applicable = 1
```

**The two skip gates (OR — grid skipped if either):**
```python
if (not gh_applies) or (budget <= 0):   continue
```
- `GH_<HC> = 0` → grid doesn't apply to this MAJ_CAT.
- **`budget <= 0`** → this is the **MBQ=0 sparse skip**: `*_MBQ=0` means "no constraint at this grain", not "zero budget". The grid is skipped, never trimmed.

**Breach + high-demand override:**
```python
if run_before + ship > budget:    # breach
    if opt_req >= 1.0 × opt_mbq:  # SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT = 100
        admit anyway (store needs full display), remark SEC_CAP_PRE_OVERRIDE
    else:
        block → SHIP=0, ALLOC_QTY=0, SKIP_REASON='SEC_CAP_PRE_<grid>' (only if reason empty)
```

**Knobs:** `apply_sec_cap_in_normal` (default true — master toggle); per-grid `ARS_GRID_BUILDER.sec_cap_applicable` + `sec_cap_pct`.

> **Change notes:**
> - The SKIP_REASON is `SEC_CAP_PRE_<grid>` (not `SEC_CAP_<grid>`); the per-grid column is `sec_cap_pct` (not `cap_pct`).
> - `SEC_CAP_DEFAULT_PCT=130.0` and `SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT=100.0` are hardcoded module constants — promote to settings if Ops needs run-time tuning.
> - Lowering the override ratio re-introduces the "cap is a no-op" problem the code comment warns about.
> - Depends on grid extras (`FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG`) reaching alloc — if dropped, the column-existence check silently skips the grid.

---

## Step 11 — `_classify_alloc_reason` is a STUB

```python
def _classify_alloc_reason(conn, alloc_table):
    """stub — the SHIPPED_FULL / BLOCKED_<reason> / INELIGIBLE_<R##> taxonomy
       was referenced by the May-2026 refactor but never landed."""
    logger.debug(...)   # no-op
```

`ALLOC_REASON` is always NULL. It exists only so call sites don't `AttributeError`.

> **🔴 Biggest missing point in Stage D.** The intended per-row reason taxonomy was never implemented. **If someone lands it, it MUST preserve gate/sec-cap `SKIP_REASON`** (mirror the `ISNULL(SKIP_REASON,'')<>''` guard from step 8) or it will overwrite `*_MJ_REQ_GATE_FAIL` / `SEC_CAP_PRE_*` with a generic reason — the latent "cosmetic overwrite" risk.

---

## Worked example — a row flipping at each gate

> Synthetic (local `ARS_ALLOC_WORKING` was empty at query time). One slice `WERKS=H001, MAJ_CAT=MENS_TSHIRT`, `MJ_REQ=30`, one Secondary grid `MJ_FAB` (COTTON `MBQ=40` → budget `52`; LINEN `MBQ=0` → budget 0 = sparse skip). PAK_SZ=6, `tbl_mj_req_cap_pct=100`.

| Pri | OPT | TYPE | OPT_MBQ | FAB | waterfall SHIP | one size |
|---|---|---|---|---|---|---|
| 1 | A | RL  | 12 | COTTON | 12 | S: 5 |
| 2 | B | TBC | 18 | COTTON | 18 | M: 11 |
| 3 | C | TBL | 17 | COTTON | 12 | L: 2 |
| 4 | D | TBL | 17 | COTTON | 10 | S: 10 |
| 5 | E | TBL | 8  | LINEN  | 8  | M: 8 |

**Step 2 (PAK=6):** A/S `5→6`; C/L `2 < 3 → 0, PAK_SZ_BELOW_HALF`; D/S `10→12`; B/M `11→12`; E/M `8→6`.

**Step 3 (MJ_REQ gate, req_rem=30):**
- A (RL): not TBL → ship, req_rem 30→18
- B (TBC): not TBL → ship, req_rem 18→**0**
- C (TBL): budget `0×1.0 = 0`; threshold `0.5×17 = 8.5`; `0 ≥ 8.5`? No; floor-escape? not first → **SKIP** `TBL_MJ_REQ_GATE_FAIL(req_rem=0,opt_mbq=17,...)`
- D (TBL): budget 0 < 8.5 → **SKIP**
- E (TBL): budget 0 < `0.5×8=4` → **SKIP**

**Step 8:** A, B → ALLOCATED/PARTIAL; C/D/E → SKIPPED with gate reason **preserved**.

**Step 9 (sec-cap, COTTON budget 52):** only A,B still shipping → `12 + 18 = 30 ≤ 52` → no breach. (A 6th COTTON OPT shipping 30 would breach → block or override based on `OPT_REQ ≥ OPT_MBQ`.)

**Final:**

| OPT | SHIP | STATUS | SKIP_REASON | flipped at |
|---|---|---|---|---|
| A | 12 | ALLOCATED/PARTIAL | — | PAK round-up |
| B | 18 | ALLOCATED/PARTIAL | — | PAK round-up |
| C | 0 | SKIPPED | TBL_MJ_REQ_GATE_FAIL | step 3 (+ one size PAK_SZ_BELOW_HALF at step 2) |
| D | 0 | SKIPPED | TBL_MJ_REQ_GATE_FAIL | step 3 |
| E | 0 | SKIPPED | TBL_MJ_REQ_GATE_FAIL | step 3 |

(Had `MJ_REQ` been 60: after A,B req_rem=30 → C ships, req_rem 18 → D ships, req_rem 6 → E `6≥4` ships.)

---

## Change / upgrade summary for Stage D

| # | Finding | Severity |
|---|---|---|
| 1 | `_classify_alloc_reason` is an unimplemented stub (ALLOC_REASON always NULL) | 🔴 biggest gap |
| 2 | `_stage_c_apply_opt_mj_req_cap` / `_apply_tbl_mj_req_cap` dead code | 🔴 delete |
| 3 | MJ_REQ gate docstring stale (claims RL/TBC gated; only TBL is) | ⚠ fix docstring |
| 4 | `SEC_CAP_DEFAULT_PCT=130`, override=100, `mbq_gate_factor=0.5`, PAK `0.5` hardcoded | ⚠ promote to settings |
| 5 | RL/TBC `mj_req_cap_pct` sliders effectively informational | document |

---

**Prev:** [Stage C — The Waterfall](/process/stage-c-waterfall) · **Next:** [Secondary Cap deep-dive](/process/sec-cap)
