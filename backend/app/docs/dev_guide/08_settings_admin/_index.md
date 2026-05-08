# Settings, Admin & RBAC — Developer Reference

The Settings menu is admin-only. Five sub-pages: **App Settings, Table
Management, Users, Roles, Row-Level Security, Audit Log, TempDB
Maintenance**. Plus the underlying authentication/RBAC layer that
gates every other page.

---

## A. Authentication

### How a user logs in

```
POST /api/v1/auth/login  { username, password }
            │
            ▼
   rbac_users  ── verify hash ──► sign JWT
            │
            ▼
   { access_token, refresh_token, user, permissions }
            │
            ▼
   Browser stores tokens (zustand store + localStorage)
            │
            ▼
   Every subsequent request:
       Authorization: Bearer <token>
            │
            ▼
   Backend `get_current_user`  ── decode JWT ── load User row
```

### Files

| File | Role |
|---|---|
| `app/api/v1/endpoints/auth.py` | `/login`, `/logout`, `/refresh`, `/me`, `get_current_user` dep |
| `app/security/dependencies.py` | `RequirePermissions([...])` dependency |
| `app/security/jwt_helper.py` | Encode/decode JWT |
| `app/security/password_helper.py` | Bcrypt hash/verify |
| `frontend/src/store/authStore.js` | Token + user state (zustand) |
| `frontend/src/services/api.js` | Axios interceptor injects the token |

### Example: programmatic login

```bash
TOKEN=$(curl -s -X POST "$API/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"superadmin","password":"Admin@12345"}' \
  | jq -r .data.access_token)

curl -H "Authorization: Bearer $TOKEN" "$API/api/v1/auth/me"
```

### JWT settings

`app/core/config.py`:

| Setting | Default | What it controls |
|---|---|---|
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 480 (8 hours) | How long until forced re-login |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | 7 | Refresh window |
| `JWT_SECRET_KEY` | `your-super-secret...` | **Must change in prod** |

### Security checklist

- ✅ Passwords hashed with bcrypt (cost factor 12) — never plaintext.
- ✅ `MAX_LOGIN_ATTEMPTS=5` then account locks for 30 min.
- ✅ JWT signed with HS256.
- ⚠️ Default `JWT_SECRET_KEY` ships in code — production deploys MUST override via env.

---

## B. RBAC — Role-Based Access Control

### Tables (System DB)

```
rbac_users            ── one row per user (username, password_hash, is_active)
rbac_roles            ── one row per role (e.g. "Planner", "Allocation Lead")
rbac_user_roles       ── M:N join: which roles each user has
rbac_permissions      ── master list: ALLOC_READ, MSA_VIEW, etc.
rbac_role_permissions ── M:N join: which perms each role has
```

### How a permission check happens

Backend route declares:
```python
@router.get("/grids", dependencies=[Depends(RequirePermissions(["GRID_VIEW"]))])
def list_grids(...): ...
```

`RequirePermissions` does:
1. Get user from JWT (via `get_current_user`).
2. Look up that user's roles.
3. Gather permissions from those roles.
4. If any required perm is missing → 403.

### Frontend permission check

```jsx
import useAuthStore from '@/store/authStore'
const { hasPermission } = useAuthStore()
if (!hasPermission('GRID_VIEW')) return <AccessDenied/>
```

Sidebar items use the same hook — see `Sidebar.jsx`.

### Common change: add a new permission

1. Pick a name (`UPPER_SNAKE_CASE`, e.g. `GRID_PUBLISH`).
2. Add to the permission seed list in `app/core/permissions.py` (or wherever your seed file lives).
3. Insert into `rbac_permissions` table — done automatically on app startup if you wired the seed code.
4. Grant to roles via the Roles UI.
5. Use it server-side: `Depends(RequirePermissions(["GRID_PUBLISH"]))`.
6. Use it client-side: `hasPermission('GRID_PUBLISH')`.

### Special role: superadmin

`is_superadmin=true` on `rbac_users` bypasses all permission checks.
Use sparingly. Frontend check: `useAuthStore().isSuperAdmin()`.

---

## C. Row-Level Security (RLS) — `frontend/src/pages/RLSPage.jsx`

Sometimes you want users to see only "their" data — a planner for MENS
shouldn't see WOMENS allocations. RLS does row-level filtering.

### Tables

```
rls_categories        ── master list of categories (e.g. MENS, WOMENS, KIDS)
rls_user_categories   ── which categories each user can see
```

### How it's applied

Endpoints that respect RLS pull `current_user.rls_categories` and add a
WHERE clause: `WHERE category IN (:rls_categories)`. See for example
`msa_service` accepting an `rls_categories` constructor arg.

### Common change: enforce RLS on a new endpoint

1. Pull RLS categories from the request:
   ```python
   from app.security.rls import get_user_rls_categories
   cats = get_user_rls_categories(db, current_user)
   ```
2. Inject into the WHERE clause:
   ```sql
   WHERE major_category IN (:cats)
   ```
3. Document on the endpoint (docstring) which RLS categories matter.

⚠️ Forgetting RLS on a new endpoint is **the** most common security
mistake. When code-reviewing a new route, always check: does this
return data that's category-scoped? If yes, where's the RLS filter?

---

## D. Audit Log — `frontend/src/pages/AuditPage.jsx`

Every write produces an `audit_log` row. Audit Log page browses them.

### Schema

```
audit_log:
  id, table_name, action_type, record_primary_key,
  old_data (JSON), new_data (JSON), changed_columns (JSON),
  changed_by, batch_id, source, ip_address,
  created_at
```

### Performance

`audit_log` grows fast — millions of rows in a few weeks of heavy
upload activity. Index on `(table_name, batch_id, created_at)` is
non-negotiable.

### Archive strategy

Add a job that moves rows older than 6 months to `audit_log_history`.

---

## E. App Settings — `frontend/src/pages/SettingsPage.jsx`

Edit DB connection without restarting the app. Hot-reload swaps the
SQLAlchemy engine pool in place.

### Hot-reload details

`app/database/session.py` → `reload_db_engines()`:

1. Re-read `app_settings.json` (UI writes to it).
2. Build a **temporary** engine with new credentials.
3. **Probe** it (`SELECT DB_NAME()`).
4. If probe succeeds, swap `system_engine.pool` and `data_engine.pool`
   in place — every `from app.database.session import data_engine`
   keeps working.
5. Dispose the old pools.
6. Probe again to be sure.

### Why probing first matters

Without the probe, a typo in the new password would silently swap, then
every endpoint would 500 with no clue. The probe catches it before the
swap.

---

## F. Table Management — `frontend/src/pages/TableManagementPage.jsx`

Schema-level operations: ALTER COLUMN, drop column, rename, add index.

### What it can do (today)

- View column metadata for any table.
- Add a column.
- Change a column's type (with `TRY_CONVERT` safety).
- Drop a column.

### What it can't do (yet, watch out)

- No transaction wrapping → an error mid-operation can leave the table
  half-altered.
- No automatic backup before destructive changes.

If you extend it: wrap each operation in `BEGIN TRY / BEGIN CATCH +
ROLLBACK TRAN` and ALWAYS take a column-data backup before destructive
changes.

---

## G. TempDB Maintenance — `frontend/src/pages/TempDBAdminPage.jsx` (superadmin only)

`tempdb` fills up when ARS upserts and grids leave global temp tables.
This page surfaces the cleanup service.

### Files

| File | Role |
|---|---|
| `app/services/tempdb_cleanup_service.py` | The daemon |
| `app/api/v1/endpoints/maintenance.py` | `/tempdb/run-now`, `/tempdb/aggressive-shrink-now`, `/tempdb/top-sessions` |

### Settings (config.py)

```python
DB_TEMPDB_CLEANUP_INTERVAL_MINUTES   # how often the daemon runs (default 5)
DB_TEMPDB_ORPHAN_AGE_MINUTES         # drop ##temp tables older than this
DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB    # auto-aggressive when total > this
DB_TEMPDB_ALERT_THRESHOLD_MB         # alert in UI when total > this
DB_TEMPDB_AGGRESSIVE_TARGET_MB       # shrink each file down to this size
```

### What "aggressive" means

`DBCC FREEPROCCACHE` + `DBCC FREESYSTEMCACHE` + `DBCC SHRINKFILE` to a
target size. Heavy-handed; **do not run during peak**.

### Auto-cleanup after heavy jobs

The middleware in `app/middleware/auto_free_space.py` (controlled by
`AUTO_FREE_AFTER_JOB`) runs a lightweight cleanup after any successful
POST/PUT to a configured path (`AUTO_FREE_PATHS` in config). Cooldown
of 60s prevents thrashing.

### Common change: trigger cleanup after a new heavy endpoint

`config.py` → `AUTO_FREE_PATHS` — add the path substring (e.g.
`/my-new-heavy-endpoint`). No code change needed; the middleware
matches by substring.

---

## Conventions for any new admin endpoint

1. **Permission:** declare a new permission for it; never reuse a
   broad one like `ADMIN_SETTINGS`.
2. **Logging:** log who did what and when (separate from `audit_log`
   if it's a system-level operation that doesn't fit the schema).
3. **Confirmation step:** destructive ops (`DROP`, `TRUNCATE`, `KILL`)
   should require an explicit "type the name to confirm" step in the
   UI.
4. **Idempotency:** running the same admin op twice should produce
   the same end state. Guard with `IF EXISTS` etc.
5. **Audit who triggered it:** `current_user.username` into a log
   row.
