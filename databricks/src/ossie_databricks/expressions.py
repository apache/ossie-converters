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

"""SQL expressions in OSSIE_SQL_2026 and Databricks SQL.

sqlglot parses and prints both languages. Its printer also rewrites some functions into forms
with another meaning: numeric `TRUNC(x, 2)` becomes a cast to BIGINT for Databricks, and
`PERCENTILE_CONT` becomes `PERCENTILE_APPROX`. So `translate` prints only trees whose every
function is in `_PORTABLE`, a set that the table test in `tests/test_expressions.py` covers row
by row. For any other tree it raises `Untranslatable`, and the caller keeps the original text.
"""

import sqlglot
from sqlglot import exp
from sqlglot.dialects.databricks import Databricks
from sqlglot.errors import ParseError, TokenError

from ossie_databricks import _ossie

OSSIE = "OSSIE_SQL_2026"
DATABRICKS = "DATABRICKS"


class Untranslatable(Exception):
    """The expression has no verified translation."""


_PORTABLE: frozenset[type[exp.Expression]] = frozenset(
    {
        # Aggregates.
        exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max, exp.Stddev, exp.StddevSamp,
        exp.StddevPop, exp.Variance, exp.VariancePop, exp.Median, exp.ApproxDistinct, exp.Agg,
        # Conditionals and casts.
        exp.Case, exp.If, exp.Coalesce, exp.Nullif, exp.Nvl2, exp.Cast, exp.TryCast,
        # Strings.
        exp.Concat, exp.Length, exp.Lower, exp.Upper, exp.Left, exp.Right, exp.Substring,
        exp.Replace, exp.SplitPart, exp.StrPosition, exp.Contains, exp.StartsWith, exp.EndsWith,
        exp.RegexpExtract,
        # Numbers.
        exp.Abs, exp.Round, exp.Floor, exp.Ceil, exp.Sign, exp.Pow, exp.Sqrt, exp.Exp, exp.Ln,
        exp.Log, exp.Sin, exp.Cos, exp.Tan, exp.Asin, exp.Acos, exp.Atan, exp.Atan2,
        exp.Radians, exp.Degrees, exp.Pi, exp.Trunc,
        # Dates and times.
        exp.Year, exp.Quarter, exp.Month, exp.Day, exp.DayOfYear, exp.Hour, exp.Minute,
        exp.Second, exp.Extract, exp.DateTrunc, exp.TimestampTrunc, exp.TimestampAdd,
        exp.TsOrDsAdd, exp.TsOrDsToDate, exp.CurrentDate, exp.CurrentTimestamp, exp.ToChar,
        # Window functions.
        exp.RowNumber, exp.Rank, exp.DenseRank, exp.Ntile, exp.PercentRank, exp.CumeDist,
        exp.Lag, exp.Lead, exp.FirstValue, exp.LastValue, exp.NthValue,
    }
)  # fmt: skip

# Calls that sqlglot keeps as plain function calls, spelled the same in both languages.
_PORTABLE_CALLS = frozenset(
    {"MEASURE", "DATE_PART", "IFF", "ZEROIFNULL", "NULLIFZERO", "TO_DATE", "TO_TIMESTAMP"}
)

# Ossie datetime format tokens and their Databricks patterns, longest token first.
# EXPERIMENTAL in the Ossie specification; Databricks to Ossie is not translated.
_FORMAT_TOKENS = (
    ("MONTH", "MMMM"), ("HH24", "HH"), ("HH12", "hh"), ("YYYY", "yyyy"), ("MON", "MMM"),
    ("DAY", "EEEE"), ("YY", "yy"), ("MM", "MM"), ("DD", "dd"), ("DY", "EEE"), ("HH", "hh"),
    ("MI", "mm"), ("SS", "ss"), ("AM", "a"), ("PM", "a"),
)  # fmt: skip
_FORMAT_LITERALS = frozenset(" -/:.,")


def _date_add(self: sqlglot.Generator, node: exp.Expression) -> str:
    unit = exp.var(node.text("unit").upper() or "DAY")
    return self.func("DATEADD", unit, node.expression, node.this)


def _date_trunc(self: sqlglot.Generator, node: exp.Expression) -> str:
    return self.func("DATE_TRUNC", exp.Literal.string(node.text("unit").upper()), node.this)


class _DatabricksSql(Databricks):
    class Generator(Databricks.Generator):
        TRANSFORMS = {
            **Databricks.Generator.TRANSFORMS,
            exp.DateTrunc: _date_trunc,
            exp.TimestampAdd: _date_add,
            exp.TsOrDsAdd: _date_add,
            # A bare CURRENT_DATE resolves to a column of that name when one exists.
            exp.CurrentDate: lambda self, node: "CURRENT_DATE()",
        }


class _OssieSql(Databricks):
    """Databricks spelling, which Ossie shares for its functions, with ANSI quoting."""

    class Tokenizer(Databricks.Tokenizer):
        IDENTIFIERS = ['"']
        QUOTES = ["'"]
        STRING_ESCAPES = ["'"]

    class Generator(Databricks.Generator):
        TRANSFORMS = {
            **Databricks.Generator.TRANSFORMS,
            exp.DateTrunc: _date_trunc,
            exp.TimestampTrunc: _date_trunc,
            exp.TimestampAdd: _date_add,
            exp.TsOrDsAdd: _date_add,
            exp.CurrentDate: lambda self, node: "CURRENT_DATE",
            exp.StrPosition: lambda self, node: (
                f"POSITION({self.sql(node, 'substr')} IN {self.sql(node, 'this')})"
            ),
        }


def parse(text: str, dialect: str) -> exp.Expression:
    """Parse `text` as the Ossie validator does for `dialect`."""
    try:
        tree = sqlglot.parse_one(text, read=_ossie.sqlglot_dialect(dialect))
    except (ParseError, TokenError, KeyError) as error:
        raise Untranslatable(f"cannot parse {text!r} as {dialect}") from error
    if tree is None:
        raise Untranslatable(f"cannot parse an empty expression as {dialect}")
    return tree.transform(_normalize_dateadd)


def _normalize_dateadd(node: exp.Expression) -> exp.Expression:
    # The default dialect reads the unit of DATEADD(day, 3, x) as a column named `day`.
    if _call_name(node) == "DATEADD" and len(node.expressions) == 3:
        unit, amount, value = node.expressions
        return exp.TimestampAdd(this=value, expression=amount, unit=exp.var(unit.name.upper()))
    return node


def _call_name(node: exp.Expression) -> str | None:
    return node.name.upper() if isinstance(node, exp.Anonymous) else None


def _unsupported_functions(tree: exp.Expression) -> list[str]:
    return sorted(
        {
            _call_name(node) or node.sql_name()
            for node in tree.find_all(exp.Func)
            # sqlglot models AND and OR as functions.
            if not isinstance(node, exp.Connector)
            and type(node) not in _PORTABLE
            and _call_name(node) not in _PORTABLE_CALLS
        }
    )


def translate(tree: exp.Expression, target: str) -> str:
    """Print `tree` in `target`, OSSIE_SQL_2026 or DATABRICKS."""
    unsupported = _unsupported_functions(tree)
    if unsupported:
        raise Untranslatable(f"no verified translation for {', '.join(unsupported)}")
    if target == DATABRICKS:
        return tree.copy().transform(_to_databricks).sql(dialect=_DatabricksSql)
    return tree.copy().transform(_to_ossie).sql(dialect=_OssieSql)


def _to_databricks(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Agg):
        return exp.Anonymous(this="MEASURE", expressions=[node.this])
    if isinstance(node, exp.Trunc):
        # Databricks TRUNC truncates dates; numbers truncate toward zero through FLOOR/CEIL.
        value, digits = node.this, node.args.get("decimals") or exp.Literal.number(0)
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.LT(this=value.copy(), expression=exp.Literal.number(0)),
                    true=exp.Ceil(this=value.copy(), decimals=digits.copy()),
                )
            ],
            default=exp.Floor(this=value.copy(), decimals=digits.copy()),
        )
    if isinstance(node, exp.Extract) and node.name.upper() == "DAYOFYEAR":
        return exp.Extract(this=exp.var("DOY"), expression=node.expression)
    if isinstance(node, exp.RegexpExtract) and node.args.get("group") is None:
        # Ossie returns the whole match; Databricks defaults to group 1.
        return exp.RegexpExtract(
            this=node.this, expression=node.expression, group=exp.Literal.number(0)
        )
    if isinstance(node, exp.ToChar):
        pattern = _databricks_format(node.args["format"])
        return exp.Anonymous(this="DATE_FORMAT", expressions=[node.this, pattern])
    if _call_name(node) in ("TO_DATE", "TO_TIMESTAMP") and len(node.expressions) == 2:
        value, pattern = node.expressions
        return exp.Anonymous(this=node.name, expressions=[value, _databricks_format(pattern)])
    return node


def _to_ossie(node: exp.Expression) -> exp.Expression:
    if _call_name(node) == "MEASURE":
        return exp.Agg(this=node.expressions[0])
    if isinstance(node, exp.Extract) and node.name.upper() == "DOY":
        return exp.Extract(this=exp.var("DAYOFYEAR"), expression=node.expression)
    if isinstance(node, exp.RegexpExtract):
        group = node.args.get("group")
        if group is None or group.name != "0":
            raise Untranslatable("REGEXP_EXTRACT with a capture group has no Ossie form")
        return exp.RegexpExtract(this=node.this, expression=node.expression)
    if isinstance(node, exp.TsOrDsToDate):
        if node.args.get("format"):
            raise Untranslatable("Databricks datetime patterns are not translated to Ossie")
        return exp.Anonymous(this="TO_DATE", expressions=[node.this])
    return node


def _databricks_format(pattern: exp.Expression) -> exp.Expression:
    if not (isinstance(pattern, exp.Literal) and pattern.is_string):
        raise Untranslatable("a datetime format must be a string literal")
    text, position, out = pattern.name, 0, []
    while position < len(text):
        if text[position] in _FORMAT_LITERALS:
            out.append(text[position])
            position += 1
            continue
        token = next(
            (pair for pair in _FORMAT_TOKENS if text.upper().startswith(pair[0], position)), None
        )
        if token is None:
            raise Untranslatable(f"unknown datetime format text {text[position:]!r}")
        out.append(token[1])
        position += len(token[0])
    return exp.Literal.string("".join(out))


def columns(tree: exp.Expression) -> list[exp.Column]:
    return list(tree.find_all(exp.Column))


def measure_names(tree: exp.Expression) -> set[str]:
    """Names inside `MEASURE(...)` and `AGG(...)`."""
    names = set()
    for node in tree.find_all(exp.Anonymous, exp.Agg):
        if isinstance(node, exp.Agg) or _call_name(node) == "MEASURE":
            argument = node.this if isinstance(node, exp.Agg) else node.expressions[0]
            names.add(argument.name)
    return names


def has_window(tree: exp.Expression) -> bool:
    return tree.find(exp.Window) is not None


def measure_reference(name: str, target: str) -> str:
    """`MEASURE(source.<name>)` in Databricks, `AGG(<name>)` in Ossie."""
    if target == DATABRICKS:
        call = exp.Anonymous(this="MEASURE", expressions=[exp.column(name, "source")])
        return call.sql(dialect=_DatabricksSql)
    return exp.Agg(this=exp.column(name)).sql(dialect=_OssieSql)


def identifier(name: str, target: str) -> str:
    dialect = _DatabricksSql if target == DATABRICKS else _OssieSql
    return exp.column(name).sql(dialect=dialect)
