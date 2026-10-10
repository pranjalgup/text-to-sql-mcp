"""Run compiled queries against DuckDB in read-only mode with a timeout."""

from __future__ import annotations

import datetime as dt
import decimal
import threading
from pathlib import Path
from typing import Any

import duckdb

from .compiler import CompiledQuery


class QueryTimeout(RuntimeError):
    pass


class Executor:
    def __init__(self, db_path: str | Path, timeout_seconds: float = 10):
        if not Path(db_path).exists():
            raise FileNotFoundError(
                f"Database not found at {db_path}. Run `uv run text2sql-seed` to create the sample data."
            )
        # read_only: the server physically cannot modify data, whatever SQL reaches it.
        self._con = duckdb.connect(str(db_path), read_only=True)
        self._con.execute("SET enable_external_access = false")
        self._timeout = timeout_seconds
        self._lock = threading.Lock()

    def run(self, q: CompiledQuery) -> dict[str, Any]:
        with self._lock:
            timer = threading.Timer(self._timeout, self._con.interrupt)
            timer.start()
            try:
                rows = self._con.execute(q.sql, q.params).fetchall()
            except duckdb.InterruptException:
                raise QueryTimeout(f"Query exceeded {self._timeout}s. Narrow the date range or add filters.") from None
            finally:
                timer.cancel()

        truncated = len(rows) > q.limit
        rows = rows[: q.limit]
        return {
            "columns": q.columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v
