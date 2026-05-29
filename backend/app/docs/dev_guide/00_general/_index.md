# ARS Developer Overview

Welcome. This page is the entry point for any new engineer joining the ARS
codebase. It is auto-paired with live introspection — the Routes / Services
/ Pages / Tables tabs reflect the **running** code, not whatever was true
when this note was last edited.

## What ARS is

V2 Retail's Auto Replenishment System. Replaces 20-machine Excel allocation
process. Serves 320+ stores × 242 major categories. Inputs come from SAP via
the RFC pipeline; outputs feed the warehouse picking lists.

## Stack at a glance

| Layer | Tech |
|---|---|
| Frontend | React 18 + Vite + Tailwind, served at `localhost:3001` (dev) |
| Backend | FastAPI + SQLAlchemy 2.x + pyodbc, runs at `:8000` |
| Databases | Two SQL Servers — `Claude` (system: RBAC, audit, jobs) and `Rep_Data` (business data) |
| Workers (PaaS) | 34 Cloudflare Workers — RFC pipeline, log forwarders, SQL analyst, MCP, etc. |

## How to find the code for any feature you see in the UI

1. Open **Developer Guide → Pages**. Find the page by name.
2. The `route` column tells you the URL; the `file` column tells you the
   `.jsx` source.
3. Open **Developer Guide → Routes** and filter by the API path the page
   calls (look at the page's source for `api.get(...)` / `api.post(...)`).
4. From the route, follow the `file:line` link to the endpoint function.
5. The endpoint usually delegates to a service in `app/services/*.py` —
   that's where the business logic lives.

## Where to start changing things

| You want to... | Open... |
|---|---|
| Add a new screen | `frontend/src/pages/` + register in `App.jsx` + sidebar |
| Add a new API | `backend/app/api/v1/endpoints/` + register in `router.py` |
| Add background job | `app/services/upload_job_service.py` pattern |
| Change DB schema | A model in `app/models/` + relevant migration; reconcile_columns() picks up additions on startup |
| Add a permission | `app/core/permissions.py` + assign to roles in `rbac_permissions` |
| Tune Azure SQL | `app/core/config.py` — DB pool, grid throttles, tempdb, auto-cleanup |

## Day-zero checklist for a new dev

1. `git clone` and `pip install -r requirements.txt`
2. Copy `backend/.env.example` → `backend/.env`, fill in DB creds
3. Backend: `uvicorn main:app --reload`
4. Frontend: `npm install && npm run dev`
5. Login as `superadmin` / `Admin@12345` (dev only)
6. Open the **Developer Guide** menu — click through Routes, Services,
   Tables. Now you have a map.

## Read these next

- `01_how_to_add_a_route.md` — the canonical recipe
- `02_how_to_change_an_upsert.md` — Upload Data flow end-to-end
- `03_how_to_add_a_grid.md` — Grid Builder
- `CLAUDE.md` (repo root) — credentials, deploy commands, MCP setup
