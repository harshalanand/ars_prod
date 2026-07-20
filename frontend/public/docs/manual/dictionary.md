# Data Dictionary — ARS Manual

## BRD — Why this exists

ARS has hundreds of computed columns with terse names (`FNL_Q`, `MJ_REQ_REM`, `werks_cap`, `PRI_CT%`). Anyone reading a table, debugging a run, or changing engine code needs to know what a column means, where it lives, and the exact formula behind it. The Data Dictionary is that reference — searchable, editable, and kept in sync with the code.

Used by operators (to understand a report column), analysts (to trace a number), and developers (to change logic without breaking a definition). It is the column-level companion to this manual's rule dossiers.

## FSD — How it works

### Inputs & outputs
- Backed by the `ARS_DATA_DICTIONARY` table (rep_data DB), served via `/api/v1/data-dictionary` (CRUD).
- Each row: `column_name`, `abbreviation` (full form), `purpose`, `related_tables`, `formula`, `module` (MSA / Grid / Listing / Allocation / Master), plus `updated_by` / `updated_at`.
- The table auto-creates and self-seeds on first use; ~100+ entries were extracted directly from the engine code.

### Rules & invariants
- Formulas are the **implemented** logic, not intent — when code changes, the entry must be updated (that is what the refresh does).
- In-memory engine variables (e.g. `werks_cap`, `take_pool`) are tagged so readers know they are logic, not physical columns.
- Entries added by the automated code-sweep carry `updated_by = 'code-sweep'`; manual edits overwrite with the editor's name.

### Validation gates — validate first, then create
Before adding a new entry, check it does not already exist (search by name) — the UI edit button is preferred over creating a duplicate.

### Key columns (the table's own schema)
| Column | Meaning |
|---|---|
| `column_name` | the ARS column being defined |
| `abbreviation` | full-form expansion |
| `purpose` | plain-words meaning |
| `related_tables` | where it lives / is used |
| `formula` | exact formula as implemented |
| `module` | pipeline area for filtering |

## Recorded rules
<!-- dated appendable rules -->
