"""MCP server exposing a semantic-layer text-to-SQL interface.

The LLM never writes SQL. It discovers the vocabulary with
`list_metrics_and_dimensions`, looks up valid filter values with
`get_dimension_values`, and asks for data with `query`. The server compiles
safe, parameterised SQL and runs it read-only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .compiler import Filter, QueryRequest, compile_dimension_values, compile_query
from .executor import Executor, QueryTimeout
from .semantic_layer import SemanticError, SemanticModel

logger = logging.getLogger("text2sql_mcp")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


class FilterSpec(BaseModel):
    dimension: str = Field(description="Dimension name from list_metrics_and_dimensions.")
    op: Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains"] = "eq"
    value: Any = Field(description="A single value, or a list for 'in' / 'not_in'.")


def build_server(model_path: str | Path, db_path: str | Path, user: str | None = None) -> MCPServer:
    model = SemanticModel.from_yaml(model_path)
    executor = Executor(db_path, timeout_seconds=model.limits.timeout_seconds)

    server = MCPServer(
        name="text2sql-mcp",
        instructions=(
            "Answer analytics questions about an e-commerce store. Always call "
            "list_metrics_and_dimensions first. Use get_dimension_values before filtering "
            "on a value you have not seen. Use explain_query to check SQL without running it. "
            "Dates are ISO (YYYY-MM-DD)."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def list_metrics_and_dimensions() -> dict:
        """List every metric and dimension you can query, with descriptions."""
        catalog = model.catalog()
        scope = model.scope_for(user)
        if scope:
            catalog["your_access_scope"] = scope
        return catalog

    @server.tool(annotations=READ_ONLY)
    def get_dimension_values(dimension: str, search: str | None = None, limit: int = 50) -> dict:
        """Distinct values of a dimension (optionally matching `search`), to use in filters."""
        return _run(lambda: executor.run(compile_dimension_values(model, dimension, user, search, limit)))

    @server.tool(annotations=READ_ONLY)
    def query(
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[FilterSpec] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = None,
    ) -> dict:
        """Run an aggregate query. Group by `dimensions`; bucket order_date with `time_grain`.

        Example: revenue by category last quarter ->
        metrics=["revenue"], dimensions=["category"], start_date="2026-07-01", end_date="2026-09-30"
        """
        req = _request(metrics, dimensions, filters, start_date, end_date, time_grain, order_by, descending, limit)

        def go() -> dict:
            compiled = compile_query(model, req, user)
            result = executor.run(compiled)
            result["sql"] = compiled.sql
            return result

        return _run(go)

    @server.tool(annotations=READ_ONLY)
    def explain_query(
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[FilterSpec] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        time_grain: Literal["day", "week", "month", "quarter", "year"] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = None,
    ) -> dict:
        """Show the SQL and parameters `query` would run, without executing it."""
        req = _request(metrics, dimensions, filters, start_date, end_date, time_grain, order_by, descending, limit)

        def go() -> dict:
            compiled = compile_query(model, req, user)
            return {"sql": compiled.sql, "params": [str(p) for p in compiled.params]}

        return _run(go)

    return server


def _request(
    metrics, dimensions, filters, start_date, end_date, time_grain, order_by, descending, limit
) -> QueryRequest:
    return QueryRequest(
        metrics=metrics,
        dimensions=dimensions or [],
        filters=[Filter(f.dimension, f.op, f.value) for f in filters or []],
        start_date=start_date,
        end_date=end_date,
        time_grain=time_grain,
        order_by=order_by,
        descending=descending,
        limit=limit,
    )


def _run(fn) -> dict:
    """Turn expected failures into messages the LLM can act on."""
    try:
        return fn()
    except (SemanticError, QueryTimeout) as e:
        return {"error": str(e)}
    except Exception:
        logger.exception("query failed")
        return {"error": "Internal error while running the query. Try a simpler request."}


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    root = Path(__file__).resolve().parents[2]
    server = build_server(
        model_path=os.environ.get("TEXT2SQL_MODEL", root / "semantic_model.yaml"),
        db_path=os.environ.get("TEXT2SQL_DB", root / "data" / "shop.duckdb"),
        user=os.environ.get("TEXT2SQL_USER") or None,
    )
    server.run(os.environ.get("TEXT2SQL_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
