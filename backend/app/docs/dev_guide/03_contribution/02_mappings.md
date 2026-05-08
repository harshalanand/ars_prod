# Contribution % — Mappings

Two-tab page: **SSN Mappings** and **Assignments**. Together they
decide which preset gets applied to which output column for which
target scope.

---

## Conceptual model

```
SSN Mapping:
   "FASHION" → primary preset L30D, fallback L18M
   "BASIC"   → primary preset L18M, fallback L7D

Assignment:
   STORE_CONTRIB_<MAJ_CAT>   gets value from "FASHION" mapping → Store scope
   COMPANY_CONTRIB_<MAJ_CAT> gets value from "FASHION" mapping → Company scope
```

When the contribution engine runs, for each (store, article):
1. Look up the article's SSN.
2. Find the SSN mapping → primary preset.
3. If primary preset returns no data, try the fallback.
4. Output the resulting contribution into each assigned column.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ContribMappingsPage.jsx` |
| API | `app/api/v1/endpoints/contrib.py` (`/contrib/mappings/*`) |
| Service | `app/services/contrib_service.py` |
| Permission | `CONTRIB_MAPPINGS` |

## Tables

`ARS_CONTRIB_SSN_MAPPINGS`:
```
id, ssn (e.g. "FASHION"), primary_preset_id, fallback_preset_id, suffix
```

`ARS_CONTRIB_ASSIGNMENTS`:
```
id, output_column_template (e.g. "STORE_CONTRIB_<MAJ_CAT>"),
ssn_mapping_id, prefix, target  -- 'Store' / 'Company' / 'Both'
```

## Operations

| Op | Endpoint |
|---|---|
| List SSN mappings | `GET /contrib/mappings/ssn` |
| Create / update / delete SSN mapping | standard CRUD |
| List assignments | `GET /contrib/mappings/assignments` |
| Create / update / delete assignment | standard CRUD |

## Validation rules — what to enforce on save

### Rule 1: no fallback cycles

```
A → fallback B → fallback A    ← infinite loop
```

`contrib.py` POST/PUT for SSN mappings should run a DFS:

```python
def has_cycle(mappings: dict, start: str, seen: set | None = None) -> bool:
    seen = seen or set()
    if start in seen: return True
    seen.add(start)
    fb = mappings.get(start, {}).get('fallback')
    return has_cycle(mappings, fb, seen) if fb else False

# Before save:
new_mappings = current_mappings + [the_proposed_one]
if has_cycle(new_mappings, the_proposed_one.ssn):
    raise HTTPException(400, "Mapping creates a fallback cycle")
```

### Rule 2: no orphaned preset references

Each `primary_preset_id` and `fallback_preset_id` must exist in
`ARS_CONTRIB_PRESETS`. Foreign key won't catch the case where a preset
is deleted later — add an "orphaned mappings" check on the page.

### Rule 3: unique (template, target) on assignments

Two assignments writing to the same output column are redundant at best,
contradictory at worst. Enforce a unique constraint:

```sql
ALTER TABLE ARS_CONTRIB_ASSIGNMENTS
ADD CONSTRAINT UQ_assignment UNIQUE (output_column_template, target);
```

## Example: configure a clean SSN cascade

```bash
# Create SSN mappings
curl -X POST "$API/api/v1/contrib/mappings/ssn" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{ "ssn": "FASHION",   "primary_preset_id": 1, "fallback_preset_id": 2, "suffix": "_F" }'

curl -X POST "$API/api/v1/contrib/mappings/ssn" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{ "ssn": "BASIC",     "primary_preset_id": 2, "fallback_preset_id": null, "suffix": "_B" }'

# Create assignment
curl -X POST "$API/api/v1/contrib/mappings/assignments" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "output_column_template": "STORE_CONTRIB_<MAJ_CAT>",
    "ssn_mapping_id": 1,
    "prefix": "STORE_CONTRIB_",
    "target": "Store"
  }'
```

## Common changes

### Recipe: visualize the mapping graph

Build a graph view in the UI showing nodes (SSN values) connected to
their primary preset and fallback chain. Easy to spot cycles or holes.

### Recipe: bulk-edit assignments

Mass-update all `STORE_CONTRIB_*` assignments to point to a new SSN
mapping:

```sql
UPDATE ARS_CONTRIB_ASSIGNMENTS
SET ssn_mapping_id = :new_id, updated_at = GETDATE()
WHERE output_column_template LIKE 'STORE_CONTRIB_%';
```

Wrap in an admin-only endpoint with audit logging.

### Recipe: preview output column names before save

Frontend can compute `<MAJ_CAT>` substitution locally — show user the
resolved column names like `STORE_CONTRIB_MENS_APP`,
`STORE_CONTRIB_WOMENS_APP`. Catches typos before they hit the engine.

## Performance

Both tables are tiny (<200 rows typically). No indexing concerns. The
heavier work happens in `contrib_service.py` when this configuration
is *applied* during Execute.
