"""Generate a deterministic synthetic e-commerce dataset in DuckDB.

~2,000 customers, 60 products and ~60,000 order lines across Jan 2025 - Sep 2026,
with seasonality, channel mix and returns, so questions have interesting answers.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

DEFAULT_DB = Path("data/shop.duckdb")

SQL = """
SELECT setseed(0.42);

CREATE TABLE customers AS
SELECT
    i AS customer_id,
    ['India','Singapore','Japan','Australia','Germany','France','United Kingdom',
     'United States','Canada','Brazil','Mexico'][1 + (hash(i) % 11)::INT] AS country,
    ['consumer','consumer','consumer','smb','enterprise'][1 + (hash(i * 7) % 5)::INT] AS segment,
    DATE '2024-06-01' + ((hash(i * 13) % 600)::INT) AS signup_date
FROM range(1, 2001) t(i);

ALTER TABLE customers ADD COLUMN region VARCHAR;
UPDATE customers SET region = CASE
    WHEN country IN ('India','Singapore','Japan','Australia') THEN 'APAC'
    WHEN country IN ('Germany','France','United Kingdom') THEN 'EMEA'
    WHEN country IN ('United States','Canada') THEN 'NA'
    ELSE 'LATAM' END;

CREATE TABLE products AS
SELECT
    i AS product_id,
    cat AS category,
    cat || ' ' || ['Basic','Plus','Pro','Max','Lite','Mini'][1 + ((i - 1) // 10)::INT] || ' ' || (1 + (i - 1) % 10) AS product_name,
    round(base * (0.6 + (hash(i) % 100) / 100.0), 2) AS list_price
FROM (
    SELECT i,
        ['Electronics','Home','Fashion','Books','Sports','Beauty'][1 + ((i - 1) % 6)::INT] AS cat,
        [220, 60, 45, 18, 75, 30][1 + ((i - 1) % 6)::INT] AS base
    FROM range(1, 61) t(i)
);

CREATE TABLE orders AS
WITH raw AS (
    SELECT
        i AS line_id,
        (i + 1) // 2 AS order_id,                              -- ~2 lines per order
        1 + floor(random() * 2000)::INT AS customer_id,
        1 + floor(random() * 60)::INT AS product_id,
        DATE '2025-01-01' + floor(pow(random(), 0.85) * 638)::INT AS order_date,
        random() AS r_channel, random() AS r_status, random() AS r_qty
    FROM range(1, 60001) t(i)
)
SELECT
    line_id, order_id, customer_id, raw.product_id, order_date,
    CASE WHEN r_channel < 0.5 THEN 'web' WHEN r_channel < 0.85 THEN 'app' ELSE 'marketplace' END AS channel,
    CASE
        WHEN r_status < (CASE p.category WHEN 'Fashion' THEN 0.14 WHEN 'Electronics' THEN 0.07 ELSE 0.04 END) THEN 'returned'
        WHEN r_status < 0.17 THEN 'cancelled'
        WHEN order_date > DATE '2026-09-20' THEN 'shipped'
        ELSE 'delivered' END AS status,
    1 + floor(pow(r_qty, 3) * 4)::INT AS quantity,
    -- November/December sale prices
    round(p.list_price * CASE WHEN month(order_date) IN (11, 12) THEN 0.85 ELSE 1.0 END, 2) AS unit_price
FROM raw JOIN products p USING (product_id);
"""


def build(db_path: Path = DEFAULT_DB) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    for stmt in filter(str.strip, SQL.split(";")):
        con.execute(stmt)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("customers", "products", "orders")}
    con.close()
    print(f"Created {db_path}: " + ", ".join(f"{n:,} {t}" for t, n in counts.items()))
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    build(parser.parse_args().db)


if __name__ == "__main__":
    main()
