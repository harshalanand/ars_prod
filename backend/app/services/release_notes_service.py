"""
Release Notes / Changelog service.

Records every change as an entry (date + area + title + plain-language detail +
a simple example), and auto-compiles them into a DAILY release note — one note
per calendar day, newest first. The frontend generates a ready-to-paste "share
note" from the same entries.

Persisted: ARS_RELEASE_NOTES (new, app-owned). Safe to write.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

TBL = "ARS_RELEASE_NOTES"


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def ensure_table() -> None:
    ddl = f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{TBL}')
        CREATE TABLE {TBL} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            note_date DATE NOT NULL,
            area NVARCHAR(80) NOT NULL DEFAULT 'General',
            emoji NVARCHAR(10) NULL,
            marker NVARCHAR(10) NOT NULL DEFAULT '✅',
            title NVARCHAR(300) NOT NULL,
            detail NVARCHAR(MAX) NULL,          -- plain-language description
            example NVARCHAR(MAX) NULL,         -- a simple, layman example
            tag NVARCHAR(60) NULL,
            sort_order INT NOT NULL DEFAULT 0,
            created_by NVARCHAR(100) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NOT NULL DEFAULT GETDATE()
        );"""
    idx = (f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_ARS_RELEASE_NOTES_date') "
           f"CREATE INDEX IX_ARS_RELEASE_NOTES_date ON {TBL}(note_date DESC, sort_order);")
    with data_engine.begin() as c:
        c.execute(text(ddl))
        c.execute(text(idx))
    _seed_history()


# ── Curated changelog (1-Jul window → to date), plain language + a simple example.
#    Tuple: (note_date, area, emoji, title, detail, example). marker auto (🐞 for fixes).
_SEED = [
    # 09-Jul
    ("2026-07-09", "Reporting", "📊", "Report Generation hub",
     "A place to build reports as steps (SQL + code) and send the output to a folder, Snowflake, email, WhatsApp or SMS — on a schedule, on demand, or when something is approved.",
     "Set up a daily sales report that lands in a folder every morning at 7 am without anyone running it by hand."),
    ("2026-07-09", "Fresh / GRT", "🔁", "Fresh vs Growth (GRT) stock pooling",
     "One stock picture is built once; each run then chooses whether to use Fresh stock or Growth-return stock — no need to maintain two separate stocks.",
     "Run today with 'Fresh', run tomorrow with 'GRT' — same setup, different pool, chosen at run time."),
    # 10-Jul
    ("2026-07-10", "Allocation engine", "⚙️", "One allocation engine",
     "The three older calculation methods were merged into a single, well-tested engine, so every run behaves the same way.",
     "Earlier the answer could depend on which method ran; now there's one path, so the numbers are consistent."),
    # 13-Jul
    ("2026-07-13", "Sec-cap", "🛡️", "Secondary caps: a hard block beats an overshoot",
     "When two rules disagree, a strict 'do not exceed' cap now correctly wins over a 'you may go slightly over' allowance.",
     "If a category is capped, the system won't quietly ship extra just because another rule allowed a small overshoot."),
    # 18-Jul
    ("2026-07-18", "Dispatch", "📦", "Dispatch only complete quantities",
     "Fixed a case where a reduced (scaled-down) quantity could slip through; dispatch now ships the full planned quantity or holds it.",
     "An order for 100 no longer goes out as a surprise 60 — it's the full 100, or it waits."),
    ("2026-07-18", "Dispatch", "📦", "Hold stock deliberately (dispatch control)",
     "Simple hold / skip lists let specific stores, divisions or articles stay pending instead of being dispatched.",
     "Put a store on the hold list before a festival and its stock stays pending until you're ready."),
    ("2026-07-18", "Data quality", "🔧", "Run dates on every allocation",
     "Each allocation now carries its stock-consider date and picking date for reference, without changing any numbers.",
     "You can see 'this plan used yesterday's stock' right on the row."),
    ("2026-07-18", "Data quality", "🔧", "Consistent rounding + data-dictionary export",
     "Numbers round the same way everywhere, and the data dictionary can be exported to Excel.",
     "Two screens showing the same value now agree down to the last decimal."),
    # 20-Jul
    ("2026-07-20", "Reporting", "📊", "WhatsApp delivery config, stored safely",
     "Report delivery over WhatsApp can be set up in the app, with the credentials encrypted so they are never stored as plain text.",
     "Save the WhatsApp key once; it's locked away encrypted, not readable in the database."),
    # 21-Jul
    ("2026-07-21", "Reporting", "📊", "Faster, reconciled Grid report",
     "The grid analytics report was rebuilt so every measure splits by dimension and still adds back up to the totals — and it now runs about 5.6× faster.",
     "A report that took ~43 seconds now finishes in under 8."),
    ("2026-07-21", "Reporting", "📊", "MSA master reconciliation report",
     "A report that compares the opening vs closing stock plan and rolls up from detail all the way to warehouse (RDC) level.",
     "See how the plan changed from morning to evening, summarised by category or RDC."),
    # 22-Jul
    ("2026-07-22", "UPC Tracking", "🏬", "UPC Store Tracking",
     "A dedicated screen to follow a product (UPC) across stores with its live MBQ, stock, and a history of dates and remarks.",
     "Type a UPC and see which stores have it, the current stock, and when it was last shared."),
    ("2026-07-22", "Reporting", "📊", "Report parameters as dropdowns + fan-out",
     "Report inputs became pick-lists, and a 'fan-out' option automatically writes one file per selected value.",
     "Pick 5 categories → get 5 separate files in a single run."),
    ("2026-07-22", "FA & CONS", "🆕", "FA & CONS foundation",
     "New groundwork for Project-Store (FA) and Consumables (CONS): per-stream location settings, a dedicated stock/MSA calculation, and an MBQ master that sorts each row into FA vs Consumables automatically by its division.",
     "Upload one MBQ sheet and the system files each row under FA or Consumables for you."),
    # 23-Jul
    ("2026-07-23", "FA & CONS", "🆕", "MBQ Change Review — see every change on any date",
     "A new tab that lists every MBQ change for a day or a date range, with a count of created / changed / deleted rows and a one-click Excel export.",
     "Open MBQ Master → Change Review → pick 'last 7 days' to see, e.g., a store's MBQ going 700 → 562, who changed it, and the reason."),
    ("2026-07-23", "FA & CONS", "🆕", "A reason is now required for every change",
     "Whenever an MBQ is uploaded or deleted, the system asks for a short reason and saves it against that change — so nothing changes without a 'why'.",
     "Try uploading without typing a reason → the system stops you with 'Reason for this change is mandatory'."),
    ("2026-07-23", "FA & CONS", "🆕", "Every upload is saved as a 'session'",
     "Each upload is recorded as one batch (who uploaded, when, the reason, and how many rows), so you can review exactly what that one upload changed.",
     "Uploaded a sheet at 2 pm? Pick that upload in the 'Upload session' box to see only the rows it touched."),
    ("2026-07-23", "FA & CONS", "🆕", "Clickable summary cards",
     "The summary cards (Created / Changed / Deleted / Approved) are buttons — click one to filter the list instantly; 'Clear filters' and 'Refresh' bring everything back.",
     "Click the 'Changed' card → the table shows only changed rows; click 'Total events' to see all again."),
    ("2026-07-23", "FA & CONS", "🆕", "Change history at a glance",
     "Each row shows a small number badge = how many times it changed (only when there is history). Hover for a quick preview; click for the full history.",
     "A '4' badge on a row means it changed 4 times — hover to peek, click to open the full list."),
    ("2026-07-23", "FA & CONS", "🆕", "Safer uploads — preview before saving",
     "Before an upload is saved you see a preview — how many rows are new, changed, or unchanged — and can Cancel if it looks wrong.",
     "The preview says '3 new, 2 changed, 10 unchanged' before anything is saved, so an accidental file can be cancelled."),
    ("2026-07-23", "Bug fix", "🐞", "Today's changes now show the same day",
     "Fixed a time-zone issue that was hiding the current day's changes from the default view until the next day.",
     "Earlier, a change made this afternoon appeared only tomorrow; now it shows today."),
    # 24-Jul
    ("2026-07-24", "Release Notes", "🗒️", "Daily changelog, recorded & shared automatically",
     "Every change is now recorded and grouped into a note per day automatically. 'Generate share note' turns a day (or a range) into a ready-to-paste update. The page is kept under Settings and limited to admins.",
     "Open Settings → What's New to read the day's changes and copy a share note for the team."),
]


def _seed_history() -> None:
    """Idempotent one-time backfill of the curated changelog. Runs once (until the
    earliest curated entry is deleted); replaces prior auto-seed rows, leaves any
    user-recorded rows untouched."""
    try:
        with data_engine.begin() as c:
            earliest = _SEED[0][0]
            done = c.execute(text(f"SELECT 1 FROM {TBL} WHERE note_date=:d"), {"d": earliest}).first()
            if done:
                return
            c.execute(text(f"DELETE FROM {TBL} WHERE created_by='system'"))  # drop prior auto-seed only
            per_day: Dict[str, int] = {}
            for note_date, area, emoji, title, detail, example in _SEED:
                so = per_day.get(note_date, 0); per_day[note_date] = so + 1
                marker = "🐞" if area == "Bug fix" else "✅"
                c.execute(text(f"""
                    INSERT INTO {TBL} (note_date, area, emoji, marker, title, detail, example, tag, sort_order, created_by)
                    VALUES (:d,:a,:e,:m,:t,:de,:ex,:tag,:so,:by)"""),
                    {"d": note_date, "a": area, "e": emoji, "m": marker, "t": title, "de": detail,
                     "ex": example, "tag": area, "so": so, "by": "system"})
        logger.info(f"[release-notes] backfilled {len(_SEED)} changelog entries")
    except Exception as e:
        logger.warning(f"[release-notes] seed skipped: {e}")


def _row(r) -> Dict[str, Any]:
    return {
        "id": r["id"], "note_date": r["note_date"].isoformat() if r["note_date"] else None,
        "area": r["area"], "emoji": r["emoji"], "marker": r["marker"], "title": r["title"],
        "detail": r["detail"], "example": r["example"], "tag": r["tag"],
        "sort_order": r["sort_order"], "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def list_entries(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 area: Optional[str] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    ensure_table()
    where, params = [], {}
    if date_from:
        where.append("note_date >= :df"); params["df"] = date_from
    if date_to:
        where.append("note_date <= :dt"); params["dt"] = date_to
    if area:
        where.append("area = :ar"); params["ar"] = area
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, note_date, area, emoji, marker, title, detail, example, tag, "
            f"sort_order, created_by, created_at FROM {TBL} {wc} "
            f"ORDER BY note_date DESC, sort_order ASC, id ASC"), params).mappings().all()
    return [_row(r) for r in rows]


def list_days(limit: int = 120) -> List[Dict[str, Any]]:
    """Distinct dates with an entry count — drives the daily grouping + bookmarks."""
    ensure_table()
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} note_date, COUNT(*) AS n FROM {TBL} "
            f"GROUP BY note_date ORDER BY note_date DESC")).mappings().all()
    return [{"note_date": r["note_date"].isoformat() if r["note_date"] else None, "count": r["n"]} for r in rows]


def add_entry(note_date: str, area: str, title: str, detail: Optional[str] = None,
              example: Optional[str] = None, emoji: Optional[str] = None,
              marker: str = "✅", tag: Optional[str] = None,
              sort_order: Optional[int] = None, user: Optional[str] = None) -> Dict[str, Any]:
    ensure_table()
    if not _clean(title):
        raise ValueError("Title is required")
    nd = _clean(note_date) or date.today().isoformat()
    ar = _clean(area) or "General"
    with data_engine.begin() as c:
        if sort_order is None:
            mx = c.execute(text(f"SELECT ISNULL(MAX(sort_order),-1) FROM {TBL} WHERE note_date=:d"),
                           {"d": nd}).scalar()
            sort_order = int(mx) + 1
        rid = c.execute(text(f"""
            INSERT INTO {TBL} (note_date, area, emoji, marker, title, detail, example, tag, sort_order, created_by)
            OUTPUT INSERTED.id
            VALUES (:d,:a,:e,:m,:t,:de,:ex,:tag,:so,:by)"""),
            {"d": nd, "a": ar, "e": _clean(emoji), "m": _clean(marker) or "✅", "t": _clean(title),
             "de": _clean(detail), "ex": _clean(example), "tag": _clean(tag) or ar,
             "so": sort_order, "by": user}).scalar()
    return {"id": int(rid), "note_date": nd}


def update_entry(entry_id: int, **fields) -> None:
    ensure_table()
    allowed = ("note_date", "area", "emoji", "marker", "title", "detail", "example", "tag", "sort_order")
    sets, params = [], {"id": entry_id}
    for k in allowed:
        if k in fields and fields[k] is not None:
            sets.append(f"{k}=:{k}")
            params[k] = fields[k] if k == "sort_order" else _clean(fields[k])
    if not sets:
        return
    sets.append("updated_at=GETDATE()")
    with data_engine.begin() as c:
        c.execute(text(f"UPDATE {TBL} SET {', '.join(sets)} WHERE id=:id"), params)


def delete_entry(entry_id: int) -> None:
    ensure_table()
    with data_engine.begin() as c:
        c.execute(text(f"DELETE FROM {TBL} WHERE id=:id"), {"id": entry_id})
