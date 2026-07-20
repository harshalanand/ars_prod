# Refreshing the ARS Training Manual

The manual is the single source of truth for ARS — read by humans in-app
(`/manual/*`) and by Claude as "ARS memory" before changing code. Keep it
current when the pipeline changes.

## What can go stale, and how to refresh each

| Artifact | Where | Refresh command / action |
|---|---|---|
| **Screenshots** (annotated step shots) | `frontend/public/docs/manual`-linked `public/docs/guide/<module>/` | `node tools/manual/capture_steps.js` (app on :3000 + backend on :8000 must be running) |
| **Column formulas** (`ARS_DATA_DICTIONARY`) | Data Dictionary page + FSD "Key columns" tables | Re-run the code-sweep (see below), then reconcile the dossier tables |
| **Rules / formulas / gates** (BRD·FSD text) | `frontend/public/docs/manual/<module>.md` | Hand-edit the dossier — the drift hook reminds you which one |
| **Step captions** | `frontend/src/pages/guide/guideSteps.js` | Hand-edit; no rebuild needed |

## 1 · Regenerate screenshots

```bash
# from repo root, with the frontend dev server + backend API running
node tools/manual/capture_steps.js
```
Writes ~51 annotated PNGs to `frontend/public/docs/guide/<module>/`.
Login is injected via the backend `/auth/login`; edit the manifest at the top
of the script to add/rename steps. Steps that would mutate data (Generate,
Approve, DO entry) only highlight the button — the script never triggers them.

## 2 · Re-sweep column formulas into the Data Dictionary

Ask Claude: **"refresh the ARS data dictionary from code"**. It re-reads the
rule engines + listing/grid/MSA services and upserts `ARS_DATA_DICTIONARY`
(rows it maintains carry `updated_by = 'code-sweep'`). Then update the
"Key columns" tables in the affected dossier(s) to match.

## 3 · The drift hook

`.claude/settings.json` runs `tools/manual/doc_drift_hook.mjs` after every
Edit/Write. When a backend ARS source file changes it prints a reminder naming
the module + its dossier. It only reminds — it never edits. Path→module map
lives in the hook script; extend it when new source files are added.

## Rule of thumb
- Changed **logic** in `msa_service.py`, `grid_calculations.py`,
  `rule_engine_*.py`, `listing.py`, `pend_alc_service.py`, `hold_dashboard.py`,
  `merge_rules.py`? → update that module's dossier FSD + `ARS_DATA_DICTIONARY`.
- Changed the **UI** of a screen? → re-run step 1 so screenshots match.
- Learned a **new rule/gotcha**? → append a dated bullet under the dossier's
  `## Recorded rules`.
