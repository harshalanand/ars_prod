# Lookup Art Master

The "VLOOKUP for SAP article codes." Take any user file with article
codes, JOIN against `vw_master_product`, return the enriched file with
descriptions, divisions, brands, etc.

---

## What it does

Replaces the manual workflow of "Excel sheet has article codes → I
need to add the descriptions → I open SAP, copy/paste, repeat 5,000
times." Instead: upload, pick join key, pick master columns, download.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/LookupArtMasterPage.jsx` |
| API | `app/api/v1/endpoints/lookup_art_master.py` |
| Service | `app/services/lookup_art_master_service.py` |
| Permission | `LOOKUP_VIEW` |

## Four-step UX flow

```
Step 1: Upload
   POST /lookup-art-master/upload
       multipart: file
       returns: { temp_id, columns: [...], row_count }

Step 2: Configure JOIN
   - User picks: which column in their file is the article code
   - User picks: which columns from VW_MASTER_PRODUCT to pull in

Step 3: Preview
   POST /lookup-art-master/join { temp_id, join_key, master_columns }
   returns: { matched, unmatched, sample_rows: [...] }

Step 4: Download
   GET /lookup-art-master/download?temp_id=...
   returns: enriched CSV/XLSX
```

## End-to-end logic

```python
# lookup_art_master_service.run_lookup
def run_lookup(temp_id, join_key, master_columns):
    user_df = read_temp_file(temp_id)             # cached on disk
    master_df = pd.read_sql(
        f"SELECT ARTICLE_NUMBER, {','.join(master_columns)} "
        f"FROM vw_master_product WITH (NOLOCK)",
        engine
    )
    enriched = user_df.merge(
        master_df,
        how='left',
        left_on=join_key,
        right_on='ARTICLE_NUMBER',
    )
    # Stats for the preview UI
    matched = enriched['ARTICLE_NUMBER'].notna().sum()
    unmatched = len(enriched) - matched
    return {
        'temp_id': temp_id,
        'matched': matched,
        'unmatched': unmatched,
        'preview': enriched.head(50).to_dict(orient='records'),
    }
```

## Why we cache the temp file

The user might run multiple JOINs against the same upload (different
`master_columns` sets). Re-uploading is friction. We persist the file
under `LOCAL_UPLOAD_DIR/<temp_id>_<filename>` keyed by a UUID returned
on upload. Auto-clean after 24 hours.

## Performance gotchas

- **`vw_master_product` is large** — pulling all columns × all rows is
  wasteful. We select only the requested columns to minimise transfer.
- **`NOLOCK` is appropriate** here because the master view is read-only
  during the day (refreshed nightly from SAP).
- **Big user files (>50k rows)** are slow because the merge happens in
  pandas memory. For files this big, we recommend Export Data instead
  (which JOINs server-side in SQL).

## Common changes

### Recipe: support fuzzy matching

When user codes don't exactly match master (whitespace, leading
zeros), the JOIN misses. Options:

```python
# Pre-normalize both sides
user_df[join_key] = user_df[join_key].astype(str).str.strip().str.lstrip('0')
master_df['ARTICLE_NUMBER'] = master_df['ARTICLE_NUMBER'].astype(str).str.strip().str.lstrip('0')
```

Or expose a "fuzzy match" toggle in the UI that runs `rapidfuzz`
similarity over unmatched rows and surfaces a "did you mean?" panel.

### Recipe: support multi-key join

Sometimes article + size, not just article. Extend the request:

```json
{ "temp_id": "...", "join_keys": ["ARTICLE","SIZE"], "master_columns": [...] }
```

In service:
```python
enriched = user_df.merge(master_df, how='left', on=join_keys)
```

### Recipe: stream the result instead of building in memory

For files >100k rows, the merge in memory is OK (pandas is good for
that size) but the download buffer is not. Stream the CSV:

```python
@router.get("/download")
def download(temp_id: str):
    def generate():
        for chunk in run_lookup_streamed(temp_id):
            yield chunk.to_csv(index=False, header=(chunk.iloc[0:0]))
    return StreamingResponse(generate(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=enriched.csv"})
```

## Cap on file size

Server enforces `MAX_UPLOAD_SIZE_MB` (default 100 MB). Beyond that,
the user should:

1. Use **Export Data** with the master view JOINed in SQL — order of
   magnitude faster.
2. Split their file into smaller chunks.

## Audit

Each lookup run writes an `audit_log` row:

```json
{
  "action_type": "LOOKUP_ENRICH",
  "table_name": "vw_master_product",
  "changed_by": "<user>",
  "details": "{\"temp_id\":\"...\",\"join_key\":\"ARTICLE\",\"row_count\":12345,\"matched\":12300,\"unmatched\":45}"
}
```

Useful for "who pulled what from the master last month."
