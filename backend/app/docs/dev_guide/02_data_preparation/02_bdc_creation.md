# BDC Creation

BDC = Business Document Creation. Upload a delivery document (typically
a SAP-generated Excel), parse it into an allocation sequence, optionally
adjust quantities per delivery order, and save the result back to the
allocation tables.

---

## What it does

Translates an external delivery sheet into ARS allocation rows. Each
parsed row maps `(article, store, quantity)` plus optional metadata
like delivery order number, expected delivery date, vendor.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/BDCCreationPage.jsx` |
| API | `app/api/v1/endpoints/bdc.py` |
| Service | `app/services/bdc_service.py` |
| Permission | `BDC_VIEW` (read), `BDC_CREATE` (write) |

## End-to-end flow

```
1. Upload Excel
   POST /bdc/upload
       multipart: file
       returns: { upload_id, sheets: ["Sheet1","Sheet2"] }

2. Pick sheet (if multi-sheet)
   POST /bdc/parse
       body: { upload_id, sheet, header_row, mapping }
       returns: { sequence_id, rows: [...] }

3. Review sequence
   GET /bdc/sequences/<sequence_id>
       returns rows with computed fields

4. (Optional) Edit delivery order quantities
   POST /bdc/delivery-order/<sequence_id>
       body: { article: 12345, store: DH24, qty: 10 }

5. Save allocation
   POST /bdc/save-allocation
       body: { sequence_id, allocation_code }
       writes to ARS_ALLOCATION_MASTER + DETAIL
       audit row in ARS_ALLOCATION_AUDIT
```

## Parsing logic

`bdc_service.parse_bdc_sheet`:

1. Read the sheet with `pd.read_excel(..., dtype=str, header=header_row)`.
2. Validate required columns are present (`MATNR`, `WERKS`, `QTY`, plus
   metadata columns the mapping config defines).
3. Strip whitespace; coerce numerics with `TRY_CAST`-equivalent in pandas.
4. JOIN with `vw_master_product` to enrich with descriptions, MAJ_CAT, etc.
5. Reject rows with missing PK columns (article + store).
6. Persist as a sequence in `bdc_sequences` (or wherever your model is).

## Example: upload + parse via API

```bash
TOKEN=...

# Step 1: upload
UP=$(curl -s -X POST "$API/api/v1/bdc/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@delivery.xlsx" | jq -r .data.upload_id)

# Step 2: parse Sheet1 with header at row 1
SEQ=$(curl -s -X POST "$API/api/v1/bdc/parse" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"upload_id\":\"$UP\",\"sheet\":\"Sheet1\",\"header_row\":0}" \
  | jq -r .data.sequence_id)

# Step 3: review
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/bdc/sequences/$SEQ" | jq '.data.rows[:5]'

# Step 4: save as allocation
curl -X POST "$API/api/v1/bdc/save-allocation" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"sequence_id\":\"$SEQ\",\"allocation_code\":\"ALC_2026_04_30_BDC1\"}"
```

## Adjusting delivery-order quantities

Sometimes the sheet says "100 units to DH24" but operations decides to
ship only 80. Rather than re-uploading, edit inline:

```python
# bdc_service.update_do_quantity
def update_do_quantity(sequence_id, article, store, new_qty, changed_by):
    old = SELECT qty FROM bdc_rows WHERE sequence_id=:s AND matnr=:a AND werks=:w
    UPDATE bdc_rows SET qty=:q WHERE ...
    audit.log_update(table='bdc_rows', record_pk=..., old_data={qty: old},
                     new_data={qty: new_qty}, changed_by=changed_by)
```

Audit trail is critical here — quantity adjustments need traceability.

## Soft-delete sequences

When a user deletes a sequence, hard-delete breaks the audit chain.
Add `deleted=1` flag instead:

```sql
ALTER TABLE bdc_sequences ADD deleted BIT NOT NULL DEFAULT 0;

-- Listing query filters them out:
SELECT * FROM bdc_sequences WHERE deleted = 0;

-- Recovery:
UPDATE bdc_sequences SET deleted=0 WHERE id=...;
```

## Common changes

### Recipe: support a new BDC sheet format

The expected columns differ between SAP outputs. Add a "format"
selector on the parse step:

1. Define formats in `app/services/bdc_formats.py`:
   ```python
   FORMATS = {
     "v1_classic": {
       "matnr_col": "Material",
       "werks_col": "Plant",
       "qty_col": "Qty",
       ...
     },
     "v2_2026": {
       "matnr_col": "MATNR",
       ...
     },
   }
   ```
2. Pass `format` in the parse request; backend looks up the column
   mapping.
3. Frontend offers the dropdown.

### Recipe: speed up parsing for large multi-sheet files

`pd.read_excel` on a 100k-row, 5-sheet workbook is slow. Two options:

1. **Pre-validate** the sheet name + header row without loading the
   data. `openpyxl.load_workbook(read_only=True)` lets you inspect
   sheet names and dimensions cheaply.
2. **Convert XLSX to CSV** server-side once, then parse the CSV. Pandas
   reads CSV ~10× faster than XLSX.

### Recipe: cache parsed result by file hash

```python
import hashlib
file_hash = hashlib.sha256(content).hexdigest()
cached = parsed_cache.get(file_hash)
if cached:
    return cached
parsed = parse_bdc_sheet(...)
parsed_cache[file_hash] = parsed
```

Re-uploading the same file becomes instant — useful when ops re-uploads
to "double-check."

## Save-allocation logic

```python
# bdc_service.save_to_allocation
def save_to_allocation(sequence_id, allocation_code, changed_by):
    # 1. Master row
    INSERT INTO ARS_ALLOCATION_MASTER (
        allocation_code, type, source, status, created_by, created_at, ...
    ) VALUES (:code, 'BDC', 'BDC_UPLOAD', 'pending', :user, GETDATE(), ...)

    # 2. Detail rows (bulk)
    INSERT INTO ARS_ALLOCATION_DETAIL (
        allocation_code, store_code, article_number, qty
    ) SELECT :code, werks, matnr, qty FROM bdc_rows WHERE sequence_id=:s

    # 3. Audit
    INSERT INTO ARS_ALLOCATION_AUDIT (
        allocation_code, action, by, at, details
    ) VALUES (:code, 'CREATED_FROM_BDC', :user, GETDATE(), '...')
```

Why three tables? Master = header (one row per allocation), Detail =
lines (one per store-article), Audit = history.

## Performance reference

| Operation | Time |
|---|---|
| Upload 5MB Excel | 2-3 s (network + read) |
| Parse 50k-row sheet | 5-10 s (openpyxl) |
| Save 50k-row allocation | 5-15 s (bulk INSERT) |
| List sequences (paginated) | <500 ms |
| Adjust DO qty | <100 ms |

## Watch out for

- **Header row detection**: some SAP exports have 5 rows of metadata before headers. Always make `header_row` explicit, never auto-guess (auto-guessing is a support-ticket factory).
- **Excel cell-type weirdness**: a date cell that LOOKS like `2026-04-29` might internally be a serial number `46145`. Use `dtype=str` and parse explicitly.
- **Trailing whitespace in keys**: `"DH24 "` and `"DH24"` look identical but don't match. RTRIM in pandas before any join.
