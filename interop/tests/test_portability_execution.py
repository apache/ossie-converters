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

"""Tests for the report-only execution check behind the core portability grader.

Each test grades an input document against a returned document whose metric was
rewritten, then checks what running both expressions in DuckDB found. The M0-M3
fixtures from the pinned ossie submodule are used where they fit; a test that
depends on how a fixture writes a metric sets that metric itself, so a later
submodule bump cannot change what it checks. Tests that execute SQL are skipped
when duckdb or sqlglot is not installed; the last test simulates that case.
"""

import copy
import importlib.util
import json
import os
import sys

import pytest

from interop import portability_execution as execute

grade = execute.grader()

requires_engine = pytest.mark.skipif(
    importlib.util.find_spec("duckdb") is None
    or importlib.util.find_spec("sqlglot") is None,
    reason="duckdb and sqlglot are needed to execute expressions",
)

PORTABILITY_DIR = os.path.join(execute.ROOT, "ossie", "compliance", "portability")


def _fixture(relative_path):
    return grade._load(os.path.join(PORTABILITY_DIR, relative_path))


def _with_metric(document, name, expression, dialect="ANSI_SQL"):
    """A copy of ``document`` with metric ``name`` set to one expression."""
    returned = copy.deepcopy(document)
    for metric in returned["metrics"]:
        if metric["name"] == name:
            metric["expression"] = {
                "dialects": [{"dialect": dialect, "expression": expression}]
            }
    return returned


def _sales(fields, metric_expression):
    """A one-dataset document; ``fields`` is a list of (name, datatype), each a bare column."""
    return {
        "version": "0.2.0.dev0",
        "name": "t",
        "datasets": [
            {
                "name": "sales",
                "source": "db.sales",
                "fields": [
                    {
                        "name": n,
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": str(n)}]
                        },
                        "datatype": d,
                    }
                    for n, d in fields
                ],
            }
        ],
        "metrics": [
            {
                "name": "m",
                "expression": {
                    "dialects": [
                        {"dialect": "ANSI_SQL", "expression": metric_expression}
                    ]
                },
            }
        ],
    }


def _run(fields, authored, returned, **kwargs):
    report = execute.check(_sales(fields, authored), _sales(fields, returned), **kwargs)
    assert len(report.findings) == 1, [(f.name, f.outcome) for f in report.findings]
    return report.findings[0]


AMOUNT_STATUS = [("amount", "Decimal"), ("status", "String")]
ELSE_ZERO = "SUM(CASE WHEN sales.status = 'completed' THEN sales.amount ELSE 0 END)"
NO_ELSE = "SUM(CASE WHEN sales.status = 'completed' THEN sales.amount END)"


@requires_engine
def test_issue_20_else_zero_differs_from_no_else_on_a_group_with_no_match():
    # apache/ossie-converters #20: SUM(CASE ... ELSE 0 END) gives 0 where no row
    # matches; without ELSE 0 it gives NULL.
    src = _with_metric(
        _fixture("m3/m3_filtered_agg_qualified.ossie.yaml"),
        "completed_amount",
        ELSE_ZERO,
    )
    returned = _with_metric(src, "completed_amount", NO_ELSE)
    (finding,) = execute.check(src, returned).findings
    assert finding.outcome == execute.DIFFERENT
    assert 1 <= len(finding.rows) <= 4
    assert (finding.authored_result, finding.returned_result) == (0, None)
    status = finding.columns.index("status")
    assert all(row[status] != "completed" for row in finding.rows)


@requires_engine
def test_dbt_count_rewrite_differs_on_an_empty_table():
    # What the dbt converter returns today for COUNT(sales.order_id), see
    # apache/ossie-converters@62a3066 dbt/src/ossie_dbt/msi_to_ossie.py:325.
    src = _fixture("m2/m2_aggregates_qualified.ossie.yaml")
    rewrite = "SUM(CASE WHEN sales.order_id IS NOT NULL THEN 1 ELSE 0 END)"
    (finding,) = execute.check(src, _with_metric(src, "count_orders", rewrite)).findings
    assert finding.outcome == execute.DIFFERENT
    assert finding.rows == []
    assert (finding.authored_result, finding.returned_result) == (0, None)
    assert "empty table" in finding.detail


@requires_engine
def test_bare_and_qualified_variants_of_every_rung_are_same_on_test_data():
    pairs = [
        ("m0/m0_sum_bare.ossie.yaml", "m0/m0_ansi_qualified.ossie.yaml", 1),
        (
            "m2/m2_aggregates_bare.ossie.yaml",
            "m2/m2_aggregates_qualified.ossie.yaml",
            6,
        ),
        (
            "m3/m3_filtered_agg_bare.ossie.yaml",
            "m3/m3_filtered_agg_qualified.ossie.yaml",
            1,
        ),
    ]
    for bare, qualified, metric_count in pairs:
        report = execute.check(_fixture(bare), _fixture(qualified), all_rewrites=True)
        outcomes = [(f.name, f.outcome) for f in report.findings]
        assert len(outcomes) == metric_count, outcomes
        assert all(outcome == execute.SAME_ON_TEST_DATA for _, outcome in outcomes), (
            outcomes
        )


@requires_engine
def test_rewrite_proved_cosmetic_is_run_only_on_request():
    src = _fixture("m0/m0_sum_bare.ossie.yaml")
    returned = _fixture("m0/m0_ansi_qualified.ossie.yaml")
    assert grade._equivalent_tree("SUM(amount)", "SUM(sales.amount)") is True
    assert execute.check(src, returned).findings == []
    assert len(execute.check(src, returned, all_rewrites=True).findings) == 1


@requires_engine
def test_nullif_on_a_zero_count_agrees_because_division_by_zero_is_null():
    # The check runs with ieee_floating_point_ops = false, so 1 / 0 is NULL and
    # the NULLIF guard changes nothing. Under DuckDB defaults (checked on 1.5.5)
    # 1 / 0 is inf and the two forms would differ where COUNT(DISTINCT ...) is 0;
    # that is the engine, not the meaning (apache/ossie-converters#24).
    fields = [("a", "Decimal"), ("b", "Integer")]
    finding = _run(
        fields,
        "SUM(sales.a) / COUNT(DISTINCT sales.b)",
        "SUM(sales.a) / NULLIF(COUNT(DISTINCT sales.b), 0)",
    )
    assert finding.outcome == execute.SAME_ON_TEST_DATA


@requires_engine
def test_division_by_zero_is_null_and_division_stays_fractional():
    import duckdb

    con = execute.connect(duckdb)
    assert con.execute("SELECT 1 / 0, 7 / 2").fetchone() == (None, 3.5)


@requires_engine
def test_count_star_differs_from_count_column_with_nulls():
    finding = _run([("order_id", "Integer")], "COUNT(*)", "COUNT(sales.order_id)")
    assert finding.outcome == execute.DIFFERENT
    assert finding.rows == [(None,)]
    assert (finding.authored_result, finding.returned_result) == (1, 0)


@requires_engine
def test_calculated_field_runs_through_the_dataset_view():
    src = _fixture("m1/m1_scalar_field.ossie.yaml")
    same = _with_metric(src, "total_amount", "SUM(sales.amount_with_tax) / 1.1")
    assert execute.check(src, same).findings[0].outcome == execute.SAME_ON_TEST_DATA
    changed = _with_metric(src, "total_amount", "SUM(sales.amount_with_tax)")
    assert execute.check(src, changed).findings[0].outcome == execute.DIFFERENT


@pytest.mark.parametrize(
    "fields, authored, returned",
    [
        (
            [("d", "Date")],
            "COUNT(CASE WHEN d >= DATE '2026-06-01' THEN 1 END)",
            "COUNT(CASE WHEN d > '2026-06-01' THEN 1 END)",
        ),
        (
            [("t", "DateTime")],
            "COUNT(CASE WHEN t < '2026-06-01 12:00:00' THEN 1 END)",
            "COUNT(CASE WHEN t <= '2026-06-01 12:00:00' THEN 1 END)",
        ),
        (
            [("x", "Integer")],
            "SUM(x) FILTER (WHERE x >= -5)",
            "SUM(x) FILTER (WHERE x > -5)",
        ),
        (
            [("x", "Decimal")],
            "SUM(x) FILTER (WHERE x > 4.5)",
            "SUM(x) FILTER (WHERE x >= 5)",
        ),
        ([("x", "Decimal")], "ROUND(SUM(x), 3)", "SUM(x)"),
        (
            [("s", "String")],
            "COUNT(CASE WHEN s ILIKE 'completed' THEN 1 END)",
            "COUNT(CASE WHEN s LIKE 'completed' THEN 1 END)",
        ),
        (
            [("s", "String")],
            "COUNT(CASE WHEN TRIM(s) = 'completed' THEN 1 END)",
            "COUNT(CASE WHEN s = 'completed' THEN 1 END)",
        ),
    ],
)
@requires_engine
def test_test_data_reaches_both_sides_of_each_literal(fields, authored, returned):
    assert _run(fields, authored, returned).outcome == execute.DIFFERENT


@requires_engine
def test_non_aggregate_return_is_different_not_a_crash():
    for returned in ("amount", "1"):
        finding = _run(AMOUNT_STATUS, "SUM(amount)", returned)
        assert finding.outcome == execute.DIFFERENT
        assert "returned fails: does not run as an aggregate" in finding.detail


@pytest.mark.parametrize(
    "fields, literal",
    [
        ([("d", "Date")], "DATE '9999-12-31'"),
        ([("d", "Date")], "DATE '0001-01-01'"),
        ([("t", "DateTime")], "'9999-12-31 23:59:59'"),
    ],
)
@requires_engine
def test_literals_at_the_end_of_the_calendar_do_not_crash(fields, literal):
    column = fields[0][0]
    authored = f"COUNT(CASE WHEN {column} < {literal} THEN 1 END)"
    returned = f"COUNT(CASE WHEN {column} <= {literal} THEN 1 END)"
    assert _run(fields, authored, returned).outcome == execute.DIFFERENT


@requires_engine
def test_both_sides_failing_on_the_same_rows_counts_as_agreeing():
    cases = [
        ([("x", "Decimal")], "SUM(LN(x))", "SUM(LN(x)) * 1"),
        ([("x", "Float")], "SUM(SQRT(x))", "SUM(SQRT(x)) + 0"),
        (
            AMOUNT_STATUS,
            "SUM(CAST(status AS INTEGER))",
            "SUM(CAST(status AS INTEGER)) * 1",
        ),
    ]
    for fields, authored, returned in cases:
        finding = _run(fields, authored, returned)
        assert finding.outcome == execute.SAME_ON_TEST_DATA, finding
        assert "both sides fail on" in finding.detail
    finding = _run([("x", "Decimal")], "SUM(LN(x))", "SUM(LN(x)) * 2")
    assert finding.outcome == execute.DIFFERENT


@requires_engine
def test_one_side_that_does_not_bind_is_not_run():
    finding = _run(
        AMOUNT_STATUS, "MAX(COALESCE(status, 'n'))", "MAX(COALESCE(status, 0))"
    )
    assert finding.outcome == execute.NOT_RUN
    assert "cannot bind the returned expression" in finding.detail


@requires_engine
def test_one_side_raising_on_some_rows_is_different():
    finding = _run(AMOUNT_STATUS, "SUM(amount)", "SUM(CAST(status AS INTEGER))")
    assert finding.outcome == execute.DIFFERENT
    assert "returned fails: Conversion Error" in finding.detail
    assert len(finding.rows) == 1


@requires_engine
def test_extra_statements_are_refused_and_not_run(tmp_path):
    target = tmp_path / "out.csv"
    smuggled = f"SUM(amount); COPY test_rows TO '{target.as_posix()}'"
    finding = _run(AMOUNT_STATUS, "SUM(amount)", smuggled)
    assert finding.outcome == execute.NOT_RUN
    assert "not a single expression" in finding.detail
    assert not target.exists()


@requires_engine
def test_subquery_in_a_field_is_refused(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    returned = _sales(AMOUNT_STATUS, "SUM(amount) + 1")
    returned["datasets"][0]["fields"][0]["expression"]["dialects"][0]["expression"] = (
        f"(SELECT LENGTH(content) FROM read_text('{secret.as_posix()}'))"
    )
    (finding,) = execute.check(_sales(AMOUNT_STATUS, "SUM(amount)"), returned).findings
    assert finding.outcome == execute.NOT_RUN
    assert "subquery or statement" in finding.detail


@requires_engine
def test_connections_have_no_file_access(tmp_path):
    import duckdb

    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    con = execute.connect(duckdb)
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_text('{secret.as_posix()}')")
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")


@requires_engine
def test_unsupported_shapes_are_not_run():
    two = _sales(AMOUNT_STATUS, "SUM(sales.amount)")
    two["datasets"].append(copy.deepcopy(two["datasets"][0]))
    two["datasets"][1]["name"] = "returns"
    cases = [
        (
            two,
            _with_metric(two, "m", "SUM(sales.amount) - SUM(returns.amount)"),
            "more than one dataset",
        ),
        (
            _sales(AMOUNT_STATUS, "SUM(amount)"),
            _sales(AMOUNT_STATUS, "SUM(SUM(amount)) OVER ()"),
            "window",
        ),
        (
            _sales(AMOUNT_STATUS, "SUM(amount)"),
            _sales(AMOUNT_STATUS, "(SELECT SUM(amount) FROM sales)"),
            "subquery",
        ),
        (
            _sales(AMOUNT_STATUS, "SUM(amount)"),
            _sales(AMOUNT_STATUS, "SUM(amount"),
            "could not parse",
        ),
    ]
    for src, returned, reason in cases:
        (finding,) = execute.check(src, returned).findings
        assert finding.outcome == execute.NOT_RUN, finding
        assert reason in finding.detail, finding.detail


@requires_engine
def test_vendor_dialect_is_not_run():
    src = _fixture("m0/m0_ansi_qualified.ossie.yaml")
    returned = _with_metric(src, "total_amount", "SUM('Sales'[Amount])", dialect="DAX")
    (finding,) = execute.check(src, returned).findings
    assert finding.outcome == execute.NOT_RUN
    assert "vendor dialect" in finding.detail


@requires_engine
def test_numeric_field_name_and_reserved_column_name_do_not_break_the_check():
    fields = [(2024, "Integer"), ("_test_row", "Integer")]
    finding = _run(fields, 'SUM("2024") + SUM(_test_row)', 'SUM("2024" + _test_row)')
    assert finding.outcome == execute.DIFFERENT


@requires_engine
def test_rejected_document_produces_a_note():
    report = execute.check(
        _sales(AMOUNT_STATUS, "SUM(amount)"), {"version": "0.2.0.dev0", "name": "t"}
    )
    assert report.rejected is True
    assert "REJECTED" in execute._format(report)


@requires_engine
def test_main_json_emits_json_values(tmp_path, capsys):
    src = _with_metric(
        _fixture("m3/m3_filtered_agg_qualified.ossie.yaml"),
        "completed_amount",
        ELSE_ZERO,
    )
    source = tmp_path / "input.json"
    source.write_text(json.dumps(src), encoding="utf-8")
    path = tmp_path / "returned.json"
    path.write_text(
        json.dumps(_with_metric(src, "completed_amount", NO_ELSE)), encoding="utf-8"
    )
    assert (
        execute.main(["--input", str(source), "--returned", str(path), "--json"]) == 0
    )
    output = json.loads(capsys.readouterr().out)
    (finding,) = output["findings"]
    assert finding["outcome"] == execute.DIFFERENT
    assert (finding["authored_result"], finding["returned_result"]) == (0, None)
    assert finding["rows"] == [[1, "other"]]
    assert output["summary"] == {execute.DIFFERENT: 1}


def test_missing_duckdb_is_not_run(monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)
    src = _fixture("m3/m3_filtered_agg_qualified.ossie.yaml")
    returned = _with_metric(src, "completed_amount", "SUM(sales.amount)")
    (finding,) = execute.check(src, returned).findings
    assert finding.outcome == execute.NOT_RUN
    assert "duckdb is not installed" in finding.detail


def test_main_reports_an_unreadable_document_as_a_harness_error(tmp_path, capsys):
    source = os.path.join(PORTABILITY_DIR, "m0", "m0_sum_bare.ossie.yaml")
    missing = tmp_path / "missing.json"
    assert execute.main(["--input", source, "--returned", str(missing)]) == 2
    assert "error:" in capsys.readouterr().err


def test_a_precomputed_grade_is_used_as_given():
    src = _fixture("m0/m0_sum_bare.ossie.yaml")
    returned = _fixture("m0/m0_ansi_qualified.ossie.yaml")
    graded = grade.grade(src, returned)
    graded.findings = []
    assert execute.check(src, returned, all_rewrites=True, graded=graded).findings == []


def test_a_missing_submodule_names_the_fix(tmp_path):
    with pytest.raises(FileNotFoundError, match="git submodule update --init"):
        execute.grader(tmp_path)
