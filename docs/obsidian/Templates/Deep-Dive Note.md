---
title: {{title}}
tags: [ars, deep-dive]
updated: {{date}}
---

# {{title}} — Exhaustive Step-by-Step

The complete, ordered walkthrough. Trace everything to actual code (`file:line`). Overview lives in the parent module note.

## 1. Setup / preconditions
What earlier stages produced; the exact columns/state this step reads and writes.

## 2. The loop(s) / phases
Outer → inner ordering, and *why* the order is what it is (determinism, dependencies).

## 3. Step-by-step (in exact execution order)
| # | Step / gate | Condition | Pass path | Fail path (reason stamped) |
|---|-------------|-----------|-----------|----------------------------|
| G0 |  |  |  |  |

## 4. Math / formulas
```
exact expressions from the code
```

## 5. State that carries
How shared state mutates and changes later iterations' outcomes.

## 6. Edge cases & special branches

## 7. Output / write-back

## 8. Taxonomy
Every status / reason / remark value this step can produce, and where.

## 9. Worked example
A concrete numeric trace through every step.

## Cross-links
Link related notes with double-bracket syntax, e.g. `[[Rule Engine (per_opt)]]`.
