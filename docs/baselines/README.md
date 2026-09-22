# Step 0 regression baselines

Created by `backend/scripts/step0_rdc_baseline.py --baseline <sid> --label own|cross`.

Each folder holds CSV exports of the 4 tables V5 diffs, plus a `manifest.json`
with a SHA-256 per file so a later comparison is provable.

**These must be captured BEFORE any code change.** Once the code changes the
"before" state is gone and Own can never be proven unchanged.

Folders: `own_<session_id>/`, `cross_<session_id>/`
