---
title: {{title}}
tags: [ars]
updated: {{date}}
---

# {{title}}

> One-paragraph purpose: what this module/stage does and where it sits in the pipeline. Link the stage with the pipeline wikilink.

- **Source:** `backend/app/...` (endpoint + service files)
- **Trigger / entry:** `METHOD /route`
- **Inputs → Outputs:** which tables it reads → which it writes (with grain)

## How it works
Step-by-step or part-by-part. Show the actual formulas/expressions in code blocks:
```
FORMULA = ...
```

## Key tables
| Table | Grain | Role |
|-------|-------|------|
|  |  |  |

## Key endpoints
- `METHOD /route` — purpose

## Config / tunables
Parameters and the UI knobs that map to them (defaults).

## Gotchas & invariants
- Anything that would surprise a maintainer.

## Cross-links
Link related notes with double-bracket syntax, e.g. `[[Pipeline Overview]]`, `[[ARS Glossary]]`.
