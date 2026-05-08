# How to Change Upload / Upsert Behaviour

The Upload Data screen sends files to the upsert engine. The whole flow:

```
Upload UI  →  POST /api/v1/upload/[async]  →  FileUploadService.process_upload
                                                    ↓
                                           UpsertEngine.upsert
                                            ├── Fast Bulk Path  (rows > 1000)
                                            │     - stage to #temp
                                            │     - one UPDATE
                                            │     - one INSERT
                                            └── Chunked MERGE   (fallback)
```

## Files involved

| Layer | File | What lives there |
|---|---|---|
| Route | `backend/app/api/v1/endpoints/upload.py` | `/upload/`, `/upload/async`, `/upload/jobs/*` |
| Service | `backend/app/services/file_upload_service.py` | File parsing, validation, calls upsert engine |
| Job worker | `backend/app/services/upload_job_service.py` | Background queue, heartbeat, cancel |
| Engine | `backend/app/services/upsert_engine.py` | The actual SQL — staging, MERGE, audit |
| Frontend | `frontend/src/pages/UploadPage.jsx` | The UI |

## Common changes

### Change the staging batch size

`upsert_engine.py` → `_bulk_upsert` → `batch_size` (currently 20,000).
Bigger = fewer round-trips but more memory per call. Anything above
~50,000 with NVARCHAR(MAX) staging columns will thrash the pyodbc driver.

### Change the chunked-fallback chunk size

`backend/app/core/config.py` → `UPLOAD_CHUNK_SIZE`. Default 10,000. Used
only when the fast bulk path fails and the engine falls back to chunked
MERGE.

### Disable per-row audit on bulk uploads

`file_upload_service.py` → call to `engine.upsert(...)` →
`enable_row_audit=False`. Saves writing N rows to `audit_log` for an
N-row upload. The summary audit row is always written.

### Add a new file-cleaning rule

`file_upload_service.py` → `_clean_dataframe`. Currently:
- blank/empty → `__SKIP__`
- `'|'` or `'-'` → `__NULL__`
- `'NA'` is preserved as the literal string `'NA'`

Add a branch for whatever marker you need; the engine's MERGE handles
`__SKIP__` (keep existing) and `__NULL__` (set to NULL) automatically.

## Performance gotchas

1. **NVARCHAR(MAX) staging** balloons fast_executemany memory. We use
   NVARCHAR(4000) — see `_bulk_upsert` line ~343.
2. **Synchronous upsert inside an `async def` endpoint** blocks the
   FastAPI event loop and freezes every other request. The fix already
   in place: `asyncio.to_thread(engine.upsert, ...)` — keep it that way.
3. **Audit log failures** must NOT trigger the chunked fallback. We
   isolate audit writes in their own try/except — see `upsert_engine.py`
   around line 150.

## Verifying a change

1. Restart backend.
2. Upload a known-good file (~10k rows) via the UI.
3. Confirm the result banner shows correct **Total / Inserted / Updated /
   Errors** counts.
4. Open Jobs Dashboard and confirm `duration_ms` is reasonable.
5. Spot-check `audit_log` for the batch_id.
