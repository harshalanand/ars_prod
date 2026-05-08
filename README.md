# Retail Listing & Allocation System

Enterprise-grade backend for multi-store retail listing and allocation management.

## Tech Stack

- **Backend**: Python 3.11+ / FastAPI / SQLAlchemy
- **Database**: SQL Server (ODBC Driver 18)
- **Auth**: JWT (access + refresh tokens)
- **Security**: RBAC + Row-Level Security + Column-Level Security
- **Audit**: Full change tracking with JSON diff logging

## Project Structure

```
backend/
├── main.py                          # FastAPI entry point
├── requirements.txt
├── .env                             # Configuration (DO NOT commit)
├── scripts/
│   └── 001_create_schema.sql        # Database schema
├── app/
│   ├── api/v1/
│   │   ├── router.py                # Route aggregator
│   │   └── endpoints/
│   │       ├── auth.py              # Login, refresh, password
│   │       ├── users.py             # User CRUD
│   │       ├── roles.py             # Roles & permissions
│   │       ├── rls.py               # RLS management
│   │       └── audit.py             # Audit log viewer
│   ├── core/
│   │   └── config.py                # Pydantic settings
│   ├── database/
│   │   └── session.py               # Engine, session, pooling
│   ├── models/
│   │   ├── rbac.py                  # User, Role, Permission
│   │   ├── rls.py                   # Store, Region, Column access
│   │   ├── audit.py                 # Audit log
│   │   ├── retail.py                # Products, Allocation, Stock
│   │   └── table_mgmt.py           # Dynamic table registry
│   ├── schemas/
│   │   ├── auth.py                  # Pydantic request/response
│   │   └── common.py               # Shared schemas
│   ├── services/
│   │   └── auth_service.py          # Auth + user business logic
│   ├── security/
│   │   ├── jwt_handler.py           # JWT create/verify
│   │   ├── password.py              # Bcrypt hashing
│   │   └── dependencies.py          # FastAPI deps (RBAC, RLS)
│   ├── audit/
│   │   └── service.py               # Audit logging service
│   └── middleware/
│       └── exception_handler.py     # Global error handling
```

## Setup

### 1. Prerequisites

- Python 3.11+
- SQL Server with ODBC Driver 18
- pip

### 2. Database Setup

Run the schema script on your SQL Server:

```sql
-- Open SQL Server Management Studio
-- Connect to HOPC560
-- Execute: scripts/001_create_schema.sql
```

### 3. Backend Setup

```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Configure .env (edit as needed)
cp .env .env.local

# Run the server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Access

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health**: http://localhost:8000/health

### 5. Default Login

- Username: `superadmin`
- Password: `Admin@12345`

## API Endpoints (Phase 1)

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | /api/v1/auth/login | Login | No |
| POST | /api/v1/auth/refresh | Refresh token | No |
| POST | /api/v1/auth/change-password | Change password | Yes |
| GET | /api/v1/auth/me | Current user info | Yes |
| POST | /api/v1/users/ | Create user | ADMIN |
| GET | /api/v1/users/ | List users | ADMIN |
| GET | /api/v1/users/{id} | Get user | ADMIN |
| PUT | /api/v1/users/{id} | Update user | ADMIN |
| POST | /api/v1/users/{id}/unlock | Unlock account | ADMIN |
| GET | /api/v1/roles/ | List roles | Yes |
| POST | /api/v1/roles/ | Create role | ADMIN |
| PUT | /api/v1/roles/{id} | Update role | ADMIN |
| GET | /api/v1/roles/permissions | List permissions | Yes |
| POST | /api/v1/roles/{id}/permissions | Assign permissions | ADMIN |
| POST | /api/v1/rls/store-access | Grant store access | ADMIN |
| GET | /api/v1/rls/store-access/{uid} | Get user stores | Yes |
| POST | /api/v1/rls/region-access | Grant region access | ADMIN |
| GET | /api/v1/rls/region-access/{uid} | Get user regions | Yes |
| POST | /api/v1/rls/column-restrictions | Set column rules | ADMIN |
| GET | /api/v1/rls/stores | List stores | Yes |
| GET | /api/v1/audit/ | Query audit logs | ADMIN |
| GET | /api/v1/audit/{id} | Audit log detail | ADMIN |

## Security Architecture

### RBAC (Role-Based Access Control)
- 5 default roles: Super Admin, Admin, Planner, Analyst, Viewer
- 25 granular permissions across modules
- Permission checks via FastAPI dependencies

### RLS (Row-Level Security)
- Store-level access (direct store assignment)
- Region-level access (covers all stores in region/hub/division)
- Super Admin and Admin bypass RLS

### Column-Level Security
- Hide or mask sensitive columns per role
- Applied at API response level

### Audit
- Every INSERT, UPDATE, DELETE tracked
- JSON diff of old/new values
- Batch tracking for bulk operations
- IP address and user agent logging

## Phases

- [x] **Phase 1**: Database schema + FastAPI scaffold + Auth + RBAC + RLS + Audit
- [x] **Phase 2**: Upsert engine + Table management + Bulk upload
- [x] **Phase 3**: Allocation engine service layer
- [ ] **Phase 4**: React frontend + AG Grid + Admin panel

---

## Phase 2 API Endpoints: Table Management, Upsert, Upload

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | /api/v1/tables/ | Create new table | TABLE_CREATE |
| PUT | /api/v1/tables/{name}/alter | Alter table (add/drop/rename cols) | TABLE_ALTER |
| DELETE | /api/v1/tables/{name} | Soft-delete table | TABLE_DELETE |
| GET | /api/v1/tables/{name}/schema | Get table schema + metadata | Yes |
| GET | /api/v1/tables/ | List registered tables | Yes |
| GET | /api/v1/tables/database/all | List all SQL Server tables | TABLE_READ |
| GET | /api/v1/tables/{name}/data | Paginated data query | Yes |
| DELETE | /api/v1/tables/{name}/data | Truncate table data | TABLE_DELETE |
| POST | /api/v1/data/upsert | JSON upsert (small batches) | DATA_EDIT |
| PUT | /api/v1/data/update | Direct cell update | DATA_EDIT |
| POST | /api/v1/data/delete | Bulk delete by PK | DATA_EDIT |
| POST | /api/v1/upload/ | CSV/Excel upload → upsert | DATA_UPLOAD |
| POST | /api/v1/upload/preview | Preview file columns/data | Yes |
| POST | /api/v1/upload/sheets | Get Excel sheet names | Yes |

### Upsert Engine
- SQL Server MERGE-based upsert (temp table → MERGE → OUTPUT)
- Differential update detection (only updates changed columns)
- Chunked processing (10K rows per chunk, configurable)
- Supports 1M+ rows via file upload
- Full audit logging with batch_id

### File Upload
- CSV and Excel (.xlsx, .xls) support
- Column mapping (rename file cols to table cols)
- Data preview before processing
- Auto-encoding detection (UTF-8, Latin-1, CP1252)
- File saved for audit trail

---

## Phase 3 API Endpoints: Allocation Engine

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | /api/v1/allocations/run | Run new allocation | ALLOC_CREATE |
| GET | /api/v1/allocations/ | List allocations | ALLOC_READ |
| GET | /api/v1/allocations/{id}/details | Allocation details (paginated) | ALLOC_READ |
| GET | /api/v1/allocations/{id}/summary | Summary with breakdowns | ALLOC_READ |
| POST | /api/v1/allocations/{id}/overrides | Apply manual overrides | ALLOC_UPDATE |
| POST | /api/v1/allocations/{id}/approve | Approve allocation | ALLOC_APPROVE |
| POST | /api/v1/allocations/{id}/execute | Execute (lock) allocation | ALLOC_EXECUTE |
| POST | /api/v1/allocations/{id}/cancel | Cancel allocation | ALLOC_UPDATE |
| GET | /api/v1/allocations/{id}/grid/{store} | Size × Color grid view | ALLOC_READ |

### Allocation Strategies
- **RATIO**: Distribute warehouse stock by store grade ratios (A=1.0, B=0.7, C=0.4)
- **SALES**: Proportional to store's historical sales (configurable lookback)
- **STOCK**: Fill stores with low stock relative to grade target

### Allocation Flow
```
DRAFT → (run) → IN_PROGRESS → (complete) → DRAFT
    ↓                                          ↓
  (override)                                (approve)
                                               ↓
                                           APPROVED
                                               ↓
                                           EXECUTED ← (locked, cannot modify)
```

### Features
- Store grade-based ratios (A/B/C/D)
- Size × Color grid distribution
- Warehouse availability capping
- Per-store min/max constraints
- Total qty limit enforcement
- Manual override with audit trail
- RLS enforcement on allocation details

---

## Phase 4 – Frontend (React + Vite + Tailwind + AG Grid)

### Tech Stack
| Layer | Technology |
|-------|-----------|
| Framework | React 18 + Vite 5 |
| Styling | Tailwind CSS 3.4 |
| Data Grid | AG Grid Enterprise 32 |
| State | Zustand 4.5 |
| Charts | Recharts 2.12 |
| HTTP | Axios with JWT interceptors |
| Icons | Lucide React |
| Notifications | React Hot Toast |
| Routing | React Router DOM 6 |

### Frontend Structure
```
frontend/
├── src/
│   ├── components/
│   │   ├── layout/          # Layout, Sidebar, Header
│   │   └── tables/          # CreateTableModal
│   ├── pages/
│   │   ├── LoginPage.jsx        # JWT auth with gradient UI
│   │   ├── DashboardPage.jsx    # Stats cards + charts + recent allocs
│   │   ├── TablesPage.jsx       # Table cards, search, create modal
│   │   ├── TableDataPage.jsx    # AG Grid with inline editing + pagination
│   │   ├── UploadPage.jsx       # Drag-drop, preview, progress bar, results
│   │   ├── AllocationsPage.jsx  # Allocation list with status filters
│   │   ├── NewAllocationPage.jsx # Allocation config form
│   │   ├── AllocationDetailPage.jsx # Summary, charts, AG Grid details
│   │   ├── UsersPage.jsx        # User CRUD with role assignment
│   │   ├── RolesPage.jsx        # Role list + permission checkboxes
│   │   ├── AuditPage.jsx        # Audit log with filters + AG Grid
│   │   └── RLSPage.jsx          # Store/region access per user
│   ├── services/api.js      # Axios + JWT refresh + all API methods
│   ├── store/authStore.js   # Zustand auth store
│   ├── styles/globals.css   # Tailwind + AG Grid theme
│   ├── App.jsx              # Routes with permission guards
│   └── main.jsx             # Entry point
├── vite.config.js           # Proxy /api → backend:8000
├── tailwind.config.js
└── package.json
```

### Pages (21 files, ~3,500 lines)
| Page | Features |
|------|----------|
| **Login** | Gradient dark theme, JWT auth, auto-redirect |
| **Dashboard** | 3 stat cards, bar chart, recent allocations |
| **Tables** | Card grid, search, create modal, soft-delete |
| **Table Data** | AG Grid inline editing, floating filters, pagination, CSV export |
| **Upload** | 4-step wizard, drag-drop, preview, progress bar, result summary |
| **Allocations** | Status-filtered list, pagination, badges |
| **New Allocation** | Multi-section form: type/basis/warehouse, grade ratios, constraints |
| **Allocation Detail** | Stats, pie/bar charts, AG Grid, approve/execute/cancel |
| **Users** | CRUD with role toggle buttons, unlock |
| **Roles** | Split panel: role list + grouped permission checkboxes |
| **Audit** | 6-filter bar, AG Grid with color-coded operations |
| **RLS** | User list + store/region tag management |

### Quick Start (Frontend)
```bash
cd frontend
npm install
npm run dev          # :3000, proxies /api → :8000
npm run build        # Production → dist/
```
