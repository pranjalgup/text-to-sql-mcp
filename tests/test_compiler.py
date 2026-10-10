from pathlib import Path

import pytest

from text2sql_mcp.compiler import Filter, QueryRequest, compile_dimension_values, compile_query
from text2sql_mcp.semantic_layer import SemanticError, SemanticModel

MODEL = SemanticModel.from_yaml(Path(__file__).parents[1] / "semantic_model.yaml")


def test_joins_only_what_is_needed():
    q = compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["channel"]))
    assert "JOIN" not in q.sql
    q = compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["category"]))
    assert "JOIN products p" in q.sql and "customers" not in q.sql


def test_filter_values_are_parameters_not_sql():
    evil = "web'; DROP TABLE orders; --"
    q = compile_query(MODEL, QueryRequest(metrics=["orders"], filters=[Filter("channel", "eq", evil)]))
    assert evil not in q.sql
    assert q.params == [evil]


def test_unknown_names_suggest_corrections():
    with pytest.raises(SemanticError, match="Did you mean: revenue"):
        compile_query(MODEL, QueryRequest(metrics=["revenu"]))
    with pytest.raises(SemanticError, match="Unknown dimension 'countryy'"):
        compile_query(MODEL, QueryRequest(metrics=["orders"], dimensions=["countryy"]))


def test_raw_sql_cannot_be_smuggled_through_names():
    with pytest.raises(SemanticError):
        compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["1; DROP TABLE orders"]))


def test_access_scope_is_always_applied():
    q = compile_query(
        MODEL,
        QueryRequest(metrics=["revenue"], filters=[Filter("region", "eq", "NA")]),
        user="apac_analyst",
    )
    assert "c.region IN (?)" in q.sql
    assert q.params[-1] == "APAC"  # scope appended after the user's own filters


def test_unknown_user_gets_default_scope():
    q = compile_query(MODEL, QueryRequest(metrics=["revenue"]), user="nobody")
    assert "WHERE" not in q.sql


def test_limit_is_clamped():
    q = compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["product_name"], limit=10**6))
    assert q.limit == MODEL.limits.max_rows


def test_time_series_defaults_to_chronological_order():
    q = compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["order_date"], time_grain="month"))
    assert "date_trunc('month', o.order_date)" in q.sql
    assert "ORDER BY order_date ASC" in q.sql


def test_order_by_must_be_a_selected_column():
    with pytest.raises(SemanticError, match="order_by"):
        compile_query(MODEL, QueryRequest(metrics=["revenue"], order_by="orders"))


def test_bad_inputs_are_rejected():
    with pytest.raises(SemanticError, match="time_grain"):
        compile_query(MODEL, QueryRequest(metrics=["revenue"], dimensions=["order_date"], time_grain="hour"))
    with pytest.raises(SemanticError, match="ISO date"):
        compile_query(MODEL, QueryRequest(metrics=["revenue"], start_date="last week"))
    with pytest.raises(SemanticError, match="operator"):
        compile_query(MODEL, QueryRequest(metrics=["revenue"], filters=[Filter("channel", "like", "w")]))


def test_dimension_values_respect_scope():
    q = compile_dimension_values(MODEL, "country", user="emea_manager")
    assert "c.region IN (?)" in q.sql and "o.channel IN (?, ?)" in q.sql
