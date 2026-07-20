"""
Report delivery — convert report output to Excel/PDF/Word and email it.

Used by the Report Generation engine when a report has EMAIL_CONFIG enabled.
After a run produces its files, this:
  1. converts each data file to the requested attachment format
     (source | xlsx | pdf | docx),
  2. optionally zips them into one attachment,
  3. emails them to the configured to / cc / bcc via SMTP.

Sending uses Python's stdlib smtplib and is DISABLED until settings.SMTP_HOST is
set — send_report_email() returns a clear, non-fatal error otherwise, so a run
never crashes just because SMTP isn't configured yet.

EMAIL_CONFIG (JSON on ARS_REPORTS):
    {
      "enabled": true,
      "to": ["a@x.com"], "cc": [...], "bcc": [...],
      "subject": "Daily allocation report",
      "body": "See attached.",
      "attach_format": "xlsx",   # source | xlsx | pdf | docx
      "zip": false               # force-zip; auto-true when >1 attachment
    }
"""
import os
import re
import smtplib
import zipfile
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATA_EXTS = (".csv", ".xlsx")
MAX_PDF_ROWS = 2000     # keep emailed PDFs sane
MAX_DOCX_ROWS = 5000


def _smtp_config() -> Dict[str, Any]:
    """Resolve SMTP settings, preferring what the Settings → Email UI saved
    (app_settings.json) and falling back to env-based settings.SMTP_*.

    This keeps report emails and the Settings "Send Test" button on ONE config.
    """
    host = user = password = sender = ""
    port, use_tls = 587, True
    try:
        from app.api.v1.endpoints.settings import load_app_settings
        ecfg = (load_app_settings() or {}).get("email", {}) or {}
        host = ecfg.get("smtp_server") or ""
        port = int(ecfg.get("smtp_port") or 587)
        user = ecfg.get("smtp_username") or ""
        password = ecfg.get("smtp_password") or ""
        sender = ecfg.get("from_address") or ""
        use_tls = bool(ecfg.get("use_tls", True))
    except Exception as e:
        logger.debug(f"[delivery] app_settings email read failed: {e}")
    # Env fallback (settings.SMTP_*) for anything the UI left blank.
    host = host or (settings.SMTP_HOST or "")
    user = user or (settings.SMTP_USER or "")
    password = password or (settings.SMTP_PASSWORD or "")
    sender = sender or (settings.SMTP_FROM or "") or user
    return {"host": host, "port": port, "user": user,
            "password": password, "from": sender, "use_tls": use_tls}


# ── Format conversion ───────────────────────────────────────────────────────
def _read_data(path: str) -> pd.DataFrame:
    if path.lower().endswith(".xlsx"):
        return pd.read_excel(path)
    return pd.read_csv(path)


def _to_xlsx(src: str) -> str:
    if src.lower().endswith(".xlsx"):
        return src
    out = os.path.splitext(src)[0] + ".xlsx"
    _read_data(src).to_excel(out, index=False)
    return out


def _to_docx(src: str) -> str:
    from docx import Document
    df = _read_data(src)
    truncated = len(df) > MAX_DOCX_ROWS
    if truncated:
        df = df.head(MAX_DOCX_ROWS)
    doc = Document()
    doc.add_heading(os.path.splitext(os.path.basename(src))[0], level=1)
    if truncated:
        doc.add_paragraph(f"(showing first {MAX_DOCX_ROWS:,} rows)")
    cols = [str(c) for c in df.columns]
    table = doc.add_table(rows=1, cols=len(cols))
    table.style = "Light Grid Accent 1"
    for i, c in enumerate(cols):
        table.rows[0].cells[i].text = c
    for _, row in df.iterrows():
        cells = table.add_row().cells
        for i, c in enumerate(df.columns):
            cells[i].text = "" if pd.isna(row[c]) else str(row[c])
    out = os.path.splitext(src)[0] + ".docx"
    doc.save(out)
    return out


def _to_pdf(src: str) -> str:
    from fpdf import FPDF
    df = _read_data(src)
    truncated = len(df) > MAX_PDF_ROWS
    if truncated:
        df = df.head(MAX_PDF_ROWS)
    cols = [str(c) for c in df.columns]

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, os.path.splitext(os.path.basename(src))[0], ln=1)
    if truncated:
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 5, f"(showing first {MAX_PDF_ROWS:,} rows)", ln=1)

    usable = pdf.w - 2 * pdf.l_margin
    col_w = max(18.0, usable / max(1, len(cols)))
    pdf.set_font("Helvetica", "B", 7)
    for c in cols:
        pdf.cell(col_w, 6, str(c)[:22], border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 7)
    for _, row in df.iterrows():
        for c in df.columns:
            val = "" if pd.isna(row[c]) else str(row[c])
            pdf.cell(col_w, 5, val[:22], border=1)
        pdf.ln()
    out = os.path.splitext(src)[0] + ".pdf"
    pdf.output(out)
    return out


def _convert(src: str, fmt: str) -> str:
    if fmt == "xlsx":
        return _to_xlsx(src)
    if fmt == "docx":
        return _to_docx(src)
    if fmt == "pdf":
        return _to_pdf(src)
    return src  # 'source' — attach as produced


def build_attachments(files: List[Dict[str, Any]], attach_format: str,
                      export_dir: str, base_name: str,
                      zip_all: bool = False) -> List[str]:
    """Convert produced data files to attach_format; return attachment paths.

    Non-data artifacts (already-made files that aren't csv/xlsx) are attached
    as-is. If more than one attachment (or zip_all), everything is zipped into
    one <base_name>.zip.
    """
    paths: List[str] = []
    for f in files:
        p = f.get("file")
        if not p or not os.path.exists(p):
            continue
        if p.lower().endswith(_DATA_EXTS):
            try:
                paths.append(_convert(p, attach_format))
            except Exception as e:
                logger.warning(f"[delivery] convert {p} -> {attach_format} failed: {e}; "
                               f"attaching source")
                paths.append(p)
        else:
            paths.append(p)

    # de-dupe (a source→source conversion can repeat paths)
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p); uniq.append(p)
    paths = uniq

    if paths and (zip_all or len(paths) > 1):
        zpath = os.path.join(export_dir, f"{base_name}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, arcname=os.path.basename(p))
        return [zpath]
    return paths


# ── Email send ──────────────────────────────────────────────────────────────
def _clean_addrs(values) -> List[str]:
    out = []
    for v in (values or []):
        v = str(v).strip()
        if v and _EMAIL_RE.match(v):
            out.append(v)
    return out


def send_report_email(email_config: Dict[str, Any], report_name: str,
                      files: List[Dict[str, Any]], export_dir: str,
                      session_code: str) -> Dict[str, Any]:
    """Send the report's output by email. Never raises — returns a status dict."""
    cfg = email_config or {}
    to = _clean_addrs(cfg.get("to"))
    cc = _clean_addrs(cfg.get("cc"))
    bcc = _clean_addrs(cfg.get("bcc"))
    if not to and not cc and not bcc:
        return {"sent": False, "error": "no valid recipients"}

    smtp = _smtp_config()
    if not smtp["host"]:
        return {"sent": False,
                "error": "SMTP not configured (set it in Settings → Email)"}

    attach_format = str(cfg.get("attach_format", "source")).lower()
    if attach_format not in ("source", "xlsx", "pdf", "docx"):
        attach_format = "source"

    # attach_scope: 'all' (default) attaches every produced data file;
    # 'manifest' attaches only the manifest file (add the build_manifest step).
    scope = str(cfg.get("attach_scope", "all")).lower()
    deliver_files = files
    if scope == "manifest":
        deliver_files = [f for f in files
                         if "manifest" in os.path.basename(f.get("file", "")).lower()]
        if not deliver_files:
            return {"sent": False,
                    "error": "‘Email manifest only’ is set but no manifest file was "
                             "produced — add the build_manifest code step to this report"}

    try:
        attachments = build_attachments(
            deliver_files, attach_format, export_dir,
            base_name=f"{_safe(report_name)}_{session_code}",
            zip_all=bool(cfg.get("zip")))
    except Exception as e:
        return {"sent": False, "error": f"attachment build failed: {e}"}

    msg = EmailMessage()
    sender = smtp["from"] or smtp["user"] or "ars@localhost"
    msg["From"] = sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = cfg.get("subject") or f"Report: {report_name}"
    msg.set_content(cfg.get("body") or f"Automated report '{report_name}'.\n"
                    f"Session: {session_code}\nAttachments: {len(attachments)}")

    for p in attachments:
        try:
            with open(p, "rb") as fh:
                data = fh.read()
            msg.add_attachment(data, maintype="application",
                               subtype="octet-stream",
                               filename=os.path.basename(p))
        except Exception as e:
            logger.warning(f"[delivery] attach {p} failed: {e}")

    all_rcpts = to + cc + bcc
    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as s:
            if smtp["use_tls"]:
                s.starttls()
            if smtp["user"]:
                s.login(smtp["user"], smtp["password"])
            s.send_message(msg, from_addr=sender, to_addrs=all_rcpts)
        logger.info(f"[delivery] emailed '{report_name}' to {len(all_rcpts)} "
                    f"recipient(s), {len(attachments)} attachment(s)")
        return {"sent": True, "to": to, "cc": cc, "bcc_count": len(bcc),
                "attachments": [os.path.basename(p) for p in attachments],
                "format": attach_format}
    except Exception as e:
        logger.error(f"[delivery] send failed: {e}")
        return {"sent": False, "error": str(e)}


def send_failure_email(email_config: Dict[str, Any], report_name: str,
                       session_code: str,
                       errors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Send a plain-text FAILURE ALERT (no attachments). Never raises."""
    cfg = email_config or {}
    to = _clean_addrs(cfg.get("to"))
    cc = _clean_addrs(cfg.get("cc"))
    bcc = _clean_addrs(cfg.get("bcc"))
    if not to and not cc and not bcc:
        return {"sent": False, "error": "no valid recipients"}
    smtp = _smtp_config()
    if not smtp["host"]:
        return {"sent": False, "error": "SMTP not configured (Settings → Email)"}

    lines = [f"Report '{report_name}' had errors during its run.",
             f"Session: {session_code}", "", "Errors:"]
    for e in (errors or []):
        step = e.get("step", e.get("name", "?"))
        lines.append(f"  • [{step}] {e.get('error', e)}")
    body = "\n".join(lines)

    msg = EmailMessage()
    sender = smtp["from"] or smtp["user"] or "ars@localhost"
    msg["From"] = sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = f"[FAILED] {report_name}"
    msg.set_content(body)

    all_rcpts = to + cc + bcc
    try:
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as s:
            if smtp["use_tls"]:
                s.starttls()
            if smtp["user"]:
                s.login(smtp["user"], smtp["password"])
            s.send_message(msg, from_addr=sender, to_addrs=all_rcpts)
        logger.info(f"[delivery] failure alert for '{report_name}' → {len(all_rcpts)} recipient(s)")
        return {"sent": True, "kind": "failure_alert", "to": to, "cc": cc, "bcc_count": len(bcc)}
    except Exception as e:
        logger.error(f"[delivery] failure alert send failed: {e}")
        return {"sent": False, "error": str(e)}


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", str(name).strip())[:60] or "report"
