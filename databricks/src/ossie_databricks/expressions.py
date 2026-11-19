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

import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.databricks import Databricks
from sqlglot.errors import ParseError, TokenError

from ossie_databricks import _ossie

OSSIE = "OSSIE_SQL_2026"
DATABRICKS = "DATABRICKS"

# A name that may be written without quotes. A Databricks backtick name is case-insensitive,
# so its quote is dropped when Databricks text is read: a double-quoted Ossie name would match
# case-sensitively and stop matching the column. A bare reserved word reads as a keyword, or
# as a call such as CURRENT_ROLE, so the Ossie printer quotes it in its normalized (upper
# case) form, which Ossie matches like the bare name. Quoting more names than needed only
# changes the spelling, so the set also holds every word sqlglot reads as a keyword, which the
# Ossie validator could not parse bare.
_REGULAR_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SQL_2016_RESERVED = frozenset(
    {
        "ABS", "ACOS", "ALL", "ALLOCATE", "ALTER", "AND", "ANY", "ARE", "ARRAY", "ARRAY_AGG",
        "ARRAY_MAX_CARDINALITY", "AS", "ASENSITIVE", "ASIN", "ASYMMETRIC", "AT", "ATAN",
        "ATOMIC", "AUTHORIZATION", "AVG", "BEGIN", "BEGIN_FRAME", "BEGIN_PARTITION", "BETWEEN",
        "BIGINT", "BINARY", "BLOB", "BOOLEAN", "BOTH", "BY", "CALL", "CALLED", "CARDINALITY",
        "CASCADED", "CASE", "CAST", "CEIL", "CEILING", "CHAR", "CHARACTER", "CHARACTER_LENGTH",
        "CHAR_LENGTH", "CHECK", "CLASSIFIER", "CLOB", "CLOSE", "COALESCE", "COLLATE",
        "COLLECT", "COLUMN", "COMMIT", "CONDITION", "CONNECT", "CONSTRAINT", "CONTAINS",
        "CONVERT", "COPY", "CORR", "CORRESPONDING", "COS", "COSH", "COUNT", "COVAR_POP",
        "COVAR_SAMP", "CREATE", "CROSS", "CUBE", "CUME_DIST", "CURRENT", "CURRENT_CATALOG",
        "CURRENT_DATE", "CURRENT_DEFAULT_TRANSFORM_GROUP", "CURRENT_PATH", "CURRENT_ROLE",
        "CURRENT_ROW", "CURRENT_SCHEMA", "CURRENT_TIME", "CURRENT_TIMESTAMP",
        "CURRENT_TRANSFORM_GROUP_FOR_TYPE", "CURRENT_USER", "CURSOR", "CYCLE", "DATE", "DAY",
        "DEALLOCATE", "DEC", "DECFLOAT", "DECIMAL", "DECLARE", "DEFAULT", "DEFINE", "DELETE",
        "DENSE_RANK", "DEREF", "DESCRIBE", "DETERMINISTIC", "DISCONNECT", "DISTINCT", "DOUBLE",
        "DROP", "DYNAMIC", "EACH", "ELEMENT", "ELSE", "EMPTY", "END", "END_FRAME",
        "END_PARTITION", "EQUALS", "ESCAPE", "EVERY", "EXCEPT", "EXEC", "EXECUTE", "EXISTS",
        "EXP", "EXTERNAL", "EXTRACT", "FALSE", "FETCH", "FILTER", "FIRST_VALUE", "FLOAT",
        "FLOOR", "FOR", "FOREIGN", "FRAME_ROW", "FREE", "FROM", "FULL", "FUNCTION", "FUSION",
        "GET", "GLOBAL", "GRANT", "GROUP", "GROUPING", "GROUPS", "HAVING", "HOLD", "HOUR",
        "IDENTITY", "IN", "INDICATOR", "INITIAL", "INNER", "INOUT", "INSENSITIVE", "INSERT",
        "INT", "INTEGER", "INTERSECT", "INTERSECTION", "INTERVAL", "INTO", "IS", "JOIN",
        "JSON_ARRAY", "JSON_ARRAYAGG", "JSON_EXISTS", "JSON_OBJECT", "JSON_OBJECTAGG",
        "JSON_QUERY", "JSON_TABLE", "JSON_TABLE_PRIMITIVE", "JSON_VALUE", "LAG", "LANGUAGE",
        "LARGE", "LAST_VALUE", "LATERAL", "LEAD", "LEADING", "LEFT", "LIKE", "LIKE_REGEX",
        "LISTAGG", "LN", "LOCAL", "LOCALTIME", "LOCALTIMESTAMP", "LOG", "LOG10", "LOWER",
        "MATCH", "MATCHES", "MATCH_NUMBER", "MATCH_RECOGNIZE", "MAX", "MEMBER", "MERGE",
        "METHOD", "MIN", "MINUTE", "MOD", "MODIFIES", "MODULE", "MONTH", "MULTISET",
        "NATIONAL", "NATURAL", "NCHAR", "NCLOB", "NEW", "NO", "NONE", "NORMALIZE", "NOT",
        "NTH_VALUE", "NTILE", "NULL", "NULLIF", "NUMERIC", "OCCURRENCES_REGEX", "OCTET_LENGTH",
        "OF", "OFFSET", "OLD", "OMIT", "ON", "ONE", "ONLY", "OPEN", "OR", "ORDER", "OUT",
        "OUTER", "OVER", "OVERLAPS", "OVERLAY", "PARAMETER", "PARTITION", "PATTERN", "PER",
        "PERCENT", "PERCENTILE_CONT", "PERCENTILE_DISC", "PERCENT_RANK", "PERIOD", "PORTION",
        "POSITION", "POSITION_REGEX", "POWER", "PRECEDES", "PRECISION", "PREPARE", "PRIMARY",
        "PROCEDURE", "PTF", "RANGE", "RANK", "READS", "REAL", "RECURSIVE", "REF", "REFERENCES",
        "REFERENCING", "REGR_AVGX", "REGR_AVGY", "REGR_COUNT", "REGR_INTERCEPT", "REGR_R2",
        "REGR_SLOPE", "REGR_SXX", "REGR_SXY", "REGR_SYY", "RELEASE", "RESULT", "RETURN",
        "RETURNS", "REVOKE", "RIGHT", "ROLLBACK", "ROLLUP", "ROW", "ROWS", "ROW_NUMBER",
        "RUNNING", "SAVEPOINT", "SCOPE", "SCROLL", "SEARCH", "SECOND", "SEEK", "SELECT",
        "SENSITIVE", "SESSION_USER", "SET", "SHOW", "SIMILAR", "SIN", "SINH", "SKIP",
        "SMALLINT", "SOME", "SPECIFIC", "SPECIFICTYPE", "SQL", "SQLEXCEPTION", "SQLSTATE",
        "SQLWARNING", "SQRT", "START", "STATIC", "STDDEV_POP", "STDDEV_SAMP", "SUBMULTISET",
        "SUBSET", "SUBSTRING", "SUBSTRING_REGEX", "SUCCEEDS", "SUM", "SYMMETRIC", "SYSTEM",
        "SYSTEM_TIME", "SYSTEM_USER", "TABLE", "TABLESAMPLE", "TAN", "TANH", "THEN", "TIME",
        "TIMESTAMP", "TIMEZONE_HOUR", "TIMEZONE_MINUTE", "TO", "TRAILING", "TRANSLATE",
        "TRANSLATE_REGEX", "TRANSLATION", "TREAT", "TRIGGER", "TRIM", "TRIM_ARRAY", "TRUE",
        "TRUNCATE", "UESCAPE", "UNION", "UNIQUE", "UNKNOWN", "UNNEST", "UPDATE", "UPPER",
        "USER", "USING", "VALUE", "VALUES", "VALUE_OF", "VARBINARY", "VARCHAR", "VARYING",
        "VAR_POP", "VAR_SAMP", "VERSIONING", "WHEN", "WHENEVER", "WHERE", "WIDTH_BUCKET",
        "WINDOW", "WITH", "WITHIN", "WITHOUT", "YEAR",
    }
)  # fmt: skip
_RESERVED = (
    _SQL_2016_RESERVED
    # Reserved in Databricks SQL only.
    | {"ANTI", "ASC", "DESC", "LIMIT", "SEMI"}
    # sqlglot's keyword table also holds multi-word phrases ("ORDER BY"), which no name matches.
    | {keyword.upper() for keyword in Databricks.Tokenizer.KEYWORDS if " " not in keyword}
)


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

# Operators with the same meaning in both languages. An operator outside this set (a
# Databricks bitwise `^`/`&`/`|`/`~`/`<<`/`>>`, which other engines read as exponentiation
# or something else) has no verified Ossie form and is left untranslated with a warning.
_PORTABLE_OPERATORS: frozenset[type[exp.Expression]] = frozenset(
    {
        exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Paren,
        exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ,
        exp.And, exp.Or, exp.Not, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.Escape,
        exp.DPipe,
    }
)  # fmt: skip

# The other nodes a translated tree may hold. Anything else, such as an array subscript `x[0]`
# (Databricks counts from 0) or a subquery, which Ossie does not allow, is left untranslated.
_PORTABLE_SYNTAX: frozenset[type[exp.Expression]] = frozenset(
    {
        exp.Column, exp.Identifier, exp.Star, exp.Literal, exp.Boolean, exp.Null, exp.Var,
        exp.DataType, exp.DataTypeParam, exp.Distinct, exp.Filter, exp.Where, exp.Window,
        exp.WindowSpec, exp.Order, exp.Ordered,
    }
)  # fmt: skip

# Databricks integer division `x DIV y` has an Ossie form, TRUNC(x / y, 0), but DIV is not an
# Ossie operator, so it is translated only from Databricks.
_FROM_DATABRICKS_ONLY: frozenset[type[exp.Expression]] = frozenset({exp.IntDiv})

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


class _OssieParse(sqlglot.Dialect):
    """Default spelling for reading OSSIE_SQL_2026, with Ossie's NULL ordering.

    Ossie orders NULLs last for ASC and first for DESC, the reverse of the sqlglot default.
    Reading with it records a bare ORDER BY item's implied placement with Ossie's meaning, so
    a translation keeps it instead of flipping it. The specification gives this default to
    OSSIE_SQL_2026 only: ANSI_SQL leaves the placement to the engine, here Databricks, whose
    placement the sqlglot default already reads.
    """

    NULL_ORDERING = "nulls_are_large"


class _OssieSql(Databricks):
    """Databricks spelling, which Ossie shares for its functions, with ANSI quoting."""

    # Match _OssieParse so the printer omits only the placements Ossie also defaults to,
    # and spells out the rest (a Databricks ASC NULLS FIRST becomes an explicit clause).
    NULL_ORDERING = "nulls_are_large"

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
            # Databricks integer division; Ossie writes it as TRUNC(x / y, 0). A generator
            # transform (not a tree transform) so the operands are still rendered.
            exp.IntDiv: lambda self, node: (
                f"TRUNC({self.sql(node, 'this')} / {self.sql(node, 'expression')}, 0)"
            ),
        }

        def identifier_sql(self, expression: exp.Identifier) -> str:
            # A name parsed from Databricks text or built from a name, both unquoted.
            if not expression.quoted and expression.name.upper() in _RESERVED:
                expression = exp.to_identifier(expression.name.upper(), quoted=True)
            return super().identifier_sql(expression)


def parse(text: str, dialect: str) -> exp.Expression:
    """Parse `text` as the Ossie validator does for `dialect`."""
    try:
        # The validator reads OSSIE_SQL_2026 with the default dialect; read it with Ossie's
        # NULL ordering so a bare window ORDER BY keeps its meaning across a translation.
        read = _OssieParse if dialect == OSSIE else _ossie.sqlglot_dialect(dialect)
        tree = sqlglot.parse_one(text, read=read)
    except (ParseError, TokenError, KeyError) as error:
        raise Untranslatable(f"cannot parse {text!r} as {dialect}") from error
    tree = tree.transform(_normalize_dateadd)
    if dialect == DATABRICKS:
        # Databricks identifiers are case-insensitive, so a backtick quote on a regular name
        # only marks a reserved word, which the Ossie printer quotes again. ANSI and
        # OSSIE_SQL quotes are case-sensitive and intentional, so they are left alone.
        for node in tree.find_all(exp.Identifier):
            if node.quoted and _REGULAR_IDENTIFIER.fullmatch(node.name):
                node.set("quoted", False)
    return tree


def _normalize_dateadd(node: exp.Expression) -> exp.Expression:
    # The default dialect reads the unit of DATEADD(day, 3, x) as a column named `day`.
    if _call_name(node) == "DATEADD" and len(node.expressions) == 3:
        unit, amount, value = node.expressions
        return exp.TimestampAdd(this=value, expression=amount, unit=exp.var(unit.name.upper()))
    return node


def _call_name(node: exp.Expression) -> str | None:
    return node.name.upper() if isinstance(node, exp.Anonymous) else None


def _unsupported(tree: exp.Expression, target: str) -> list[str]:
    """Functions, operators and other syntax with no verified translation to `target`."""
    portable = _PORTABLE | _PORTABLE_OPERATORS | _PORTABLE_SYNTAX
    if target == OSSIE:
        portable |= _FROM_DATABRICKS_ONLY
    return sorted(
        {
            (_call_name(node) or node.sql_name())
            if isinstance(node, exp.Func)
            else type(node).__name__
            for node in tree.walk()
            if type(node) not in portable and _call_name(node) not in _PORTABLE_CALLS
        }
    )


def translate(tree: exp.Expression, target: str) -> str:
    """Print `tree` in `target`, OSSIE_SQL_2026 or DATABRICKS."""
    unsupported = _unsupported(tree, target)
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
