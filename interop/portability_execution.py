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

"""Report-only execution check for the metrics the portability grader marks REWRITTEN.

The core grader (``ossie/compliance/portability/grader/grade.py``, loaded from the
pinned submodule) compares normalized sqlglot trees. When the trees differ it can
only flag the rewrite for review: it cannot tell a benign rewrite from a change in
meaning. This check runs the authored expression and the returned expression in
DuckDB on the same small edge-case data and compares the results:

* SAME_ON_TEST_DATA  equal results on every test group and on an empty table.
                     This is evidence, not proof: other data can still tell the
                     two expressions apart
* DIFFERENT          the results differ, or exactly one side fails. The finding
                     carries a counterexample (the rows of the smallest differing
                     group, shrunk greedily until no single row can be dropped)
                     and the two results on it
* NOT_RUN            the check could not run, with the reason

Scope: metrics in the top-level ``metrics`` list over a single dataset, tagged
ANSI_SQL or OSSIE_SQL_2026. See interop/README.md for how the test data is built.

DuckDB runs with ``ieee_floating_point_ops = false``, so ``x / 0`` is NULL rather
than ``inf``; ``/`` stays floating-point division. A step that drives converters
calls ``check()`` once per round trip, passing the grader's report when it already
has one. ``duckdb`` and ``sqlglot`` are optional: without them every check is
NOT_RUN.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
import importlib.util
import itertools
import json
import math
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
GRADER = Path("ossie/compliance/portability/grader/grade.py")


@cache
def grader(root=ROOT):
    """The core portability grader from the pinned Ossie submodule."""
    path = Path(root) / GRADER
    if not path.is_file():
        raise FileNotFoundError(f"{GRADER} is missing; run git submodule update --init")
    spec = importlib.util.spec_from_file_location("interop_portability_grader", path)
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up in sys.modules while it executes.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


SAME_ON_TEST_DATA = "SAME_ON_TEST_DATA"
DIFFERENT = "DIFFERENT"
NOT_RUN = "NOT_RUN"

# Numeric results match if |a - b| <= TOLERANCE * max(|a|, |b|, 1).
TOLERANCE = 1e-9

# Every connection: no file or network access, no extension loading, and no
# changing that from inside an expression.
LOCKED = {
    "enable_external_access": False,
    "ieee_floating_point_ops": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "lock_configuration": True,
}
# DuckDB 2 raises on x / 0 unless this is off too; DuckDB 1.5 does not know it.
NULL_ON_DIVISION_BY_ZERO = {"error_on_division_by_zero": False}


def connect(duckdb):
    """A locked connection where x / 0 is NULL, on DuckDB 1.5 and 2."""
    try:
        return duckdb.connect(config={**NULL_ON_DIVISION_BY_ZERO, **LOCKED})
    except duckdb.InvalidInputException as exc:
        if "error_on_division_by_zero" not in str(exc):
            raise
    return duckdb.connect(config=LOCKED)


# DuckDB type and default non-NULL test values per Ossie datatype. The first
# value is the ordinary one a column takes while another column is varied.
TEST_VALUES = {
    "String": ("VARCHAR", ["other", ""]),
    "Integer": ("BIGINT", [1, 0, -1, 2]),
    "Decimal": (
        "DECIMAL(18, 6)",
        [Decimal(1), Decimal(0), Decimal(-1), Decimal("2.5"), Decimal("1.234567")],
    ),
    "Float": ("DOUBLE", [1.0, 0.0, -1.0, 2.5, 1.234567]),
    "Boolean": ("BOOLEAN", [True, False]),
    "Date": ("DATE", [date(2026, 1, 1), date(2026, 12, 31)]),
    "Time": ("TIME", [time(0, 0), time(23, 59, 59)]),
    "DateTime": (
        "TIMESTAMP",
        [datetime(2026, 1, 1), datetime(2026, 12, 31, 23, 59, 59)],
    ),
}
# A physical column whose fields carry no datatype is assumed numeric.
DEFAULT_DATATYPE = "Decimal"


class NotRunnable(Exception):
    """The check cannot run for this metric; the message is the reason."""


@dataclass
class Check:
    name: str
    outcome: str
    detail: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    authored_result: object = None
    returned_result: object = None


@dataclass
class _Side:
    """One document's half of a check: its metric and its dataset view."""

    dataset: str
    tag: str
    metric_sql: str
    fields: dict  # field name -> DuckDB SQL over the physical columns
    columns: dict  # physical column -> Ossie datatype (or None)
    numbers: list  # numeric literals in the metric, sign included
    strings: list  # string literals in the metric


def _first_line(exc):
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _portable_text(expression_obj, prefer=None):
    """Return (tag, raw expression) under a portable tag, preferring ``prefer``."""
    dialects = {}
    for entry in (expression_obj or {}).get("dialects") or []:
        if (
            isinstance(entry, dict)
            and entry.get("dialect") in grader().PORTABLE_DIALECTS
        ):
            dialects.setdefault(entry["dialect"], entry.get("expression"))
    if prefer in dialects:
        return prefer, dialects[prefer]
    return next(iter(dialects.items()), (None, None))


def _parse(text, what):
    """Parse exactly one scalar or aggregate expression; refuse anything else."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = sqlglot.parse(str(text))
    except Exception as exc:
        raise NotRunnable(f"could not parse {what}: {_first_line(exc)}") from exc
    if len(trees) != 1 or trees[0] is None:
        raise NotRunnable(f"{what} is not a single expression")
    tree = trees[0]
    if tree.find(exp.Query, exp.DML, exp.DDL, exp.Command, exp.Copy, exp.Block):
        raise NotRunnable(f"{what} contains a subquery or statement")
    if tree.find(exp.Window):
        raise NotRunnable(f"{what} uses a window function")
    return tree


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _strip_qualifier(column):
    for part in ("table", "db", "catalog"):
        column.set(part, None)


def _find_metric(document, name):
    metrics = document.get("metrics") or []
    return next(
        (m for m in metrics if isinstance(m, dict) and m.get("name") == name), None
    )


def _datasets(document):
    """Datasets keyed by lower-cased name: (name, {lower-cased field name: field})."""
    result = {}
    for dataset in document.get("datasets") or []:
        if isinstance(dataset, dict) and dataset.get("name") is not None:
            fields = dataset.get("fields") or []
            by_name = {
                str(f["name"]).lower(): f
                for f in fields
                if isinstance(f, dict) and f.get("name") is not None
            }
            result[str(dataset["name"]).lower()] = (str(dataset["name"]), by_name)
    return result


def _resolve_dataset(tree, datasets):
    """The one dataset a metric reads: by qualifier, or the dataset owning a bare name."""
    from sqlglot import exp

    used = set()
    for column in tree.find_all(exp.Column):
        if column.table:
            used.add(column.table.lower())
            continue
        owners = [
            key
            for key, (_, fields) in datasets.items()
            if column.name.lower() in fields
        ]
        if len(owners) != 1:
            raise NotRunnable(
                f"bare reference {column.name!r} matches {len(owners)} datasets"
            )
        used.add(owners[0])
    if not used and len(datasets) == 1:
        used = set(datasets)
    if len(used) != 1:
        raise NotRunnable(
            "metric spans more than one dataset"
            if used
            else "metric references no dataset"
        )
    key = used.pop()
    if key not in datasets:
        raise NotRunnable(f"unknown dataset {key!r}")
    return datasets[key]


def _build_view(fields):
    """Field name -> DuckDB SQL, and physical column -> datatype. A field that is a
    bare column sets that column's datatype; a calculated field only fills a gap."""
    from sqlglot import exp

    view, columns = {}, {}
    for field_def in fields.values():
        name = str(field_def["name"])
        _, text = _portable_text(field_def.get("expression"))
        if text is None:
            continue
        tree = _parse(text, f"field {name!r}")
        datatype = field_def.get("datatype")
        for column in tree.find_all(exp.Column):
            _strip_qualifier(column)
            key = next(
                (k for k in columns if k.lower() == column.name.lower()), column.name
            )
            if columns.get(key) is None or (datatype and isinstance(tree, exp.Column)):
                columns[key] = datatype or columns.get(key)
        view[name] = tree.sql(dialect="duckdb")
    return view, columns


def _literals(tree):
    from sqlglot import exp

    numbers, strings = [], []
    for literal in tree.find_all(exp.Literal):
        if literal.is_string:
            strings.append(literal.this)
            continue
        try:
            number = Decimal(literal.this)
        except InvalidOperation:
            continue
        numbers.append(-number if isinstance(literal.parent, exp.Neg) else number)
    return numbers, strings


def _prepare(document, metric_name, prefer=None):
    """Resolve one document's metric to a single dataset and build that dataset's view."""
    from sqlglot import exp

    metric = _find_metric(document, metric_name)
    if metric is None:
        raise NotRunnable("not returned under the same name")
    tag, text = _portable_text(metric.get("expression"), prefer)
    if text is None:
        raise NotRunnable("vendor dialect: no ANSI_SQL or OSSIE_SQL_2026 expression")
    tree = _parse(text, f"metric {metric_name!r}")
    dataset_name, fields = _resolve_dataset(tree, _datasets(document))
    view, columns = _build_view(fields)
    known = {name.lower() for name in view}
    for column in tree.find_all(exp.Column):
        if column.name.lower() not in known:
            raise NotRunnable(
                f"{column.name!r} is not a field of {dataset_name!r} with a portable expression"
            )
        _strip_qualifier(column)
    return _Side(
        dataset_name, tag, tree.sql(dialect="duckdb"), view, columns, *_literals(tree)
    )


def _near_numbers(numbers):
    """Each literal n gives n - 1, n and n + 1, plus the midpoint of each pair of
    neighbouring literals, so both sides of every comparison are covered."""
    ordered = sorted(set(numbers))
    values = [n + step for n in ordered for step in (-1, 0, 1)]
    return values + [(a + b) / 2 for a, b in itertools.pairwise(ordered)]


def _near_moments(datatype, text):
    """A date, time or timestamp literal gives itself and one day (Date) or one
    second (Time, DateTime) either side. Text that is not ISO 8601 gives nothing,
    and a neighbour past the end of the calendar is skipped."""
    try:
        if datatype == "Time":
            moment = datetime.combine(date(2026, 1, 1), time.fromisoformat(text))
        else:
            moment = datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return []
    step = timedelta(days=1) if datatype == "Date" else timedelta(seconds=1)
    moments = [moment]
    for neighbour in (lambda: moment - step, lambda: moment + step):
        try:
            moments.append(neighbour())
        except OverflowError:
            pass
    if datatype == "Date":
        return [m.date() for m in moments]
    return [m.time() for m in moments] if datatype == "Time" else moments


def _fit(datatype, number):
    """``number`` as a value of the column type, or None if it does not fit."""
    if datatype == "Float":
        return float(number)
    if datatype == "Integer":
        return (
            int(number)
            if number == number.to_integral_value() and abs(number) < 2**63
            else None
        )
    if abs(number) < 10**12 and number == number.quantize(Decimal("0.000001")):
        return number
    return None


def _test_values(datatype, numbers, strings):
    """Non-NULL test values for one column: the defaults plus values near the
    literals of both expressions. String literals go first, so a condition like
    ``status = 'completed'`` holds on the ordinary rows; each also adds an
    upper-cased and a space-padded variant at the end."""
    if datatype not in TEST_VALUES:
        raise NotRunnable(f"datatype {datatype} is not covered by the test data")
    values = list(TEST_VALUES[datatype][1])
    if datatype == "String":
        values = (
            strings
            + values
            + [variant for s in strings for variant in (s.upper(), f" {s} ")]
        )
    elif datatype in ("Integer", "Decimal", "Float"):
        values += [_fit(datatype, number) for number in _near_numbers(numbers)]
    elif datatype in ("Date", "Time", "DateTime"):
        values += [moment for s in strings for moment in _near_moments(datatype, s)]
    return list(dict.fromkeys(value for value in values if value is not None))


def _test_groups(pools):
    """Deterministic test groups, each a list of row tuples in column order.

    * for every column and every one of its values (NULL included), two identical
      rows with that value and the ordinary value elsewhere: covers NULLs, zeros,
      negatives, fractions, duplicates, and a filter that matches no row
    * one mixed group that cycles every column through all its values
    * one group of two all-NULL rows
    """
    names = list(pools)
    ordinary = [pools[name][0] for name in names]
    groups = []
    for index, name in enumerate(names):
        for value in pools[name] + [None]:
            row = tuple(value if i == index else ordinary[i] for i in range(len(names)))
            groups.append([row, row])
    cycles = [pools[name] + [None] for name in names]
    width = max(len(cycle) for cycle in cycles)
    groups.append(
        [
            tuple(cycle[(i + j) % len(cycle)] for j, cycle in enumerate(cycles))
            for i in range(width)
        ]
    )
    groups.append([tuple(None for _ in names)] * 2)
    return groups


def _is_number(value):
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _same(left, right):
    """Two NULLs match. Two numbers (int, Decimal, float; not bool) match within
    TOLERANCE, two NaNs match and infinities must be equal. Any other two values
    match only if they have the same type and are equal, so TRUE is not 1 and a
    date is not the timestamp at its midnight."""
    if left is None or right is None:
        return left is None and right is None
    if not (_is_number(left) and _is_number(right)):
        return type(left) is type(right) and left == right
    left, right = float(left), float(right)
    if math.isnan(left) or math.isnan(right):
        return math.isnan(left) and math.isnan(right)
    if math.isinf(left) or math.isinf(right):
        return left == right
    return abs(left - right) <= TOLERANCE * max(abs(left), abs(right), 1.0)


def _agree(pair):
    """True if both sides give the same result, False if they differ or exactly
    one side fails, None if both fail (which counts as agreeing, like two NULLs)."""
    (left, left_error), (right, right_error) = pair
    if left_error or right_error:
        return None if left_error and right_error else False
    return _same(left, right)


def _show(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


class _Runner:
    """One locked DuckDB connection holding every test row and the two views."""

    def __init__(self, duckdb, columns, rows, authored, returned):
        self.duckdb = duckdb
        self.con = connect(duckdb)
        self.sides = {"authored_view": authored, "returned_view": returned}
        taken = {
            name.lower() for name in [*columns, *authored.fields, *returned.fields]
        }
        self.row_id = "_test_row"
        while self.row_id in taken:
            self.row_id = "_" + self.row_id
        definitions = [f"{self.row_id} INTEGER"] + [
            f"{_quote(c)} {TEST_VALUES[t][0]}" for c, t in columns.items()
        ]
        self.con.execute(f"CREATE TABLE test_rows ({', '.join(definitions)})")
        marks = ", ".join("?" for _ in definitions)
        self.con.executemany(
            f"INSERT INTO test_rows VALUES ({marks})",
            [(i, *row) for i, row in enumerate(rows)],
        )
        for view, side in self.sides.items():
            selected = [self.row_id] + [
                f"{sql} AS {_quote(name)}" for name, sql in side.fields.items()
            ]
            self.con.execute(
                f"CREATE VIEW {view} AS SELECT {', '.join(selected)} FROM test_rows"
            )

    def _where(self, row_ids):
        return (
            f"{self.row_id} IN ({', '.join(map(str, row_ids))})" if row_ids else "FALSE"
        )

    def result(self, view, row_ids):
        """(value, None) for one aggregate over ``row_ids`` (none: an empty table),
        or (None, error) if it fails or does not return exactly one row."""
        query = f"SELECT {self.sides[view].metric_sql} FROM {view} WHERE {self._where(row_ids)}"
        try:
            rows = self.con.execute(query).fetchall()
        except (
            self.duckdb.BinderException,
            self.duckdb.CatalogException,
            self.duckdb.ParserException,
        ) as exc:
            side = view.split("_")[0]
            raise NotRunnable(
                f"DuckDB cannot bind the {side} expression: {_first_line(exc)}"
            ) from exc
        except self.duckdb.Error as exc:
            return None, _first_line(exc)
        if len(rows) != 1:
            return None, f"does not run as an aggregate ({len(rows)} result rows)"
        return rows[0][0], None

    def pair(self, row_ids):
        return self.result("authored_view", row_ids), self.result(
            "returned_view", row_ids
        )

    def stored(self, row_ids):
        """The rows as DuckDB holds them, so a counterexample shows what was run."""
        query = f"SELECT * EXCLUDE ({self.row_id}) FROM test_rows WHERE {self._where(row_ids)} ORDER BY {self.row_id}"
        return self.con.execute(query).fetchall()


def _shrink(runner, row_ids):
    """Greedily drop one row at a time while the two sides still disagree."""
    shrinking = True
    while shrinking:
        shrinking = False
        for index in range(len(row_ids)):
            candidate = row_ids[:index] + row_ids[index + 1 :]
            if _agree(runner.pair(candidate)) is False:
                row_ids, shrinking = candidate, True
                break
    return row_ids


def _describe(pair, row_ids):
    (left, left_error), (right, right_error) = pair
    where = {0: "on an empty table", 1: "on 1 row"}.get(
        len(row_ids), f"on {len(row_ids)} rows"
    )
    if left_error:
        return f"authored fails: {left_error}; returned gives {_show(right)} {where}"
    if right_error:
        return f"returned fails: {right_error}; authored gives {_show(left)} {where}"
    return f"authored gives {_show(left)}, returned gives {_show(right)} {where}"


def _one_pass(name, runner, columns, groups):
    """The verdict: the empty table first, then each group. NOT_RUN only if both
    sides fail everywhere."""
    row_ids = []
    pair = runner.pair(row_ids)
    if _agree(pair) is not False:
        verdicts = [_agree(runner.pair(group)) for group in groups]
        differing = [
            group for group, verdict in zip(groups, verdicts) if verdict is False
        ]
        failing = verdicts.count(None)
        if not differing and failing == len(groups) and _agree(pair) is None:
            raise NotRunnable(f"both sides fail on all test data: {pair[0][1]}")
        if not differing:
            note = f"; both sides fail on {failing} of the groups" if failing else ""
            return Check(
                name,
                SAME_ON_TEST_DATA,
                f"same result on {len(groups)} test groups and an empty table{note}",
            )
        row_ids = _shrink(runner, min(differing, key=len))
        pair = runner.pair(row_ids)
    (left, _), (right, _) = pair
    return Check(
        name,
        DIFFERENT,
        _describe(pair, row_ids),
        list(columns),
        runner.stored(row_ids),
        left,
        right,
    )


def _run(name, duckdb, authored, returned):
    """Build the test data and run one check."""
    columns = dict(authored.columns)
    for column, datatype in returned.columns.items():
        key = next((k for k in columns if k.lower() == column.lower()), column)
        columns[key] = columns.get(key) or datatype
    columns = {
        column: datatype or DEFAULT_DATATYPE for column, datatype in columns.items()
    }
    numbers, strings = (
        authored.numbers + returned.numbers,
        authored.strings + returned.strings,
    )
    pools = {
        column: _test_values(datatype, numbers, strings)
        for column, datatype in columns.items()
    }
    if not pools:
        raise NotRunnable("the dataset has no physical columns")
    groups, rows = [], []
    for group in _test_groups(pools):
        groups.append(list(range(len(rows), len(rows) + len(group))))
        rows.extend(group)

    runner = _Runner(duckdb, columns, rows, authored, returned)
    try:
        return _one_pass(name, runner, columns, groups)
    finally:
        runner.con.close()


def _check_metric(name, input_document, returned_document):
    try:
        import duckdb
        import sqlglot  # noqa: F401
    except ImportError as exc:
        return Check(name, NOT_RUN, f"{exc.name or 'a dependency'} is not installed")
    try:
        authored = _prepare(input_document, name)
        returned = _prepare(returned_document, name, prefer=authored.tag)
        if authored.dataset.lower() != returned.dataset.lower():
            raise NotRunnable(
                f"authored over {authored.dataset!r}, returned over {returned.dataset!r}"
            )
        return _run(name, duckdb, authored, returned)
    except NotRunnable as exc:
        return Check(name, NOT_RUN, str(exc))
    except duckdb.Error as exc:
        return Check(
            name, NOT_RUN, f"DuckDB could not set up the test data: {_first_line(exc)}"
        )


def _proved_cosmetic(name, input_document, returned_document):
    """True when sqlglot proves the two expressions equal, so running them adds nothing."""
    authored = _find_metric(input_document, name)
    returned = _find_metric(returned_document, name)
    if authored is None or returned is None:
        return (
            True  # paired by the grader under another name, by an equivalent expression
        )
    tag, authored_text = _portable_text(authored.get("expression"))
    _, returned_text = _portable_text(returned.get("expression"), tag)
    if authored_text is None or returned_text is None:
        return False
    return grader()._equivalent_tree(str(authored_text), str(returned_text)) is True


def check(
    input_document, returned_document, all_rewrites=False, converter="", graded=None
):
    """Execute the metrics the grader marks REWRITTEN, skipping those sqlglot proves
    equal unless ``all_rewrites``. A metric returned in a vendor dialect only is
    listed as NOT_RUN so the gap stays visible.

    ``graded`` is the grader's report for this same pair of documents. A step that
    has already graded the round trip passes it, so both read the same findings;
    without it the pair is graded here."""
    if graded is None:
        graded = grader().grade(input_document, returned_document, converter=converter)
    report = grader().Report(converter=converter, rejected=graded.rejected)
    for finding in graded.findings:
        if finding.kind != "metric":
            continue
        if finding.outcome == grader().SURVIVED_VENDOR_ONLY:
            report.findings.append(
                Check(finding.name, NOT_RUN, f"vendor dialect: {finding.detail}")
            )
        elif finding.outcome == grader().REWRITTEN and (
            all_rewrites
            or not _proved_cosmetic(finding.name, input_document, returned_document)
        ):
            report.findings.append(
                _check_metric(finding.name, input_document, returned_document)
            )
    return report


def _format(report):
    lines = [
        f"converter: {report.converter}" if report.converter else "execution check"
    ]
    if report.rejected:
        lines.append(
            "  note: the returned document has no elements (REJECTED); nothing to execute"
        )
    for finding in report.findings:
        detail = f"  -- {finding.detail}" if finding.detail else ""
        lines.append(f"  [{finding.outcome}] metric {finding.name}{detail}")
        if finding.rows:
            table = [list(finding.columns)] + [
                [_show(value) for value in row] for row in finding.rows
            ]
            widths = [
                max(len(line[i]) for line in table) for i in range(len(finding.columns))
            ]
            for line in table:
                lines.append(
                    "      "
                    + " | ".join(
                        cell.ljust(width) for cell, width in zip(line, widths)
                    ).rstrip()
                )
    tally = report.counts()
    lines.append(
        "summary: "
        + ", ".join(f"{name}={count}" for name, count in sorted(tally.items()))
    )
    return "\n".join(lines)


def _json_value(value):
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSON has no inf or NaN
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


def _as_json(report):
    findings = []
    for finding in report.findings:
        entry = vars(finding).copy()
        entry["rows"] = [[_json_value(value) for value in row] for row in finding.rows]
        entry["authored_result"] = _json_value(finding.authored_result)
        entry["returned_result"] = _json_value(finding.returned_result)
        findings.append(entry)
    summary = {
        "converter": report.converter,
        "rejected": report.rejected,
        "findings": findings,
    }
    return json.dumps({**summary, "summary": report.counts()}, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m interop.portability_execution",
        description="Execute the metrics the portability grader marks REWRITTEN, on edge-case data. "
        "Report-only: exits 0 whatever the findings, and 2 when the documents or the grader cannot be loaded.",
    )
    parser.add_argument(
        "--input", required=True, help="the input .ossie.yaml/.json document"
    )
    parser.add_argument(
        "--returned", required=True, help="the document the converter returned"
    )
    parser.add_argument(
        "--all-rewrites",
        action="store_true",
        help="also run rewrites sqlglot proves equal",
    )
    parser.add_argument(
        "--converter", default="", help="converter name, for the report header"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    try:
        input_document = grader()._load(args.input)
        returned_document = grader()._load(args.returned)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    report = check(
        input_document,
        returned_document,
        all_rewrites=args.all_rewrites,
        converter=args.converter,
    )
    print(_as_json(report) if args.json else _format(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
