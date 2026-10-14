# Refreshing the ARS Manual

The manual dossiers (`frontend/public/docs/manual/<module>.md`) are the single
source of truth for ARS — read by Claude as "ARS memory" before changing code.
Keep them current when the pipeline changes. (The in-app Training Manual viewer
and its screenshot gallery were removed on 2026-10-14; the dossiers stay.)

## What can go stale, and how to refresh each

| Artifact | Where | Refresh command / action |
|---|---|---|
| **Column formulas** (`ARS_DATA_DICTIONARY`) | Data Dictionary page + FSD "Key columns" tables | Re-run the code-sweep (see below), then reconcile the dossier tables |
| **Rules / formulas / gates** (BRD·FSD text) | `frontend/public/docs/manual/<module>.md` | Hand-edit the dossier — the drift hook reminds you which one |

## 1 · Re-sweep column formulas into the Data Dictionary

Ask Claude: **"refresh the ARS data dictionary from code"**. It re-reads the
rule engines + listing/grid/MSA services and upserts `ARS_DATA_DICTIONARY`
(rows it maintains carry `updated_by = 'code-sweep'`). Then update the
"Key columns" tables in the affected dossier(s) to match.

## 2 · The drift hook

`.claude/settings.json` runs `tools/manual/doc_drift_hook.mjs` after every
Edit/Write. When a backend ARS source file changes it prints a reminder naming
the module + its dossier. It only reminds — it never edits. Path→module map
lives in the hook script; extend it when new source files are added.

## Rule of thumb
- Changed **logic** in `msa_service.py`, `grid_calculations.py`,
  `rule_engine_*.py`, `listing.py`, `pend_alc_service.py`, `hold_dashboard.py`,
  `merge_rules.py`? → update that module's dossier FSD + `ARS_DATA_DICTIONARY`.
- Learned a **new rule/gotcha**? → append a dated bullet under the dossier's
  `## Recorded rules`.
