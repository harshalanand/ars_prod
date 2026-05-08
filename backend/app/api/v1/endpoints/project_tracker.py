"""
Project Tracker API
===================
Enterprise-style project management module: hierarchical projects with
classification (status / priority / phase / category), assignees, due dates,
auto-rollup progress, and a per-change activity audit log.

Tables (Data DB / rep_data):
    PT_PROJECT          — main hierarchy (self-FK PARENT_ID, max depth 3)
    PT_ACTIVITY_LOG     — append-only audit (CREATED, FIELD_CHANGED, DELETED)

Endpoints under /api/v1/pt/
"""
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

router = APIRouter(prefix="/pt", tags=["Project Tracker"])

PROJECT_TABLE  = "PT_PROJECT"
ACTIVITY_TABLE = "PT_ACTIVITY_LOG"
MAX_DEPTH = 3  # PROJECT → SUB_PROJECT → TASK

ALLOWED_STATUS   = {"DRAFT", "NOT_STARTED", "IN_PROGRESS", "BLOCKED",
                    "ON_HOLD", "COMPLETED", "CANCELLED"}
ALLOWED_PRIORITY = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
ALLOWED_PHASE    = {"PHASE_1", "PHASE_2", "PHASE_3", "BACKLOG", "ICEBOX"}
ALLOWED_CATEGORY = {"BUG", "FEATURE", "ENHANCEMENT", "RESEARCH",
                    "MAINTENANCE", "INFRA", "OTHER"}

# Fields whose changes get logged into PT_ACTIVITY_LOG on update
TRACKED_FIELDS = [
    "name", "status", "priority", "phase", "category",
    "owner_username", "due_date", "start_date", "progress_pct",
    "estimated_hours", "actual_hours",
]


# ── Schemas ──────────────────────────────────────────────────────────────────
class ProjectCreate(BaseModel):
    parent_id:        Optional[int] = None
    name:             str = Field(..., min_length=1, max_length=255)
    description:      Optional[str] = None
    status:           str = "NOT_STARTED"
    priority:         str = "MEDIUM"
    phase:            Optional[str] = "BACKLOG"
    category:         Optional[str] = None
    tags:             Optional[str] = None
    owner_username:   Optional[str] = None
    assignees:        Optional[str] = None       # CSV of usernames
    start_date:       Optional[date] = None
    due_date:         Optional[date] = None
    estimated_hours:  Optional[float] = None
    progress_pct:     int = 0


class ProjectUpdate(BaseModel):
    name:             Optional[str] = None
    description:      Optional[str] = None
    status:           Optional[str] = None
    priority:         Optional[str] = None
    phase:            Optional[str] = None
    category:         Optional[str] = None
    tags:             Optional[str] = None
    owner_username:   Optional[str] = None
    assignees:        Optional[str] = None
    start_date:       Optional[date] = None
    due_date:         Optional[date] = None
    estimated_hours:  Optional[float] = None
    actual_hours:     Optional[float] = None
    progress_pct:     Optional[int] = Field(None, ge=0, le=100)
    auto_progress:    Optional[bool] = None


class MoveRequest(BaseModel):
    new_parent_id: Optional[int] = None  # None = make root


# ── Helpers ──────────────────────────────────────────────────────────────────
def _run(conn, sql, params=None):
    if params:
        conn.execute(text(sql), params)
    else:
        conn.execute(text(sql))
    conn.commit()


def _ensure_tables(engine):
    """Idempotent CREATE TABLE — runs on every startup."""
    with engine.connect() as c:
        # PT_PROJECT
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{PROJECT_TABLE}')
            BEGIN
                CREATE TABLE [{PROJECT_TABLE}] (
                    PROJECT_ID         INT IDENTITY(1,1) PRIMARY KEY,
                    PARENT_ID          INT NULL,
                    PROJECT_CODE       NVARCHAR(50) NOT NULL,
                    NAME               NVARCHAR(255) NOT NULL,
                    DESCRIPTION        NVARCHAR(MAX),
                    PROJECT_TYPE       NVARCHAR(20) NOT NULL DEFAULT 'PROJECT',
                    STATUS             NVARCHAR(30) NOT NULL DEFAULT 'NOT_STARTED',
                    PRIORITY           NVARCHAR(20) NOT NULL DEFAULT 'MEDIUM',
                    PHASE              NVARCHAR(20) NULL,
                    CATEGORY           NVARCHAR(30) NULL,
                    TAGS               NVARCHAR(500) NULL,
                    OWNER_USERNAME     NVARCHAR(100) NULL,
                    ASSIGNEES          NVARCHAR(MAX) NULL,
                    PROGRESS_PCT       INT NOT NULL DEFAULT 0,
                    AUTO_PROGRESS      BIT NOT NULL DEFAULT 1,
                    START_DATE         DATE NULL,
                    DUE_DATE           DATE NULL,
                    COMPLETED_DATE     DATETIME NULL,
                    ESTIMATED_HOURS    FLOAT NULL,
                    ACTUAL_HOURS       FLOAT NULL,
                    SORT_ORDER         INT NOT NULL DEFAULT 0,
                    IS_ARCHIVED        BIT NOT NULL DEFAULT 0,
                    CREATED_BY         NVARCHAR(100) NULL,
                    CREATED_AT         DATETIME NOT NULL DEFAULT GETDATE(),
                    UPDATED_BY         NVARCHAR(100) NULL,
                    UPDATED_AT         DATETIME NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT UQ_{PROJECT_TABLE}_CODE UNIQUE (PROJECT_CODE)
                );
                CREATE INDEX IX_{PROJECT_TABLE}_PARENT ON [{PROJECT_TABLE}](PARENT_ID);
                CREATE INDEX IX_{PROJECT_TABLE}_STATUS ON [{PROJECT_TABLE}](STATUS, IS_ARCHIVED);
                CREATE INDEX IX_{PROJECT_TABLE}_PHASE  ON [{PROJECT_TABLE}](PHASE,  IS_ARCHIVED);
                CREATE INDEX IX_{PROJECT_TABLE}_OWNER  ON [{PROJECT_TABLE}](OWNER_USERNAME);
                CREATE INDEX IX_{PROJECT_TABLE}_DUE    ON [{PROJECT_TABLE}](DUE_DATE);
            END
        """)

        # PT_ACTIVITY_LOG (append-only)
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{ACTIVITY_TABLE}')
            BEGIN
                CREATE TABLE [{ACTIVITY_TABLE}] (
                    ACTIVITY_ID    INT IDENTITY(1,1) PRIMARY KEY,
                    PROJECT_ID     INT NOT NULL,
                    ACTIVITY_TYPE  NVARCHAR(50) NOT NULL,
                    FIELD_NAME     NVARCHAR(100) NULL,
                    OLD_VALUE      NVARCHAR(MAX) NULL,
                    NEW_VALUE      NVARCHAR(MAX) NULL,
                    ACTOR          NVARCHAR(100) NOT NULL,
                    CREATED_AT     DATETIME NOT NULL DEFAULT GETDATE(),
                    DETAILS        NVARCHAR(MAX) NULL
                );
                CREATE INDEX IX_{ACTIVITY_TABLE}_PROJECT
                    ON [{ACTIVITY_TABLE}](PROJECT_ID, CREATED_AT DESC);
            END
        """)


def _validate_enum(field, value, allowed):
    if value is not None and value not in allowed:
        raise HTTPException(400, f"{field} must be one of {sorted(allowed)} — got {value!r}")


def _next_project_code(conn) -> str:
    """Generate PT-YYYY-NNNN where NNNN is the next sequence for the year."""
    year = datetime.utcnow().year
    row = conn.execute(text(f"""
        SELECT ISNULL(MAX(TRY_CAST(SUBSTRING(PROJECT_CODE, 9, 10) AS INT)), 0)
        FROM [{PROJECT_TABLE}]
        WHERE PROJECT_CODE LIKE :p
    """), {"p": f"PT-{year}-%"}).scalar()
    return f"PT-{year}-{(int(row or 0) + 1):04d}"


def _depth(conn, project_id: int) -> int:
    """Return depth (0 = root) by walking PARENT_ID chain."""
    d = 0
    cur = project_id
    while cur is not None and d < MAX_DEPTH + 2:
        row = conn.execute(text(f"SELECT PARENT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                           {"i": cur}).fetchone()
        if not row or row[0] is None:
            return d
        cur = row[0]
        d += 1
    return d


def _project_type_for_depth(d: int) -> str:
    return ["PROJECT", "SUB_PROJECT", "TASK"][min(d, 2)]


def _row_to_dict(row) -> dict:
    """Convert a SQLAlchemy Row to a dict with normalised types."""
    d = dict(row._mapping)
    # Normalise dates / datetimes to ISO strings for JSON
    for k, v in list(d.items()):
        if isinstance(v, (date, datetime)):
            d[k] = v.isoformat()
    return d


def _log_activity(conn, project_id: int, activity_type: str, actor: str,
                  field_name: Optional[str] = None,
                  old_value=None, new_value=None, details: Optional[str] = None):
    conn.execute(text(f"""
        INSERT INTO [{ACTIVITY_TABLE}]
            (PROJECT_ID, ACTIVITY_TYPE, FIELD_NAME, OLD_VALUE, NEW_VALUE, ACTOR, DETAILS)
        VALUES (:pid, :at, :fn, :ov, :nv, :ac, :dt)
    """), {
        "pid": project_id, "at": activity_type, "fn": field_name,
        "ov": None if old_value is None else str(old_value),
        "nv": None if new_value is None else str(new_value),
        "ac": actor, "dt": details,
    })


def _recompute_parent_progress(conn, parent_id: int):
    """Roll up AVG(child.PROGRESS_PCT) to parent.PROGRESS_PCT if AUTO_PROGRESS=1."""
    if not parent_id:
        return
    conn.execute(text(f"""
        UPDATE p SET p.PROGRESS_PCT = CAST(c.avg_pct AS INT),
                     p.UPDATED_AT   = GETDATE()
        FROM [{PROJECT_TABLE}] p
        INNER JOIN (
            SELECT PARENT_ID, AVG(CAST(PROGRESS_PCT AS FLOAT)) AS avg_pct
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0 AND PARENT_ID = :pid
            GROUP BY PARENT_ID
        ) c ON c.PARENT_ID = p.PROJECT_ID
        WHERE p.AUTO_PROGRESS = 1 AND p.PROJECT_ID = :pid
    """), {"pid": parent_id})
    # Cascade up
    grand = conn.execute(text(f"SELECT PARENT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                         {"i": parent_id}).scalar()
    if grand:
        _recompute_parent_progress(conn, grand)


# ── Initialise tables on first import ─────────────────────────────────────────
try:
    _ensure_tables(get_data_engine())
    logger.info("[pt] PT_PROJECT / PT_ACTIVITY_LOG tables ready")
except Exception as e:
    logger.warning(f"[pt] Could not auto-create PT tables on import: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CRUD ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/projects", response_model=APIResponse)
def create_project(body: ProjectCreate, current_user: User = Depends(get_current_user)):
    _validate_enum("status",   body.status,   ALLOWED_STATUS)
    _validate_enum("priority", body.priority, ALLOWED_PRIORITY)
    _validate_enum("phase",    body.phase,    ALLOWED_PHASE | {None})
    _validate_enum("category", body.category, ALLOWED_CATEGORY | {None})

    eng = get_data_engine()
    with eng.connect() as c:
        # Validate parent + depth
        depth = 0
        if body.parent_id:
            parent = c.execute(text(f"SELECT PROJECT_ID, IS_ARCHIVED FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                               {"i": body.parent_id}).fetchone()
            if not parent:
                raise HTTPException(404, f"Parent project {body.parent_id} not found")
            if parent[1]:
                raise HTTPException(400, "Cannot add child to an archived project")
            depth = _depth(c, body.parent_id) + 1
            if depth >= MAX_DEPTH:
                raise HTTPException(400, f"Hierarchy depth limit reached (max {MAX_DEPTH} levels: PROJECT → SUB_PROJECT → TASK)")

        code = _next_project_code(c)
        ptype = _project_type_for_depth(depth)
        actor = current_user.username

        result = c.execute(text(f"""
            INSERT INTO [{PROJECT_TABLE}]
                (PARENT_ID, PROJECT_CODE, NAME, DESCRIPTION, PROJECT_TYPE,
                 STATUS, PRIORITY, PHASE, CATEGORY, TAGS,
                 OWNER_USERNAME, ASSIGNEES, PROGRESS_PCT,
                 START_DATE, DUE_DATE, ESTIMATED_HOURS,
                 CREATED_BY, UPDATED_BY)
            OUTPUT INSERTED.PROJECT_ID
            VALUES (:pid, :code, :name, :desc, :ptype,
                    :status, :priority, :phase, :cat, :tags,
                    :owner, :assignees, :pct,
                    :sd, :dd, :est,
                    :actor, :actor)
        """), {
            "pid": body.parent_id, "code": code, "name": body.name,
            "desc": body.description, "ptype": ptype,
            "status": body.status, "priority": body.priority,
            "phase": body.phase, "cat": body.category, "tags": body.tags,
            "owner": body.owner_username or actor,
            "assignees": body.assignees,
            "pct": body.progress_pct,
            "sd": body.start_date, "dd": body.due_date,
            "est": body.estimated_hours,
            "actor": actor,
        })
        new_id = int(result.scalar())

        _log_activity(c, new_id, "CREATED", actor,
                      details=f"Created {ptype} '{body.name}' under parent_id={body.parent_id}")
        c.commit()

        if body.parent_id:
            _recompute_parent_progress(c, body.parent_id)
            c.commit()

    logger.info(f"[pt] {actor} created project {code} (id={new_id})")
    return APIResponse(success=True, message=f"Project {code} created",
                       data={"project_id": new_id, "project_code": code})


def _build_filter_sql(status=None, priority=None, phase=None, category=None,
                     owner=None, assignee=None, q=None, parent_id=None,
                     archived=False, tag=None,
                     due_before=None, due_after=None,
                     overdue=False):
    """Build WHERE clause + params dict for filtered queries."""
    where = ["IS_ARCHIVED = :archived"]
    params = {"archived": 1 if archived else 0}
    if status:
        where.append("STATUS = :status")
        params["status"] = status
    if priority:
        where.append("PRIORITY = :priority")
        params["priority"] = priority
    if phase:
        where.append("PHASE = :phase")
        params["phase"] = phase
    if category:
        where.append("CATEGORY = :category")
        params["category"] = category
    if owner:
        where.append("OWNER_USERNAME = :owner")
        params["owner"] = owner
    if assignee:
        where.append("(ASSIGNEES LIKE :as_like OR OWNER_USERNAME = :as_eq)")
        params["as_like"] = f"%{assignee}%"
        params["as_eq"]   = assignee
    if tag:
        where.append("TAGS LIKE :tag")
        params["tag"] = f"%{tag}%"
    if q:
        where.append("(NAME LIKE :q OR DESCRIPTION LIKE :q OR PROJECT_CODE LIKE :q)")
        params["q"] = f"%{q}%"
    if parent_id is not None:
        if parent_id == 0:
            where.append("PARENT_ID IS NULL")
        else:
            where.append("PARENT_ID = :parent_id")
            params["parent_id"] = parent_id
    if due_before:
        where.append("DUE_DATE <= :due_before")
        params["due_before"] = due_before
    if due_after:
        where.append("DUE_DATE >= :due_after")
        params["due_after"] = due_after
    if overdue:
        where.append("DUE_DATE < CAST(GETDATE() AS DATE) AND STATUS NOT IN ('COMPLETED','CANCELLED')")
    return " AND ".join(where), params


@router.get("/projects", response_model=APIResponse)
def list_projects(
    status:   Optional[str] = None,
    priority: Optional[str] = None,
    phase:    Optional[str] = None,
    category: Optional[str] = None,
    owner:    Optional[str] = None,
    assignee: Optional[str] = None,
    q:        Optional[str] = None,
    parent_id: Optional[int] = None,
    archived: bool = False,
    overdue:  bool = False,
    limit:    int = 500,
    current_user: User = Depends(get_current_user),
):
    where, params = _build_filter_sql(
        status=status, priority=priority, phase=phase, category=category,
        owner=owner, assignee=assignee, q=q, parent_id=parent_id,
        archived=archived, overdue=overdue,
    )
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT TOP ({int(limit)}) p.*,
                   (SELECT COUNT(*) FROM [{PROJECT_TABLE}] ch
                      WHERE ch.PARENT_ID = p.PROJECT_ID AND ch.IS_ARCHIVED = 0) AS CHILDREN_COUNT,
                   CASE WHEN p.DUE_DATE IS NOT NULL
                          AND p.DUE_DATE < CAST(GETDATE() AS DATE)
                          AND p.STATUS NOT IN ('COMPLETED','CANCELLED')
                        THEN 1 ELSE 0 END AS IS_OVERDUE
            FROM [{PROJECT_TABLE}] p
            WHERE {where}
            ORDER BY
                CASE p.PRIORITY WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                                 WHEN 'MEDIUM' THEN 3 WHEN 'LOW' THEN 4 ELSE 5 END,
                p.DUE_DATE ASC,
                p.SORT_ORDER ASC,
                p.PROJECT_ID DESC
        """), params).fetchall()
    return APIResponse(success=True, data=[_row_to_dict(r) for r in rows],
                       message=f"{len(rows)} projects")


@router.get("/projects/tree", response_model=APIResponse)
def project_tree(archived: bool = False, current_user: User = Depends(get_current_user)):
    """Return all projects flat — frontend builds the tree from PARENT_ID."""
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM [{PROJECT_TABLE}] ch
                      WHERE ch.PARENT_ID = p.PROJECT_ID AND ch.IS_ARCHIVED = 0) AS CHILDREN_COUNT,
                   CASE WHEN p.DUE_DATE IS NOT NULL
                          AND p.DUE_DATE < CAST(GETDATE() AS DATE)
                          AND p.STATUS NOT IN ('COMPLETED','CANCELLED')
                        THEN 1 ELSE 0 END AS IS_OVERDUE
            FROM [{PROJECT_TABLE}] p
            WHERE p.IS_ARCHIVED = :a
            ORDER BY p.SORT_ORDER ASC, p.PROJECT_ID ASC
        """), {"a": 1 if archived else 0}).fetchall()
    return APIResponse(success=True, data=[_row_to_dict(r) for r in rows])


@router.get("/projects/{pid}", response_model=APIResponse)
def get_project(pid: int, current_user: User = Depends(get_current_user)):
    eng = get_data_engine()
    with eng.connect() as c:
        row = c.execute(text(f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM [{PROJECT_TABLE}] ch
                      WHERE ch.PARENT_ID = p.PROJECT_ID AND ch.IS_ARCHIVED = 0) AS CHILDREN_COUNT
            FROM [{PROJECT_TABLE}] p
            WHERE p.PROJECT_ID = :i
        """), {"i": pid}).fetchone()
        if not row:
            raise HTTPException(404, f"Project {pid} not found")
        # Ancestors for breadcrumb
        ancestors = []
        cur = row.PARENT_ID
        while cur:
            a = c.execute(text(f"SELECT PROJECT_ID, PROJECT_CODE, NAME, PARENT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                          {"i": cur}).fetchone()
            if not a:
                break
            ancestors.insert(0, _row_to_dict(a))
            cur = a.PARENT_ID
        # Children
        children = c.execute(text(f"""
            SELECT *,
                   CASE WHEN DUE_DATE IS NOT NULL
                          AND DUE_DATE < CAST(GETDATE() AS DATE)
                          AND STATUS NOT IN ('COMPLETED','CANCELLED')
                        THEN 1 ELSE 0 END AS IS_OVERDUE
            FROM [{PROJECT_TABLE}]
            WHERE PARENT_ID = :i AND IS_ARCHIVED = 0
            ORDER BY SORT_ORDER, PROJECT_ID
        """), {"i": pid}).fetchall()
    return APIResponse(success=True, data={
        "project":   _row_to_dict(row),
        "ancestors": ancestors,
        "children":  [_row_to_dict(r) for r in children],
    })


@router.put("/projects/{pid}", response_model=APIResponse)
def update_project(pid: int, body: ProjectUpdate,
                   current_user: User = Depends(get_current_user)):
    if body.status:   _validate_enum("status",   body.status,   ALLOWED_STATUS)
    if body.priority: _validate_enum("priority", body.priority, ALLOWED_PRIORITY)
    if body.phase:    _validate_enum("phase",    body.phase,    ALLOWED_PHASE)
    if body.category: _validate_enum("category", body.category, ALLOWED_CATEGORY)

    eng = get_data_engine()
    with eng.connect() as c:
        existing = c.execute(text(f"SELECT * FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                             {"i": pid}).fetchone()
        if not existing:
            raise HTTPException(404, f"Project {pid} not found")

        actor = current_user.username
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            return APIResponse(success=True, message="No changes", data={"project_id": pid})

        # Auto-stamp COMPLETED_DATE on transition to COMPLETED
        completed_date_set = ""
        if updates.get("status") == "COMPLETED" and existing.STATUS != "COMPLETED":
            completed_date_set = ", COMPLETED_DATE = GETDATE()"
            updates.setdefault("progress_pct", 100)
        elif updates.get("status") and updates["status"] != "COMPLETED" and existing.STATUS == "COMPLETED":
            completed_date_set = ", COMPLETED_DATE = NULL"

        # Build SET clause
        col_map = {
            "name": "NAME", "description": "DESCRIPTION",
            "status": "STATUS", "priority": "PRIORITY", "phase": "PHASE",
            "category": "CATEGORY", "tags": "TAGS",
            "owner_username": "OWNER_USERNAME", "assignees": "ASSIGNEES",
            "start_date": "START_DATE", "due_date": "DUE_DATE",
            "estimated_hours": "ESTIMATED_HOURS", "actual_hours": "ACTUAL_HOURS",
            "progress_pct": "PROGRESS_PCT", "auto_progress": "AUTO_PROGRESS",
        }
        sets = [f"{col_map[k]} = :{k}" for k in updates if k in col_map]
        sets.append("UPDATED_BY = :actor")
        sets.append("UPDATED_AT = GETDATE()")
        sql = f"UPDATE [{PROJECT_TABLE}] SET {', '.join(sets)}{completed_date_set} WHERE PROJECT_ID = :pid"
        params = {**updates, "actor": actor, "pid": pid}
        c.execute(text(sql), params)

        # Activity log: one row per tracked field that actually changed
        for field in TRACKED_FIELDS:
            if field in updates:
                old = getattr(existing, col_map[field], None) if field in col_map else None
                new = updates[field]
                if str(old) != str(new):
                    _log_activity(c, pid, "FIELD_CHANGED", actor,
                                  field_name=field, old_value=old, new_value=new)
        c.commit()

        # Roll up progress to parent if needed
        if "progress_pct" in updates or "status" in updates:
            if existing.PARENT_ID:
                _recompute_parent_progress(c, existing.PARENT_ID)
                c.commit()

    return APIResponse(success=True, message="Updated", data={"project_id": pid})


@router.delete("/projects/{pid}", response_model=APIResponse)
def archive_project(pid: int, current_user: User = Depends(get_current_user)):
    """Soft delete: sets IS_ARCHIVED=1 on the project + all descendants."""
    eng = get_data_engine()
    actor = current_user.username
    with eng.connect() as c:
        existing = c.execute(text(f"SELECT PROJECT_ID, NAME, PARENT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                             {"i": pid}).fetchone()
        if not existing:
            raise HTTPException(404, f"Project {pid} not found")

        # Recursive cascade — collect all descendants then archive
        c.execute(text(f"""
            ;WITH Descendants AS (
                SELECT PROJECT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID = :pid
                UNION ALL
                SELECT p.PROJECT_ID FROM [{PROJECT_TABLE}] p
                INNER JOIN Descendants d ON p.PARENT_ID = d.PROJECT_ID
            )
            UPDATE [{PROJECT_TABLE}]
               SET IS_ARCHIVED = 1, UPDATED_BY = :actor, UPDATED_AT = GETDATE()
             WHERE PROJECT_ID IN (SELECT PROJECT_ID FROM Descendants)
        """), {"pid": pid, "actor": actor})

        _log_activity(c, pid, "ARCHIVED", actor,
                      details=f"Archived project '{existing.NAME}' and all descendants")
        c.commit()

        if existing.PARENT_ID:
            _recompute_parent_progress(c, existing.PARENT_ID)
            c.commit()
    return APIResponse(success=True, message=f"Project {pid} archived")


@router.post("/projects/{pid}/restore", response_model=APIResponse)
def restore_project(pid: int, current_user: User = Depends(get_current_user)):
    eng = get_data_engine()
    actor = current_user.username
    with eng.connect() as c:
        c.execute(text(f"""
            UPDATE [{PROJECT_TABLE}] SET IS_ARCHIVED = 0,
                   UPDATED_BY = :actor, UPDATED_AT = GETDATE()
             WHERE PROJECT_ID = :pid
        """), {"pid": pid, "actor": actor})
        _log_activity(c, pid, "RESTORED", actor)
        c.commit()
    return APIResponse(success=True, message="Restored")


@router.post("/projects/{pid}/move", response_model=APIResponse)
def move_project(pid: int, body: MoveRequest,
                 current_user: User = Depends(get_current_user)):
    """Reparent — recalculates depth and PROJECT_TYPE."""
    eng = get_data_engine()
    actor = current_user.username
    with eng.connect() as c:
        existing = c.execute(text(f"SELECT * FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                             {"i": pid}).fetchone()
        if not existing:
            raise HTTPException(404, f"Project {pid} not found")
        if body.new_parent_id == pid:
            raise HTTPException(400, "Cannot make a project its own parent")

        new_depth = 0
        if body.new_parent_id:
            # cycle check — walk up new_parent ancestors; if pid appears, reject
            cur = body.new_parent_id
            while cur:
                if cur == pid:
                    raise HTTPException(400, "Move would create a cycle")
                p = c.execute(text(f"SELECT PARENT_ID FROM [{PROJECT_TABLE}] WHERE PROJECT_ID=:i"),
                              {"i": cur}).fetchone()
                cur = p[0] if p else None
            new_depth = _depth(c, body.new_parent_id) + 1
            if new_depth >= MAX_DEPTH:
                raise HTTPException(400, f"Move exceeds max depth {MAX_DEPTH}")

        new_type = _project_type_for_depth(new_depth)
        c.execute(text(f"""
            UPDATE [{PROJECT_TABLE}]
               SET PARENT_ID = :npid, PROJECT_TYPE = :ntype,
                   UPDATED_BY = :actor, UPDATED_AT = GETDATE()
             WHERE PROJECT_ID = :pid
        """), {"npid": body.new_parent_id, "ntype": new_type,
               "actor": actor, "pid": pid})
        _log_activity(c, pid, "MOVED", actor, field_name="parent_id",
                      old_value=existing.PARENT_ID, new_value=body.new_parent_id)
        c.commit()

        # Recompute progress on both old + new parents
        if existing.PARENT_ID:
            _recompute_parent_progress(c, existing.PARENT_ID); c.commit()
        if body.new_parent_id:
            _recompute_parent_progress(c, body.new_parent_id); c.commit()
    return APIResponse(success=True, message="Moved")


# ─────────────────────────────────────────────────────────────────────────────
# Activity log
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/projects/{pid}/activity", response_model=APIResponse)
def project_activity(pid: int, limit: int = 100,
                     current_user: User = Depends(get_current_user)):
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT TOP ({int(limit)}) *
            FROM [{ACTIVITY_TABLE}]
            WHERE PROJECT_ID = :i
            ORDER BY CREATED_AT DESC, ACTIVITY_ID DESC
        """), {"i": pid}).fetchall()
    return APIResponse(success=True, data=[_row_to_dict(r) for r in rows])


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard / Reports
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=APIResponse)
def dashboard(current_user: User = Depends(get_current_user)):
    eng = get_data_engine()
    me = current_user.username
    with eng.connect() as c:
        # KPI tiles
        kpi = c.execute(text(f"""
            SELECT
              SUM(CASE WHEN STATUS NOT IN ('COMPLETED','CANCELLED') THEN 1 ELSE 0 END) AS open_count,
              SUM(CASE WHEN STATUS = 'COMPLETED' AND COMPLETED_DATE >= DATEADD(day,-7,GETDATE())
                       THEN 1 ELSE 0 END)                                            AS completed_7d,
              SUM(CASE WHEN DUE_DATE < CAST(GETDATE() AS DATE)
                        AND STATUS NOT IN ('COMPLETED','CANCELLED')
                       THEN 1 ELSE 0 END)                                            AS overdue,
              SUM(CASE WHEN PRIORITY = 'CRITICAL'
                        AND STATUS NOT IN ('COMPLETED','CANCELLED')
                       THEN 1 ELSE 0 END)                                            AS critical_open,
              SUM(CASE WHEN (OWNER_USERNAME = :me OR ASSIGNEES LIKE :me_in)
                        AND STATUS NOT IN ('COMPLETED','CANCELLED')
                       THEN 1 ELSE 0 END)                                            AS my_open,
              COUNT(*)                                                                AS total
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0
        """), {"me": me, "me_in": f"%{me}%"}).fetchone()

        # Status distribution
        status_dist = c.execute(text(f"""
            SELECT STATUS, COUNT(*) AS cnt
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0
            GROUP BY STATUS
        """)).fetchall()

        priority_dist = c.execute(text(f"""
            SELECT PRIORITY, COUNT(*) AS cnt
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0 AND STATUS NOT IN ('COMPLETED','CANCELLED')
            GROUP BY PRIORITY
        """)).fetchall()

        phase_dist = c.execute(text(f"""
            SELECT ISNULL(PHASE,'(none)') AS PHASE, COUNT(*) AS cnt
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0 AND STATUS NOT IN ('COMPLETED','CANCELLED')
            GROUP BY PHASE
        """)).fetchall()

        # Top overdue
        top_overdue = c.execute(text(f"""
            SELECT TOP 10 PROJECT_ID, PROJECT_CODE, NAME, PRIORITY, OWNER_USERNAME, DUE_DATE,
                   DATEDIFF(day, DUE_DATE, GETDATE()) AS days_overdue
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0
              AND DUE_DATE < CAST(GETDATE() AS DATE)
              AND STATUS NOT IN ('COMPLETED','CANCELLED')
            ORDER BY days_overdue DESC
        """)).fetchall()

    return APIResponse(success=True, data={
        "kpi": {
            "open":        int(kpi[0] or 0),
            "completed_7d":int(kpi[1] or 0),
            "overdue":     int(kpi[2] or 0),
            "critical_open":int(kpi[3] or 0),
            "my_open":     int(kpi[4] or 0),
            "total":       int(kpi[5] or 0),
        },
        "status_distribution":   [{"label": r[0], "count": int(r[1])} for r in status_dist],
        "priority_distribution": [{"label": r[0], "count": int(r[1])} for r in priority_dist],
        "phase_distribution":    [{"label": r[0], "count": int(r[1])} for r in phase_dist],
        "top_overdue":           [_row_to_dict(r) for r in top_overdue],
    })


@router.get("/my-tasks", response_model=APIResponse)
def my_tasks(current_user: User = Depends(get_current_user)):
    me = current_user.username
    eng = get_data_engine()
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT *,
                   CASE WHEN DUE_DATE IS NOT NULL
                          AND DUE_DATE < CAST(GETDATE() AS DATE)
                          AND STATUS NOT IN ('COMPLETED','CANCELLED')
                        THEN 1 ELSE 0 END AS IS_OVERDUE
            FROM [{PROJECT_TABLE}]
            WHERE IS_ARCHIVED = 0
              AND STATUS NOT IN ('COMPLETED','CANCELLED')
              AND (OWNER_USERNAME = :me OR ASSIGNEES LIKE :me_in)
            ORDER BY
                CASE PRIORITY WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                              WHEN 'MEDIUM' THEN 3 WHEN 'LOW' THEN 4 ELSE 5 END,
                DUE_DATE ASC,
                PROJECT_ID DESC
        """), {"me": me, "me_in": f"%{me}%"}).fetchall()
    return APIResponse(success=True, data=[_row_to_dict(r) for r in rows])


@router.get("/enums", response_model=APIResponse)
def list_enums(current_user: User = Depends(get_current_user)):
    """Allowed values for dropdowns — keeps frontend in sync."""
    return APIResponse(success=True, data={
        "status":   sorted(ALLOWED_STATUS),
        "priority": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
        "phase":    ["PHASE_1", "PHASE_2", "PHASE_3", "BACKLOG", "ICEBOX"],
        "category": sorted(ALLOWED_CATEGORY),
    })
