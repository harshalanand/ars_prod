---
title: 1SZ ALC module
tags: [ars, module, sz1, allocation]
updated: 2026-10-09
---

# 1SZ ALC module

Native ARS port of the **SZ1 Allocation** Streamlit tool. Built 2026-10-09 in `D:\PROJECTS\ars_v3` on `feat/central-rdc-pool`. **Uncommitted.**

- **Canonical spec:** `frontend/public/docs/manual/sz1_alloc.md` (dossier). A "1SZ Allocation — BRD & FSD" artifact also exists in Claude Docs.
- **Sidebar:** "1SZ ALC", right after GRT ALC. Routes `/sz1-alloc/{base,req,run,sessions,help}`.
- **Permissions:** `MOD_1SZ_ALC` plus `PAGE_1SZ_*`.
- **Tables:** `ARS_SZ1_MSA_SZ_BASE`, `ARS_SZ1_ST_MJ_SZ_REQ_BASE`, `ARS_SZ1_ST_ART_REQ`, `ARS_SZ1_SESSION`, `ARS_SZ1_BUILD`. DDL `backend/scripts/039_sz1_module.sql`.
- **Parity (2026-10-09, read-only):** Step 1 7,989 rows; Step 2 303,840 rows; Step 3 identical for 120 stores × 4 warehouse modes × 3 priority modes.
- **Deviations from the tool:** balance run uses `REM_MJ_SZ_SRT` (no wave-2 refill), transactional limit rollback, and other items listed in the dossier.

**Open:** Step 3 run and session delete not yet exercised end-to-end on HOPC866. Business sign-off owner before dispatch is still open.

Related: [[GRT ALC B2B module]], [[Bin Allocation module]], [[ARS Work Log]].
