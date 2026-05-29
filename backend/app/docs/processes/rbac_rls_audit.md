---
title: Users, Permissions & Audit (RBAC / RLS / Audit)
category: Admin
order: 80
source: backend/app/api/v1/endpoints/{auth,users,roles,rls,audit}.py
last_reviewed: 2026-04-20
---

# Users, Permissions, and Audit

> **═══ USER GUIDE ═══**

## Current access state (live)

<!-- @metric db="system" sql="SELECT COUNT(*) FROM rbac_users" label="Total users" -->

<!-- @metric db="system" sql="SELECT COUNT(*) FROM rbac_roles" label="Defined roles" -->

<!-- @metric db="system" sql="SELECT COUNT(*) FROM rbac_permissions" label="Permissions in catalogue" -->

<!-- @metric db="system" sql="SELECT COUNT(*) FROM audit_log WHERE created_at >= DATEADD(day, -1, GETDATE())" label="Audit events in last 24 h" -->

## In plain English

Four protections layer on top of everything you do in ARS:

1. **Authentication** — *"Are you really who you say you are?"* Password + JWT token.
2. **RBAC** (Role-Based Access Control) — *"Are you allowed to click this button?"* Each role has a list of permissions; each endpoint is gated on one.
3. **RLS** (Row- / Column-Level Security) — *"Can you see these rows / these columns?"* You might have `DATA_VIEW` but only for stores in your region.
4. **Audit** — *"What did you do, and when?"* Every sensitive action is logged.

If you're a **user**, you mostly interact with these when you run into "Access denied" — this doc explains why and how to get access.

If you're a **Super Admin**, this doc tells you how to grant/revoke access and read the audit trail.

## The mental model

```
User clicks button on UI
        ↓
Frontend sends request with JWT token
        ↓
Backend: "Is this JWT valid?"                ← Authentication (auth.py)
        ↓ yes
Backend: "Does this user's role have permission X?"   ← RBAC
        ↓ yes
Backend: fetches data
        ↓
Backend: "Strip rows the user isn't allowed to see" ← RLS row filter
Backend: "Mask columns the user isn't allowed"       ← RLS column filter
        ↓
Response to frontend
        ↓
(if action was mutating) write entry to audit_log   ← Audit
```

## When to use this

You come to this doc when:

- Someone says "I can't see Store HN14" — probably RLS.
- Someone says "Access denied" — probably RBAC.
- You need to add a new user.
- You need to know who ran which allocation when.
- You're investigating a suspicious change.

## User management — step by step

### 1. Go to Users
Sidebar → **Settings** → **Users**.

### 2. Add a new user
Click **Create User**. Fill in:
- Username (unique)
- Email
- Temporary password (they'll change on first login)
- Assigned role(s) — see below

### 3. Assign stores (if RLS is enforced for this role)
Edit the user → Store Access tab → pick stores or regions.

### 4. Save
User gets an email (if SMTP configured) or you share the password manually.

## Roles and permissions

### Default roles
| Role | Typical user | Sample permissions |
|---|---|---|
| `SUPER_ADMIN` | Santosh / dev team | All |
| `ADMIN` | IT support | User / role management, data upload |
| `ALLOCATOR` | Planning team | Listing, Alloc, BDC, preview |
| `ANALYST` | Business analyst | Read-only data, trends |
| `VIEWER` | Executive / QA | Read-only dashboard |

### How to manage
Sidebar → **Settings** → **Roles**.

From there you can:
- Create a new role.
- Assign / un-assign permissions to a role.
- See which users have that role.

Permissions are plain strings like `DATA_VIEW`, `CHECKLIST_VIEW`, `ADMIN_USERS_READ`. Each endpoint is gated on one of them.

## Row-Level Security (RLS)

RLS restricts **which rows** a user can see. Typical uses:
- City manager sees only their city's stores.
- Merchandiser sees only their brand.
- Store manager sees only their own store.

### Tables involved

| Table | Purpose |
|---|---|
| `rls_stores` | Master list of stores. |
| `rls_user_store_access` | Which stores each user can access. |
| `rls_user_region_access` | Same idea at region grain. |
| `rls_column_restrictions` | Columns hidden for specific roles on specific tables. |
| `rls_table_access` | Whole-table allow / deny per role. |

### How to grant store access
Sidebar → **Settings** → **Row-Level Security** → pick user → Add Store → save.

Changes take effect **immediately** — the user doesn't need to log out / in.

### How to hide a column for a role
Sidebar → **RLS** → Column Restrictions → pick table + role + columns.

Restricted columns come back as `***` in API responses.

## Audit log

### What's logged
Every significant action:
- Logins / login failures
- Uploads
- Table modifications
- Allocation runs
- User / role changes
- Data exports

### How to read it
Sidebar → **Settings** → **Audit Log**.

Filters: user, date range, action type, resource.

Sample rows:
```
2026-04-20 14:32  santosh    LOGIN            -
2026-04-20 14:35  santosh    UPLOAD_DATA      table:ET_STORE_STOCK (50,000 rows)
2026-04-20 15:02  santosh    ALLOCATION_RUN   listing_generate
2026-04-20 15:45  rakesh     BDC_EXPORT       filter:RDC=DW01
```

Each row also has a JSON `details` column with parameters, before/after values, etc.

## A worked example — "I can't see my store"

User Priya complains "I can't see HN14 in the Listing preview".

1. Super Admin → Users → find Priya → Roles shows `ALLOCATOR`.
2. Check Permissions on `ALLOCATOR` role — does it have `DATA_VIEW`? Yes.
3. Check Priya's RLS → `rls_user_store_access` for Priya — HN14 missing.
4. Admin adds HN14 → save.
5. Priya reloads Listing → HN14 now visible.

If step 3 showed HN14 present but she still can't see it:
- Check `rls_table_access` — is `ARS_LISTING_WORKING` denied to role `ALLOCATOR`? Fix.
- Check `rls_column_restrictions` — are critical columns masked? Unmask or remove.

## Common questions (FAQ)

**Q: A user's login keeps getting 401 after some time.**
JWT access tokens are short-lived (15–30 min). The refresh flow is automatic — if refresh fails (refresh token expired), they must re-login. No way around it.

**Q: I changed a user's role; they still see old permissions.**
Their JWT has the old permissions baked in. They need to log out and log back in, or the access token must expire naturally.

**Q: A user sees "Access denied" on one page but can use other pages.**
That page's permission string is missing from the user's role. Look at `<ProtectedRoute permission=…>` in the frontend to find the exact string, then add it to the role.

**Q: Is there a read-only audit log viewer?**
Yes — `ADMIN_AUDIT_READ` permission. Grant to auditors without giving them write access.

**Q: How long is audit retained?**
Indefinitely by default. Add a monthly cleanup job if the table gets huge.

**Q: What's the difference between row-level and column-level restriction?**
- **Row-level**: filter WHERE clause — user never sees those rows.
- **Column-level**: returned rows exist but restricted columns are `***`-masked.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| 401 on every request | Token expired; refresh also expired | Log out, log back in. |
| 403 Forbidden | Missing permission | Super Admin grants the permission to your role. |
| "Empty data" where you expected rows | RLS row filter stripped them all | Super Admin adds store/region access. |
| Columns show `***` | Column restriction masking | Super Admin removes the restriction or elevates the role. |
| Suspicious data change | Someone modified without you knowing | Sidebar → **Audit Log** → filter by resource + date range. |

## Verification

```sql
-- Who has SUPER_ADMIN?
SELECT u.username FROM rbac_users u
JOIN rbac_user_roles ur ON u.id=ur.user_id
JOIN rbac_roles r        ON ur.role_id=r.id
WHERE r.name='SUPER_ADMIN';

-- Permissions for a role
SELECT p.name FROM rbac_permissions p
JOIN rbac_role_permissions rp ON rp.permission_id=p.id
JOIN rbac_roles r               ON rp.role_id=r.id
WHERE r.name='ALLOCATOR' ORDER BY p.name;

-- Store access for a user
SELECT store_code FROM rls_user_store_access
WHERE user_id=(SELECT id FROM rbac_users WHERE username='priya');

-- Today's audit activity
SELECT TOP 50 created_at, username, action, resource_type, resource_id
FROM audit_log
WHERE created_at >= CAST(GETDATE() AS DATE)
ORDER BY id DESC;
```

## Settings you can change

- Token lifetimes (access / refresh) — backend config.
- Retention on audit log — add a SQL job.
- Self-service password reset — configurable per org.

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Auth: `auth.py` + JWT (access + refresh).
- RBAC: `Depends(require_permission("X"))` on every endpoint.
- RLS: applied in data-returning endpoints — row filter intersects with user's store/region access; column filter drops / masks restricted columns.
- Audit: middleware + inline writes.
- Models: `backend/app/models/rbac.py`, `backend/app/models/rls.py`.

## How to update this doc

Update when a new permission category is added, a new RLS dimension is introduced, or audit emits new event types. Bump `last_reviewed`.
