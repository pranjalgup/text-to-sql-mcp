"""End-to-end: call the MCP tools against a freshly generated database."""

import asyncio
import json
from pathlib import Path

import duckdb
import pytest

from text2sql_mcp.executor import Executor
from text2sql_mcp.seed import build
from text2sql_mcp.server import build_server

MODEL = Path(__file__).parents[1] / "semantic_model.yaml"


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    return build(tmp_path_factory.mktemp("data") / "shop.duckdb")


def call(server, tool, **args):
    result = asyncio.run(server.call_tool(tool, args))
    return json.loads(result.content[0].text)


def test_tools_are_registered(db):
    server = build_server(MODEL, db)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"list_metrics_and_dimensions", "get_dimension_values", "query", "explain_query"}


def test_query_returns_rows_and_sql(db):
    out = call(build_server(MODEL, db), "query", metrics=["revenue", "orders"], dimensions=["category"])
    assert out["columns"] == ["category", "revenue", "orders"]
    assert out["row_count"] == 6
    assert out["sql"].startswith("SELECT")


def test_scoped_user_only_sees_their_region(db):
    out = call(build_server(MODEL, db, user="apac_analyst"), "query", metrics=["revenue"], dimensions=["region"])
    assert [r[0] for r in out["rows"]] == ["APAC"]


def test_errors_are_returned_to_the_model(db):
    out = call(build_server(MODEL, db), "query", metrics=["profit"])
    assert "Unknown metric 'profit'" in out["error"]


def test_truncation_is_reported(db):
    out = call(build_server(MODEL, db), "query", metrics=["revenue"], dimensions=["product_name"], limit=5)
    assert out["row_count"] == 5 and out["truncated"] is True


def test_explain_does_not_execute(db):
    out = call(build_server(MODEL, db), "explain_query", metrics=["orders"], dimensions=["channel"])
    assert "rows" not in out and "GROUP BY o.channel" in out["sql"]


def test_executor_cannot_write(db):
    executor = Executor(db)
    with pytest.raises(duckdb.Error):
        executor._con.execute("DELETE FROM orders")
