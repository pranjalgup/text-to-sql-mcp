"""Compile a structured query request into parameterised SQL.

Safety model:
  * identifiers (tables, columns, expressions) come only from the semantic model;
  * user-supplied values are always bound as parameters, never interpolated;
  * the caller's access scope is appended as mandatory WHERE clauses;
  * the row limit is clamped to the model's max_rows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .semantic_layer import OPERATORS, SemanticError, SemanticModel

TIME_GRAINS = {"day", "week", "month", "quarter", "year"}


@dataclass
class Filter:
    dimension: str
    op: str
    value: Any


@dataclass
class QueryRequest:
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    time_grain: str | None = None
    order_by: str | None = None
    descending: bool = True
    limit: int | None = None


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    columns: list[str]
    limit: int


def compile_query(model: SemanticModel, req: QueryRequest, user: str | None = None) -> CompiledQuery:
    if not req.metrics:
        raise SemanticError("Request at least one metric. Call list_metrics_and_dimensions to see options.")

    metrics = [model.metric(m) for m in _dedupe(req.metrics)]
    dims = [model.dimension(d) for d in _dedupe(req.dimensions)]
    if req.time_grain and req.time_grain not in TIME_GRAINS:
        raise SemanticError(f"Unknown time_grain '{req.time_grain}'. Use one of: {', '.join(sorted(TIME_GRAINS))}.")

    params: list[Any] = []
    select: list[str] = []
    group_by: list[str] = []
    joins_needed: set[str] = set()

    for d in dims:
        expr = d.sql
        if d.type == "time" and req.time_grain:
            expr = f"date_trunc('{req.time_grain}', {d.sql})::DATE"
        select.append(f"{expr} AS {d.name}")
        group_by.append(expr)
        if d.join:
            joins_needed.add(d.join)
    for m in metrics:
        select.append(f"{m.sql} AS {m.name}")

    where: list[str] = []
    for f in req.filters:
        dim = model.dimension(f.dimension)
        if dim.join:
            joins_needed.add(dim.join)
        where.append(_filter_sql(dim.sql, f, params))

    time_dim = model.dimension(model.time_dimension)
    if req.start_date:
        where.append(f"{time_dim.sql} >= ?")
        params.append(_parse_date(req.start_date, "start_date"))
    if req.end_date:
        where.append(f"{time_dim.sql} <= ?")
        params.append(_parse_date(req.end_date, "end_date"))

    # Row-level security: appended last so nothing in the request can remove it.
    for dim_name, allowed in model.scope_for(user).items():
        dim = model.dimension(dim_name)
        if dim.join:
            joins_needed.add(dim.join)
        where.append(f"{dim.sql} IN ({', '.join('?' for _ in allowed)})")
        params.extend(allowed)

    columns = [d.name for d in dims] + [m.name for m in metrics]
    if req.order_by:
        if req.order_by not in columns:
            raise SemanticError(
                f"order_by '{req.order_by}' must be one of the requested columns: {', '.join(columns)}."
            )
        order_col, direction = req.order_by, "DESC" if req.descending else "ASC"
    elif dims and dims[0].type == "time":
        order_col, direction = dims[0].name, "ASC"  # time series read naturally oldest-first
    else:
        order_col, direction = metrics[0].name, "DESC"

    limit = min(req.limit or model.limits.default_rows, model.limits.max_rows)
    if limit < 1:
        raise SemanticError("limit must be at least 1.")

    sql = [f"SELECT {', '.join(select)}", f"FROM {model.base_table} {model.base_alias}"]
    for name in sorted(joins_needed):
        j = model.joins[name]
        sql.append(f"JOIN {j.table} {j.alias} ON {j.condition}")
    if where:
        sql.append("WHERE " + "\n  AND ".join(where))
    if group_by:
        sql.append("GROUP BY " + ", ".join(group_by))
    sql.append(f"ORDER BY {order_col} {direction}")
    # Fetch one extra row so the executor can report truncation.
    sql.append(f"LIMIT {limit + 1}")

    return CompiledQuery(sql="\n".join(sql), params=params, columns=columns, limit=limit)


def _filter_sql(column: str, f: Filter, params: list[Any]) -> str:
    if f.op not in OPERATORS:
        raise SemanticError(f"Unknown filter operator '{f.op}'. Use one of: {', '.join(sorted(OPERATORS))}.")
    if f.op in ("in", "not_in"):
        values = f.value if isinstance(f.value, list) else [f.value]
        if not values:
            raise SemanticError(f"Filter '{f.dimension}' with op '{f.op}' needs a non-empty list.")
        params.extend(values)
        neg = "NOT " if f.op == "not_in" else ""
        return f"{column} {neg}IN ({', '.join('?' for _ in values)})"
    if f.op == "contains":
        params.append(f"%{f.value}%")
        return f"{column} ILIKE ?"
    sql_op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[f.op]
    params.append(f.value)
    return f"{column} {sql_op} ?"


def _parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise SemanticError(f"{name} must be an ISO date (YYYY-MM-DD), got '{value}'.") from None


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def compile_dimension_values(
    model: SemanticModel, dimension: str, user: str | None = None, search: str | None = None, limit: int = 50
) -> CompiledQuery:
    """Distinct values of a dimension, so the LLM can pick valid filter values."""
    dim = model.dimension(dimension)
    params: list[Any] = []
    joins_needed = {dim.join} if dim.join else set()
    where: list[str] = []
    if search:
        where.append(f"{dim.sql} ILIKE ?")
        params.append(f"%{search}%")
    for dim_name, allowed in model.scope_for(user).items():
        scope_dim = model.dimension(dim_name)
        if scope_dim.join:
            joins_needed.add(scope_dim.join)
        where.append(f"{scope_dim.sql} IN ({', '.join('?' for _ in allowed)})")
        params.extend(allowed)

    limit = max(1, min(limit, model.limits.max_rows))
    sql = [f"SELECT DISTINCT {dim.sql} AS {dim.name}", f"FROM {model.base_table} {model.base_alias}"]
    for name in sorted(joins_needed):
        j = model.joins[name]
        sql.append(f"JOIN {j.table} {j.alias} ON {j.condition}")
    if where:
        sql.append("WHERE " + "\n  AND ".join(where))
    sql.append(f"ORDER BY {dim.name}")
    sql.append(f"LIMIT {limit + 1}")
    return CompiledQuery(sql="\n".join(sql), params=params, columns=[dim.name], limit=limit)
