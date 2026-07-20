"""
Report code-step registry.

A report step of type "code" names a function from this registry — NOT an
arbitrary import path. This is a security boundary: schedules/reports are
user-editable, so we never let them import or exec arbitrary modules. Only the
functions registered here can run as a report step.

To add a code step:
    @register_code_step("build_summary", "Build a summary.csv from the exported files")
    def build_summary(ctx: StepContext) -> List[dict]:
        ...
        return [{"file": path, "rows": n}]   # files this step produced

A code step receives a StepContext (session code, export dir, the files
produced by earlier steps, a read-only data-DB engine) and returns a list of
file dicts describing anything it wrote into export_dir.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

import pandas as pd
from loguru import logger

from app.database.session import get_data_engine


@dataclass
class StepContext:
    """Everything a code step is allowed to touch."""
    session_code: str
    export_dir: str
    params: Dict[str, Any] = field(default_factory=dict)
    prior_files: List[Dict[str, Any]] = field(default_factory=list)

    def engine(self):
        """Read-only access to the Data DB engine for ad-hoc queries."""
        return get_data_engine()


# name -> {"func": callable, "description": str}
_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_code_step(name: str, description: str = "") -> Callable:
    def _wrap(func: Callable[[StepContext], List[Dict[str, Any]]]):
        if name in _REGISTRY:
            raise ValueError(f"Code step already registered: {name}")
        _REGISTRY[name] = {"func": func, "description": description}
        return func
    return _wrap


def list_code_steps() -> List[Dict[str, str]]:
    """For the UI 'Add code step' picker."""
    return [
        {"name": name, "description": meta["description"]}
        for name, meta in sorted(_REGISTRY.items())
    ]


def run_code_step(name: str, ctx: StepContext) -> List[Dict[str, Any]]:
    """Run a registered code step. Raises KeyError if the name is unknown."""
    if name not in _REGISTRY:
        raise KeyError(f"Unknown code step: {name!r}")
    logger.info(f"[report] code step '{name}' on {ctx.session_code}")
    result = _REGISTRY[name]["func"](ctx)
    return result or []


# ── Built-in code steps ─────────────────────────────────────────────────────

@register_code_step(
    "build_manifest",
    "Write a manifest.csv listing every file produced by earlier steps",
)
def build_manifest(ctx: StepContext) -> List[Dict[str, Any]]:
    """Simple, always-safe example step: summarize the prior files."""
    rows = [
        {"file": os.path.basename(f.get("file", "")),
         "procedure": f.get("procedure", ""),
         "rows": f.get("rows", 0)}
        for f in ctx.prior_files
    ]
    df = pd.DataFrame(rows, columns=["file", "procedure", "rows"])
    out_path = os.path.join(ctx.export_dir, "manifest.csv")
    df.to_csv(out_path, index=False)
    return [{"procedure": "build_manifest", "file": out_path, "rows": len(df)}]


# ── Template: copy this to author your own code step ────────────────────────
# A code step is just a function that (optionally) reads params/prior files,
# does whatever Python/SQL you need, writes file(s) into ctx.export_dir, and
# returns a list describing what it wrote. Register it with a unique name and
# it shows up in the UI's "Add code step" picker automatically.
#
# The `params` you set on the step in the UI arrive as ctx.params.

@register_code_step(
    "export_query",
    "Run a SQL query (params.sql) and save it as <params.out>.csv",
)
def export_query(ctx: StepContext) -> List[Dict[str, Any]]:
    from sqlalchemy import text
    sql = ctx.params.get("sql")
    out_name = ctx.params.get("out", "query")
    if not sql:
        raise ValueError("export_query needs a 'sql' param")
    with ctx.engine().connect() as conn:
        df = pd.read_sql(text(sql), conn)
    out_path = os.path.join(ctx.export_dir, f"{out_name}.csv")
    df.to_csv(out_path, index=False)
    return [{"procedure": "export_query", "file": out_path, "rows": len(df)}]
