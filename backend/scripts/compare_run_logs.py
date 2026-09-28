#!/usr/bin/env python
"""
Compare the step-by-step signatures of two listing runs' logs.

    python scripts/compare_run_logs.py <base.log> <replay.log>

Extracts every line carrying a row count or a tagged breakdown and compares
them step by step, ignoring timestamps and elapsed seconds. Two runs of the
same parameters over the same data should agree on every count; the first
step where they diverge is where to look.
"""
from __future__ import annotations

import re
import sys

# Windows consoles default to cp1252 and choke on the stopwatch glyph.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Lines worth comparing: anything reporting counts the pipeline computed.
_PAT = re.compile(
    r"(Part [\d.]+[a-z]?)[^|]*?[:(]\s*(.*)$"
)
_TIME = re.compile(r"\d[\d.]*s\b")
# loguru's file sink separates the message with '|', the stderr sink with '-'.
_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2} [\d:.]+\s*\|\s*\w+\s*\|\s*[\w.]+:[\w<>]+:\d+\s*[-|]\s*")
_BATCH = re.compile(r"batch=\S+")


def signatures(path: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            # ANSI colour codes come BEFORE the timestamp — strip them first.
            line = re.sub(r"\x1b\[[0-9;]*m", "", raw.strip())
            line = _TS.sub("", line)
            # loguru escapes the stopwatch glyph on some sinks
            line = line.replace("\\u23f1", "⏱").replace("\\u2192", "→")
            if not line.startswith("Part") and "Part " not in line[:40]:
                continue
            # Elapsed times are machine load, not behaviour — drop them
            # everywhere (the ⏱ lines AND the end-of-run summary block).
            line = _TIME.sub("<s>", line)
            # A dict repr has no stable key order across runs; compare as a set.
            if "{" in line and "}" in line:
                pre, _, rest = line.partition("{")
                body, _, post = rest.rpartition("}")
                line = pre + "{" + ", ".join(sorted(
                    p.strip() for p in body.split(","))) + "}" + post
            if not re.search(r"\d", line):
                continue
            line = _BATCH.sub("batch=<sid>", line)
            # A cp1252 console mangles the em-dash to U+FFFD; neither the
            # dash style nor the column padding is behaviour.
            line = re.sub(r"[—–�]", "-", line)
            line = re.sub(r"\s+", " ", line).strip()
            m = re.match(r"(Part [\d.]+[a-z]?)", line.lstrip("⏱ ").strip())
            step = m.group(1) if m else line[:18]
            out.append((step, line))
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = signatures(sys.argv[1]), signatures(sys.argv[2])
    print(f"base   {sys.argv[1]}  ({len(a)} signature lines)")
    print(f"replay {sys.argv[2]}  ({len(b)} signature lines)\n")

    # align on the text, report first divergence per step
    amap: dict[str, list[str]] = {}
    for s, l in a:
        amap.setdefault(s, []).append(l)
    bmap: dict[str, list[str]] = {}
    for s, l in b:
        bmap.setdefault(s, []).append(l)

    same = diff = 0
    for step in sorted(set(amap) | set(bmap), key=lambda s: [
            float(x) if x.isdigit() else 0 for x in re.findall(r"\d+", s)] or [0]):
        la, lb = amap.get(step, []), bmap.get(step, [])
        if la == lb:
            same += len(la)
            continue
        diff += 1
        print(f"--- {step} DIFFERS ---")
        for x in la:
            if x not in lb:
                print(f"  base   : {x[:200]}")
        for x in lb:
            if x not in la:
                print(f"  replay : {x[:200]}")
    print(f"\n{same} signature lines identical, {diff} steps differ")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
