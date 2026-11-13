# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Opt-in result comparison for a Snowflake Semantic View and a converted Power BI model."""

import os
import sqlite3
from collections import Counter
from contextlib import ExitStack, closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

FIXTURE = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures/query-parity/sales.yaml").read_text(
        encoding="utf-8"
    )
)
ROWS = FIXTURE["rows"]
CASES = FIXTURE["cases"]


@dataclass(frozen=True)
class Column:
    name: str
    physical_name: str
    ossie_type: str
    sql_type: str
    m_type: str


COLUMNS = (
    Column("order_month", "src_order_month", "String", "VARCHAR", "type text"),
    Column("region", "src_region", "String", "VARCHAR", "type text"),
    Column("customer_id", "customer_id", "Integer", "INTEGER", "Int64.Type"),
    Column("amount", "amount", "Decimal", "NUMBER(12, 2)", "Currency.Type"),
)
COLUMN_NAMES = tuple(column.name for column in COLUMNS)
# Expected rows in the sales fixture use this metric order after the grain.
METRIC_NAMES = ("revenue", "distinct_customers", "revenue_per_customer")
METRIC_SQL = {
    "revenue": "SUM(sales.amount)",
    "distinct_customers": "COUNT(DISTINCT sales.customer_id)",
    "revenue_per_customer": "SUM(sales.amount) / NULLIF(COUNT(DISTINCT sales.customer_id), 0)",
}
RATIO_TOLERANCE = Decimal("0.000001")
LIVE_ENV = "OSSIE_RUN_LIVE_QUERY_PARITY"
LIVE_SETTINGS = (
    "OSSIE_PARITY_SNOWFLAKE_CONNECTION",
    "OSSIE_PARITY_FABRIC_WORKSPACE",
    "OSSIE_PARITY_FABRIC_TOKEN",
    "OSSIE_PARITY_POWERBI_TOKEN",
)


def _ossie_model(version):
    def expression(sql):
        return {"dialects": [{"dialect": "SNOWFLAKE", "expression": sql}]}

    return {
        "version": version,
        "name": "query_parity",
        "datasets": [{
            "name": "sales",
            "source": "PARITY.PUBLIC.SALES",
            "fields": [
                {
                    "name": column.name, "datatype": column.ossie_type,
                    "expression": expression(column.physical_name),
                }
                for column in COLUMNS
            ],
        }],
        "metrics": [
            {"name": name, "expression": expression(METRIC_SQL[name])}
            for name in METRIC_NAMES
        ],
    }


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


def _inline_m():
    def literal(value):
        if value is None:
            return "null"
        if isinstance(value, str):
            return _quoted(value.replace("#(", "#(#)("))
        return str(value)

    columns = ", ".join(_quoted(column.physical_name) for column in COLUMNS)
    rows = ",\n        ".join(
        "{" + ", ".join(literal(value) for value in row) + "}" for row in ROWS
    )
    types = ", ".join(f'{{"{column.physical_name}", {column.m_type}}}' for column in COLUMNS)
    return (
        f"let\n    Source = #table({{{columns}}}, {{\n        {rows}\n    }}),\n"
        f"    Typed = Table.TransformColumnTypes(Source, {{{types}}})\nin\n    Typed"
    )


def _create_view_sql(view, table):
    metrics = ",\n    ".join(f"sales.{name} AS {METRIC_SQL[name]}" for name in METRIC_NAMES)
    return (
        f"CREATE SEMANTIC VIEW {view}\n"
        f"TABLES (sales AS {table})\n"
        "DIMENSIONS (sales.order_month AS src_order_month, sales.region AS src_region)\n"
        f"METRICS (\n    {metrics}\n)"
    )


def _snowflake_query(view, case):
    grain = case["grain"]
    filters = case.get("filters", {})
    columns = ", ".join([*grain, *METRIC_NAMES])
    dimensions = ", ".join(f"sales.{column}" for column in grain)
    metrics = ", ".join(f"sales.{name}" for name in METRIC_NAMES)
    clauses = [view, f"METRICS {metrics}"]
    if dimensions:
        clauses.append(f"DIMENSIONS {dimensions}")
    if filters:
        clauses.append("WHERE " + " AND ".join(f"sales.{column} = %s" for column in filters))
    return f"SELECT {columns} FROM SEMANTIC_VIEW({' '.join(clauses)})", tuple(filters.values())


def _dax_query(case):
    groups = [f"'sales'[{column}]" for column in case["grain"]]
    filters = [
        f"TREATAS({{{_quoted(value)}}}, 'sales'[{column}])"
        for column, value in case.get("filters", {}).items()
    ]
    measures = [f'"{name}", [{name}]' for name in METRIC_NAMES]
    if groups:
        return f"EVALUATE SUMMARIZECOLUMNS({', '.join([*groups, *filters, *measures])})"
    row = f"ROW({', '.join(measures)})"
    if filters:
        row = f"CALCULATETABLE({row}, {', '.join(filters)})"
    return f"EVALUATE {row}"


def _dax_rows(rows, columns):
    for row in rows:
        # ExecuteQueries uses Table[Column] for base columns and [Alias] for
        # measures named in the query. includeNulls ensures explicit null keys.
        names = {key.rsplit("[", 1)[-1].removesuffix("]").casefold(): value
                 for key, value in row.items()}
        assert len(names) == len(row) and names.keys() == set(columns), (
            f"Power BI returned columns {list(row)}, expected {list(columns)}"
        )
        yield tuple(names[name] for name in columns)


def _index_metrics(rows, grain, context):
    indexed = {}
    for row in rows:
        assert len(row) == len(grain) + len(METRIC_NAMES), f"{context}: unexpected row {row!r}"
        key = tuple(row[:len(grain)])
        assert key not in indexed, f"{context}: duplicate group {key!r}"
        indexed[key] = dict(zip(METRIC_NAMES, row[len(grain):], strict=True))
    return indexed


def _assert_metrics(case, actual, expected, engines):
    context = f"{case['name']} ({engines})"
    actual = _index_metrics(actual, case["grain"], context)
    expected = _index_metrics(expected, case["grain"], context)
    assert actual.keys() == expected.keys(), (
        f"{context}: missing groups {expected.keys() - actual.keys()!r}, "
        f"unexpected groups {actual.keys() - expected.keys()!r}"
    )
    for key, metrics in actual.items():
        for name, value in metrics.items():
            target = expected[key][name]
            location = f"{context}: group {key!r}, metric {name!r}"
            if target is None or value is None:
                assert value is target, f"{location}: expected {target!r}, got {value!r}"
                continue
            for number in (value, target):
                assert type(number) in (int, float, Decimal), (
                    f"{location}: expected a number, got {number!r}"
                )
                assert Decimal(str(number)).is_finite(), f"{location}: non-finite {number!r}"
            tolerance = RATIO_TOLERANCE if name == "revenue_per_customer" else 0
            assert abs(Decimal(str(value)) - Decimal(str(target))) <= tolerance, (
                f"{location}: expected {target!r}, got {value!r}"
            )


@pytest.fixture(scope="module")
def live_engines():
    if os.environ.get(LIVE_ENV) != "1":
        pytest.skip(f"set {LIVE_ENV}=1 to run Snowflake and Power BI queries")
    missing = [name for name in LIVE_SETTINGS if not os.environ.get(name)]
    if missing:
        pytest.fail("Missing live query parity settings: " + ", ".join(missing), pytrace=False)
    connection_name, workspace, fabric_token, powerbi_token = (
        os.environ[name] for name in LIVE_SETTINGS
    )
    workspace = str(UUID(workspace))

    import snowflake.connector

    from ossie_microsoft import convert_ossie_to_semantic_model, engine
    from ossie_microsoft._common import OSSIE_VERSION

    suffix = uuid4().hex.upper()
    name = f"ossie-query-parity-{suffix}"
    # These IDs are only used to build the converter output. build_deployable
    # removes its Direct Lake source before the document is sent to Fabric.
    bim = convert_ossie_to_semantic_model(
        _ossie_model(OSSIE_VERSION), source={"workspaceId": workspace, "itemId": str(UUID(suffix))}
    )
    document = engine.build_deployable(bim, name)
    table, = document["model"]["tables"]
    partition, = table["partitions"]
    partition["source"]["expression"] = _inline_m()

    def delete_model(dataset):
        error = engine.delete_model(workspace, dataset, fabric_token)
        assert error is None, f"Could not delete Power BI model {name} ({dataset}): {error}"

    with snowflake.connector.connect(
        connection_name=connection_name, paramstyle="pyformat",
        autocommit=True, login_timeout=30, network_timeout=60,
        session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 60},
    ) as connection, connection.cursor() as cursor, ExitStack() as cleanup:
        database, schema, warehouse = cursor.execute(
            "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()"
        ).fetchone()
        assert database and schema and warehouse, (
            "The Snowflake connection must select a database, scratch schema, and warehouse"
        )
        namespace = f"{_quoted(database)}.{_quoted(schema)}"
        source_table = f"{namespace}.OSSIE_PARITY_{suffix}_SALES"
        view = f"{namespace}.OSSIE_PARITY_{suffix}_SV"
        columns = ", ".join(f"{column.physical_name} {column.sql_type}" for column in COLUMNS)
        cursor.execute(f"CREATE TABLE {source_table} ({columns})")
        cleanup.callback(cursor.execute, f"DROP TABLE IF EXISTS {source_table}")
        cursor.executemany(f"INSERT INTO {source_table} VALUES (%s, %s, %s, %s)", ROWS)
        cursor.execute(_create_view_sql(view, source_table))
        cleanup.callback(cursor.execute, f"DROP SEMANTIC VIEW IF EXISTS {view}")

        dataset, error = engine.deploy(document, workspace, name, fabric_token)
        if dataset is not None:
            cleanup.callback(delete_model, dataset)
        assert error is None and dataset, f"Could not deploy Power BI model {name}: {error}"
        error = engine.refresh(workspace, dataset, powerbi_token)
        assert error is None, f"Could not refresh Power BI model {name} ({dataset}): {error}"

        def dax(query):
            rows, error = engine.evaluate(workspace, dataset, powerbi_token, query)
            assert error is None, f"Power BI query failed: {error}\n{query}"
            return rows

        physical_names = ", ".join(column.physical_name for column in COLUMNS)
        snowflake_rows = cursor.execute(f"SELECT {physical_names} FROM {source_table}").fetchall()
        expected = Counter(map(tuple, ROWS))
        assert Counter(map(tuple, snowflake_rows)) == expected, (
            "Snowflake input differs from fixture"
        )
        assert Counter(_dax_rows(dax("EVALUATE 'sales'"), COLUMN_NAMES)) == expected, (
            "Power BI input differs from fixture"
        )

        yield cursor, view, dax


@pytest.mark.live_query_parity
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_live_measure_results_match(live_engines, case):
    cursor, view, dax = live_engines
    sql, parameters = _snowflake_query(view, case)
    snowflake = cursor.execute(sql, parameters).fetchall()
    power_bi = list(_dax_rows(dax(_dax_query(case)), (*case["grain"], *METRIC_NAMES)))
    _assert_metrics(case, snowflake, case["expected"], "Snowflake vs fixture")
    _assert_metrics(case, power_bi, case["expected"], "Power BI vs fixture")
    _assert_metrics(case, power_bi, snowflake, "Power BI vs Snowflake")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_fixture_expectations_match_relational_results(case):
    grain = [f"src_{name}" for name in case["grain"]]
    filters = case.get("filters", {})
    select = ", ".join([*grain, *(METRIC_SQL[name] for name in METRIC_NAMES)])
    sql = f"SELECT {select} FROM sales"
    if filters:
        sql += " WHERE " + " AND ".join(f"src_{name} = ?" for name in filters)
    if grain:
        sql += " GROUP BY " + ", ".join(grain)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE sales (src_order_month TEXT, src_region TEXT, "
            "customer_id INTEGER, amount REAL)"
        )
        connection.executemany("INSERT INTO sales VALUES (?, ?, ?, ?)", ROWS)
        rows = connection.execute(sql, tuple(filters.values())).fetchall()
    _assert_metrics(case, rows, case["expected"], "SQLite vs fixture")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([["North", 230, 3, 76.66666667]], "missing groups"),
        ([["North", 230, 3, 76.66666667]] * 2, "duplicate group"),
        ([["North", 230, 4, 76.66666667], ["South", 180, 2, 90], [None, 40, 1, 40]],
         "distinct_customers"),
        ([["North", 230, 3, 76.67], ["South", 180, 2, 90], [None, 40, 1, 40]],
         "revenue_per_customer"),
    ],
)
def test_comparison_rejects_group_and_value_regressions(rows, message):
    case = next(case for case in CASES if case["name"] == "by-region")
    with pytest.raises(AssertionError, match=message):
        _assert_metrics(case, rows, case["expected"], "test engine")


def test_filtered_queries_apply_the_same_grain_and_filter():
    case = next(case for case in CASES if case["name"] == "by-month-filtered-region")
    sql, parameters = _snowflake_query("PARITY_VIEW", case)
    assert "DIMENSIONS sales.order_month WHERE sales.region = %s" in sql
    assert parameters == ("North",)
    assert _dax_query(case).startswith(
        """EVALUATE SUMMARIZECOLUMNS('sales'[order_month], TREATAS({"North"}, 'sales'[region]), """
    )


def test_dax_result_requires_an_explicit_null_column():
    assert list(_dax_rows([{"sales[region]": None, "[revenue]": 40}], ("region", "revenue"))) == [
        (None, 40)
    ]
    with pytest.raises(AssertionError, match="Power BI returned columns"):
        list(_dax_rows([{"[revenue]": 40}], ("region", "revenue")))
