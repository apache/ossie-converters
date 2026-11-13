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

"""A deliberately narrow DAX-to-OSSIE_SQL translator for Power BI measures.

Power BI measures are DAX. On import, the Microsoft converter keeps them as DAX,
so a measure that leaves Power BI arrives at every other engine as an opaque
vendor string it cannot evaluate. This module is the opt-in reverse of
``_sql_to_dax``: it translates the measures whose ``OSSIE_SQL_2026`` equivalent is
exact, and refuses everything else.

The governing rule is the one ``_sql_to_dax`` states and ``#437`` argues for:
never emit a plausible-looking expression that was not authored for the target.
A measure this module declines is kept verbatim as DAX with a diagnostic; a
measure it mistranslated would deploy cleanly on another engine and quietly
return a wrong number. Refusal is the safe default; translation is the exception
that must be proven exact.

The measure is parsed in full (``_dax_parser``), so anything outside the translated
set is refused by name, with the reason recorded for that function in
``_dax_catalog``.

What is translated
------------------
Single-column aggregates, the exact reverses of what ``_sql_to_dax`` emits::

    SUM('Sales'[Amount])                 ->  SUM(Sales.Amount)
    AVERAGE('Sales'[Amount])             ->  AVG(Sales.Amount)
    MIN('Sales'[Amount])                 ->  MIN(Sales.Amount)
    MAX('Sales'[Amount])                 ->  MAX(Sales.Amount)
    COUNTA('Sales'[Customer])            ->  COUNT(Sales.Customer)
    DISTINCTCOUNTNOBLANK('Sales'[Cust])  ->  COUNT(DISTINCT Sales.Cust)

and, each by a rule that makes it exact:

* ``COUNT(col)`` -> ``COUNT(col)`` for a typed column that is not boolean: DAX ``COUNT``
  documents ``TRUE``/``FALSE`` values as unsupported, and counts the non-blank values of
  any other type, as SQL ``COUNT`` counts the non-NULL ones.
* ``DISTINCTCOUNT(col)`` -> ``COUNT(DISTINCT col) + MAX(CASE WHEN col IS NULL THEN 1 ELSE
  0 END)``: DAX counts BLANK as one more distinct value, SQL ``COUNT(DISTINCT)`` skips NULL,
  so 1 is added when the column holds a NULL. Over no rows the ``MAX`` is NULL, so the
  result is NULL, as DAX's BLANK.
* ``MEDIAN``, ``STDEV.S`` / ``STDEV.P`` (-> ``STDDEV_SAMP`` / ``STDDEV_POP``) and ``VAR.S``
  / ``VAR.P`` (-> ``VAR_SAMP`` / ``VAR_POP``) over a numeric column, and
  ``PERCENTILE.INC(col, k)`` -> ``PERCENTILE_CONT(k) WITHIN GROUP (ORDER BY col)`` for a
  literal ``k`` between 0 and 1 (both interpolate at position ``k * (n - 1) + 1``). Where
  DAX raises an error for too few values (one value for ``STDEV.S``), SQL returns NULL:
  neither side produces a number.

and the X-iterators over a table named directly: ``SUMX(T, e)`` -> ``SUM(e)``, likewise
``AVERAGEX`` (``AVG``), ``MINX`` / ``MAXX``, ``COUNTX`` (``COUNT``, not over TRUE/FALSE),
``MEDIANX``, ``PERCENTILEX.INC`` (``PERCENTILE_CONT ... WITHIN GROUP``), ``STDEVX.S`` / ``.P``
and ``VARX.S`` / ``.P``. The row expression ``e`` is translated in the row context of ``T``:
``T[col]`` is the column's value on the row, and the BLANK rules above apply per row. DAX
skips BLANK results as SQL skips NULL. ``e`` may read only columns of ``T`` (another table
needs ``RELATED``) and no measure (context transition) or aggregate (nested aggregation),
and must read at least one column, so the aggregate reads exactly one dataset.

and composition over them:

* ``DIVIDE(a, b)`` -> ``a / NULLIF(b, 0)``. DAX returns BLANK when ``b`` is zero or
  BLANK; ``NULLIF`` gives NULL in both cases. This is also the form ``_sql_to_dax``
  turns back into ``DIVIDE``, so the pair round-trips.
* ``a * b``, exact: BLANK times anything is BLANK, as NULL is in SQL.
* ``a + b`` and ``a - b``. DAX treats BLANK as 0 here unless both sides are BLANK
  (``BLANK + 5`` is 5, ``BLANK + BLANK`` is BLANK), whereas SQL ``NULL + 5`` is NULL.
  So each nullable operand is wrapped in ``COALESCE(x, 0)``, and when both operands
  can be NULL the result is guarded so that two BLANKs still give NULL::

      [Sales] - [Cost]  ->  CASE WHEN s IS NULL AND c IS NULL THEN NULL
                                 ELSE COALESCE(s, 0) - COALESCE(c, 0) END

* ``a / n`` for a non-zero numeric literal ``n``.
* Numeric literals, parentheses, unary ``+``, and ``-`` on a numeric literal. DAX also
  accepts ``.5`` and ``2.``; they are written as ``0.5`` and ``2.0``, the forms every SQL
  engine reads. An exponent (``1e3``) is kept: it is the SQL approximate numeric literal.
* Measure references ``[Other Measure]``, expanded inline. At the top level of a
  measure there is no row context, so a referenced measure evaluates in the same
  filter context as the measure that references it, and inlining is exact.
* ``VAR`` / ``RETURN``, by substituting each variable where it is used (a variable is
  evaluated once, in the context where it is defined, which is the same context).

and logic and conditionals, again matching DAX's documented BLANK rules:

* Comparisons ``= <> < > <= >=`` between numbers. DAX compares a BLANK number as 0
  (``BLANK = 0`` is TRUE), so each nullable operand is wrapped in ``COALESCE(x, 0)``;
  a comparison is therefore never NULL, as in DAX.
* Strict ``==``, which does not coerce (``BLANK == 0`` is FALSE, ``BLANK == BLANK`` is
  TRUE): ``a IS NOT DISTINCT FROM b``.
* ``IF(test, a[, b])`` and ``IF.EAGER`` -> ``CASE WHEN test THEN a [ELSE b] END``; without
  a value if false both return BLANK / NULL. A numeric test is TRUE when non-zero, and
  BLANK counts as 0, so it becomes ``COALESCE(x, 0) <> 0``.
* ``SWITCH(TRUE, test, result, ..., [else])`` -> ``CASE WHEN ... END`` (BLANK when nothing
  matches and there is no else).
* ``COALESCE``, ``BLANK()``, ``ISBLANK(x)`` (``x IS NULL``), ``TRUE`` / ``FALSE``,
  ``AND`` / ``OR`` / ``NOT`` and ``&&`` / ``||``, and text literals as results.

An aggregate is a number for arithmetic, comparisons and conditions only when it is known
to be one: ``COUNTA`` and ``DISTINCTCOUNTNOBLANK`` always are, while ``SUM``, ``AVERAGE``,
``MIN`` and ``MAX`` take the type of their column, which the ``resolve`` callback reports
from the model. ``MAX`` over a date or text column is a date or text value, so it is never
wrapped in ``COALESCE(x, 0)``.

and math functions, but only for arguments that can never be BLANK (a literal,
``COALESCE(x, 0)``, a variable holding one), because Microsoft does not document what
they return for BLANK: ``ABS``, ``SIGN``, ``SQRT``, ``POWER``, ``EXP``, ``LN``, ``LOG10``,
``PI``, trigonometry, ``DEGREES`` / ``RADIANS``; ``LOG(number, base)`` -> ``LOG(base,
number)`` (base 10 when omitted, ``LOG10``); ``INT`` -> ``FLOOR``; ``TRUNC`` and ``ROUNDDOWN``
-> ``TRUNC(x, d)``; ``QUOTIENT`` -> ``TRUNC(CAST(n AS DOUBLE) / d, 0)``; and ``MOD``, whose
result takes the sign of the divisor, -> ``n - d * FLOOR(CAST(n AS DOUBLE) / d)`` (the
documented identity; SQL ``MOD`` takes the sign of the dividend). ``MOD`` and ``QUOTIENT``
force true division so an engine that truncates integer division gives the same result.

and text functions, again only for arguments that can never be BLANK (a text literal,
``COALESCE(x, "")``, a column that is not nullable), for the same reason: ``LEFT`` /
``RIGHT`` (count omitted is 1), ``MID`` -> ``SUBSTRING``, ``LEN`` -> ``LENGTH``, ``UPPER``,
``LOWER``, ``SUBSTITUTE`` -> ``REPLACE`` (all occurrences, with a non-empty literal to
find), and ``CONCATENATE`` and ``&`` -> ``CONCAT``. Counts and positions must be
non-negative integer literals (positions from 1), because DAX raises an error on a
negative one where SQL engines differ.

and date functions, only for a date that can never be BLANK (DAX reads a BLANK date as 30
December 1899, so ``YEAR(BLANK())`` is 1899, where SQL gives NULL): ``YEAR``, ``QUARTER``,
``MONTH`` and ``DAY`` of a date or datetime, and ``HOUR``, ``MINUTE`` and ``SECOND`` of a
datetime (DAX gives 0 for a date-only value, while the spec's ``HOUR`` takes a timestamp);
``DATE(y, m, d)`` with integer literals for a valid date from 1900 on -> ``DATE 'YYYY-MM-DD'``
(DAX shifts earlier years and rolls out-of-range months and days over);
``EDATE(date, n)`` -> ``DATEADD(month, n, date)`` for a date-only value and an integer
literal ``n``; and ``EOMONTH(date, n)`` -> ``CAST(DATEADD(day, -1, DATEADD(month, n + 1,
DATE_TRUNC('month', date))) AS DATE)``. EOMONTH truncates first, so no day overflows a
shorter month and the result does not depend on how an engine clamps month-ends; EDATE
relies on ``DATEADD`` clamping a month-end the way DAX does (31 January plus one month is
the last day of February), which engines agree on and the spec does not spell out.
A date and a datetime combine as a datetime (``COALESCE(datetime, DATE(...))``).

Calculated columns
------------------
``translate_column`` translates a calculated column, which DAX evaluates once per row
of its own table (row context). The same rules apply, plus:

* ``[Column]`` and ``'Table'[Column]`` of the column's own table become the referenced
  column's *source column*: the spec's field expressions refer to source columns, not to
  other fields. A referenced calculated column is expanded inline (it is evaluated on
  the same row, so inlining is exact), like a measure reference.
* A column's TMSL ``isNullable: false`` is the never-BLANK precondition the text and
  math functions need; a column without it may be BLANK.
* Refused: an aggregate (in a calculated column it ranges over the whole table, which a
  field expression cannot express), a measure reference (evaluated with context
  transition), a column of another table (needs ``RELATED``, a later tier), and a column
  that is not text, number, boolean, date or datetime (a later tier). A ``dateTime``
  column with a date-only format string is a date, as on import.

What is refused (and why, so the refusal is defensible)
-------------------------------------------------------
* ``COUNT`` over a boolean column or a column the model gives no type for, and the
  statistics over a column that is not numeric. As ``_sql_to_dax`` notes, a DAX count
  over an empty set is BLANK where SQL gives 0; that convention is accepted in both
  directions (``COUNTA``, ``COUNT``).
* ``COUNTROWS`` and ``COUNTBLANK``: both need ``COUNT(*)``, which reads no dataset, and a
  model-scoped metric whose aggregate reads no dataset has no defined grain in the spec.
  ``PERCENTILE.EXC`` (it interpolates at ``k * (n + 1)``; the spec has no exclusive
  form), and ``AVERAGEA`` / ``MINA`` / ``MAXA`` (text counts as 0, ``TRUE`` as 1).
* The ``/`` operator over anything but a non-zero literal: DAX returns Infinity or
  NaN when dividing by zero or BLANK, which has no portable form (``DIVIDE`` does).
* ``DIVIDE`` with an alternate result, ``^``, and ``-`` applied to an expression: later
  tiers.
* ``SWITCH`` matching a value (``SWITCH(x, 1, "a", ...)``): how BLANK matches a value is
  not documented, so only the ``SWITCH(TRUE, ...)`` form is translated.
* Logical operators over a boolean that may be BLANK: DAX's logic with BLANK
  (``TRUE AND BLANK`` is FALSE, ``BLANK OR BLANK`` is BLANK) is not SQL's NULL logic.
* Comparisons of text, and results that mix types (text in one branch, a number in
  another): later tiers. This includes ``SUM``, ``AVERAGE``, ``MIN`` and ``MAX`` over a
  column that is not numeric (``MAX('Sales'[ShipDate]) > MAX('Sales'[OrderDate])``,
  ``MIN('Customer'[Country]) = MAX('Customer'[Country])``) or whose type the model does not
  give: comparing a BLANK date or text needs other rules than ``COALESCE(x, 0)``.
* ``TRIM``: DAX also collapses inner runs of spaces; the spec's ``TRIM`` removes only
  leading and trailing whitespace, and it does not fix whether ``REGEXP_REPLACE`` replaces
  every match. ``EXACT`` (whether a text comparison is case-sensitive depends on the
  engine's collation), ``VALUE`` (DAX raises an error on text that is not a number and
  parses by locale), ``FIND`` / ``SEARCH`` (an error when not found), ``CONTAINSSTRING``
  (case-insensitive, with wildcards), and a text function over a number (DAX formats the
  number as text). A text function over a value that may be BLANK.
* ``DATEDIFF``: DAX counts the interval boundaries crossed, and whether the spec's
  ``DATEDIFF`` counts boundaries or whole elapsed units is not specified (engines differ).
  Date arithmetic and comparisons (``[Date] + 1``), ``EDATE`` of a datetime, a time of day
  or a timestamp with a time zone, and a date that may be BLANK.
* ``ROUND``: DAX breaks ties half away from zero, and the spec's ``ROUND`` does not say how
  ties are broken. ``CEILING`` / ``FLOOR`` (rounding to a multiple of a significance) and
  ``ROUNDUP``: later tiers. A math function over a value that may be BLANK.
* Every other function, with the reason ``_dax_catalog`` records for it (a later
  tier of this converter, a feature the specification does not define yet, or no
  portable form at all).
* A column that is not fully qualified as ``Table[Column]``, a column that does not
  resolve to exactly one dataset field, and a table or column name that would need
  SQL quoting.

One reading is shared with ``_sql_to_dax`` rather than settled by the specification:
``OSSIE_SQL_2026`` division is taken to be true division, as DAX division is.

This module performs no I/O and holds no model state. The caller supplies a
``resolve`` callback mapping a DAX (table, column) to the Ossie (dataset, field,
datatype), and optionally the model's measures for measure references.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from ._dax_catalog import refusal_reason
from ._dax_parser import (
    Binary,
    Bracket,
    Call,
    Column,
    DaxParseError,
    Name,
    Number,
    Row,
    String,
    Table,
    TableConstructor,
    Unary,
    Var,
    parse_dax,
)

#: DAX aggregate -> OSSIE_SQL aggregate, for the exact reverses of _sql_to_dax.
#: COUNTA and DISTINCTCOUNTNOBLANK are what _sql_to_dax emits for SQL COUNT and
#: COUNT(DISTINCT); reversing those same functions keeps the round-trip exact.
_AGGREGATES = {
    "SUM": "SUM({})",
    "AVERAGE": "AVG({})",
    "MIN": "MIN({})",
    "MAX": "MAX({})",
    "COUNTA": "COUNT({})",
    "DISTINCTCOUNTNOBLANK": "COUNT(DISTINCT {})",
}

#: Aggregates that return a count, a number whatever the column's type.
_COUNTS = frozenset({"COUNTA", "DISTINCTCOUNTNOBLANK"})

#: Column aggregates beyond the six reverses of _sql_to_dax, each with the rule that makes
#: it exact: name -> (SQL template, kinds of column it accepts, result kind or None for
#: the column's own kind).
_NUMERIC_KINDS = frozenset({"number"})
_COLUMN_AGGREGATES = {
    # DAX COUNT counts the non-blank values of a column of any type except boolean
    # (TRUE/FALSE are documented as unsupported), so a typed, non-boolean column is SQL COUNT.
    "COUNT": ("COUNT({c})", frozenset({"number", "text", "date", "datetime"}), "number"),
    # DAX DISTINCTCOUNT counts BLANK as one more distinct value; SQL COUNT(DISTINCT) skips
    # NULL. Adding 1 when the column holds a NULL is exact, and over no rows the MAX is NULL,
    # so the whole is NULL, as DAX's BLANK.
    "DISTINCTCOUNT": (
        "COUNT(DISTINCT {c}) + MAX(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END)",
        frozenset({"number", "text", "date", "datetime", "boolean"}),
        "number",
    ),
    # Statistics over the non-blank numbers, as in SQL over the non-NULL ones.
    "MEDIAN": ("MEDIAN({c})", _NUMERIC_KINDS, "number"),
    "STDEV.S": ("STDDEV_SAMP({c})", _NUMERIC_KINDS, "number"),
    "STDEV.P": ("STDDEV_POP({c})", _NUMERIC_KINDS, "number"),
    "VAR.S": ("VAR_SAMP({c})", _NUMERIC_KINDS, "number"),
    "VAR.P": ("VAR_POP({c})", _NUMERIC_KINDS, "number"),
}

#: Functions whose DAX semantics differ from the SQL function of the same name, so a
#: generic catalog reason would understate why they are refused.
_SEMANTIC_MISMATCH = {
    "COUNTROWS": "COUNTROWS becomes COUNT(*), which reads no dataset: a model-scoped metric "
    "whose aggregate reads no dataset has no defined grain in the spec (COUNTA over a key "
    "column is the portable form)",
    "COUNTBLANK": "COUNTBLANK needs COUNT(*) minus COUNT(x), and COUNT(*) reads no dataset, "
    "so the metric has no defined grain in the spec",
    "PERCENTILE.EXC": "DAX PERCENTILE.EXC interpolates at position k * (n + 1); the spec's "
    "PERCENTILE_CONT interpolates at k * (n - 1) + 1, and has no exclusive form",
    "AVERAGEA": "DAX AVERAGEA counts text as 0 and TRUE as 1, which SQL AVG does not",
    "MINA": "DAX MINA counts text as 0 and TRUE as 1, which SQL MIN does not",
    "MAXA": "DAX MAXA counts text as 0 and TRUE as 1, which SQL MAX does not",
}

#: Every DAX function this module emits a translation for.
TRANSLATED_FUNCTIONS = frozenset(_AGGREGATES) | frozenset(
    ("ABS", "SIGN", "SQRT", "POWER", "EXP", "LN", "LOG", "LOG10", "INT", "TRUNC",
     "ROUND", "ROUNDDOWN", "QUOTIENT", "MOD", "PI", "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
     "DEGREES", "RADIANS")
) | {
    "DIVIDE", "IF", "IF.EAGER", "SWITCH", "COALESCE", "BLANK", "ISBLANK",
    "TRUE", "FALSE", "AND", "OR", "NOT",
    "LEFT", "RIGHT", "MID", "LEN", "UPPER", "LOWER", "SUBSTITUTE", "CONCATENATE",
    "YEAR", "QUARTER", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "DATE", "EDATE",
    "EOMONTH",
} | frozenset(("COUNT", "DISTINCTCOUNT", "MEDIAN", "PERCENTILE.INC", "STDEV.S", "STDEV.P",
               "VAR.S", "VAR.P")) | frozenset(("SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX",
                                              "MEDIANX", "PERCENTILEX.INC", "STDEVX.S",
                                              "STDEVX.P", "VARX.S", "VARX.P"))

# A simple identifier: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPONENT = re.compile(r"([^eE]*)(.*)")
_INTEGER = re.compile(r"\d+")

# Precedence of an emitted SQL fragment, for parenthesising only where needed.
_OR, _AND, _NOT, _CMP, _ADD, _MUL, _ATOM = 0, 1, 2, 3, 4, 5, 6

#: Fragment kinds. BLANK() is its own kind so it can stand in for a missing value of any type.
#: An aggregate over a column the model gives no type for is "untyped": not known to be a
#: number, so it is never used where a number is required.
_NUM, _BOOL, _TEXT, _DATE, _UNTYPED, _BLANK = (
    "number", "boolean", "text", "date", "untyped", "blank"
)
#: A date with a time of day (Ossie DateTime, a wall-clock timestamp), a time of day only,
#: and a timestamp with a time zone. Only date and datetime values are read by the date
#: functions; the other two are distinct so they never pass for one.
_DATETIME, _TIME, _DATETIME_TZ = "datetime", "time", "datetime with time zone"
_DATE_KINDS = frozenset({_DATE, _DATETIME})

#: Ossie datatypes and the fragment kind of an aggregate over a column of that type.
_KIND_OF_DATATYPE = {
    "Integer": _NUM,
    "Decimal": _NUM,
    "Float": _NUM,
    "String": _TEXT,
    "Boolean": _BOOL,
    "Date": _DATE,
    "Time": _TIME,
    "DateTime": _DATETIME,
    "DateTimeTz": _DATETIME_TZ,
}

_COMPARISONS = {"=": "=", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


# Longest SQL fragment the translator emits. The BLANK guard on `+` and `-` repeats its
# operands and measure references are expanded inline, so a chain of derived measures
# can double the SQL at every level; past this size the measure is refused (kept as
# DAX) rather than emitted as megabytes of SQL.
_MAX_SQL_LENGTH = 4000


class _RefusedError(Exception):
    """Raised inside the translator; its message is the refusal reason."""


@dataclass(frozen=True)
class _Sql:
    text: str
    precedence: int
    nullable: bool
    kind: str = _NUM

    def __post_init__(self):
        # Checked on every fragment, so a growing expression stops at the first
        # fragment over the limit instead of after it has been built.
        if len(self.text) > _MAX_SQL_LENGTH:
            raise _RefusedError(
                f"the expression is too large to translate exactly (over "
                f"{_MAX_SQL_LENGTH} characters of SQL)"
            )


def translate_measure(expression, resolve, measures=None):
    """Translate a DAX measure to an OSSIE_SQL expression.

    ``resolve`` maps a DAX ``(table, column)`` to the Ossie ``(dataset, field,
    datatype)``, or returns None when the column does not resolve to exactly one field.
    ``datatype`` is the field's Ossie datatype, or None when the model gives none.
    ``measures`` maps measure names to their DAX, for expanding measure references;
    without it a measure reference is refused.

    Returns ``(sql, None)`` on success or ``(None, reason)`` on refusal. The
    caller keeps the original DAX and attaches ``reason`` as a diagnostic when a
    measure is refused.
    """
    # The parser and the translator are recursive, so a deeply nested expression or a
    # long chain of measure references could exhaust the stack; that one measure is
    # then kept as DAX instead of ending the whole import.
    too_deep = "the expression is too deeply nested to translate"
    try:
        tree = parse_dax(expression)
    except DaxParseError as error:
        return None, f"the DAX could not be parsed ({error})"
    except RecursionError:
        return None, too_deep
    translator = _Translator(resolve, measures)
    try:
        return translator.translate(tree, {}).text, None
    except _RefusedError as refusal:
        return None, str(refusal)
    except RecursionError:
        return None, too_deep


@dataclass(frozen=True)
class RowColumn:
    """A column of the calculated column's own table, as ``translate_column`` sees it.

    ``source`` is the source column a data column reads (None for a calculated column),
    ``dax`` the expression of a calculated column (None for a data column), ``kind`` one
    of ``"number"``, ``"text"``, ``"boolean"``, ``"date"``, ``"datetime"`` or None for any
    other type, and
    ``nullable`` False only when the column is declared ``isNullable: false``.
    """

    source: str | None
    dax: str | None
    kind: str | None
    nullable: bool = True


def translate_column(expression, table, columns):
    """Translate a DAX calculated column of ``table`` to an OSSIE_SQL field expression.

    ``columns`` maps each column name of ``table`` to a ``RowColumn``. Returns ``(sql,
    None)`` on success or ``(None, reason)`` on refusal, like ``translate_measure``.
    """
    too_deep = "the expression is too deeply nested to translate"
    try:
        tree = parse_dax(expression)
    except DaxParseError as error:
        return None, f"the DAX could not be parsed ({error})"
    except RecursionError:
        return None, too_deep
    translator = _Translator(None, None, row_table=table, row_columns=columns)
    try:
        return translator.translate(tree, {}).text, None
    except _RefusedError as refusal:
        return None, str(refusal)
    except RecursionError:
        return None, too_deep


class _Translator:
    def __init__(self, resolve, measures, row_table=None, row_columns=None, iter_table=None):
        self.resolve = resolve
        # Iterator row context: set while translating the row expression of SUMX and the
        # other X-iterators over ``iter_table``; ``reads`` counts the columns it reads.
        self.iter_table = iter_table
        self.reads = 0
        self.measures = {name.upper(): dax for name, dax in (measures or {}).items()}
        self.expanding = []
        # Row context: set when translating a calculated column of ``row_table``.
        self.row_table = row_table
        self.row_columns = {name.upper(): (name, c) for name, c in (row_columns or {}).items()}

    def translate(self, node, variables):
        if isinstance(node, Number):
            return _Sql(_sql_number(node.text), _ATOM, nullable=False)
        if isinstance(node, String):
            return _Sql("'" + node.value.replace("'", "''") + "'", _ATOM, False, _TEXT)
        if isinstance(node, Call):
            return self.call(node, variables)
        if isinstance(node, Binary):
            return self.binary(node, variables)
        if isinstance(node, Unary):
            return self.unary(node, variables)
        if isinstance(node, Bracket):
            if self.iter_table is not None:
                raise _RefusedError(
                    f"[{node.name}] inside an iterator's row expression: a measure there is "
                    "evaluated with context transition, and a column must name its table"
                )
            if self.row_table is not None:
                return self.row_column(node.name)
            return self.measure_reference(node.name)
        if isinstance(node, Name):
            if node.name.upper() in ("TRUE", "FALSE") and node.name.upper() not in variables:
                return _Sql(node.name.upper(), _ATOM, False, _BOOL)
            return self.variable(node.name, variables)
        if isinstance(node, Var):
            return self.var_block(node, variables)
        if isinstance(node, Column) and self.iter_table is not None:
            return self.iter_column(node)
        if isinstance(node, Column) and self.row_table is not None:
            if node.table.upper() != self.row_table.upper():
                raise _RefusedError(
                    f"'{node.table}'[{node.column}] is a column of another table; reading "
                    "it from a calculated column needs RELATED, a later tier"
                )
            return self.row_column(node.column)
        if isinstance(node, Column):
            raise _RefusedError(
                f"a bare column reference ('{node.table}'[{node.column}]) is not a measure; "
                "only aggregates over columns are translated"
            )
        if isinstance(node, (TableConstructor, Row)):
            raise _RefusedError(
                "a table constructor ({...}) is a table, which a metric expression cannot express"
            )
        raise _RefusedError(f"{type(node).__name__.lower()} expressions are a later tier")

    # --- functions --------------------------------------------------------------

    def call(self, node, variables):
        if self.iter_table is not None and (
            node.name in _AGGREGATES or node.name in _COLUMN_AGGREGATES
            or node.name in _ITERATORS or node.name == "PERCENTILE.INC"
        ):
            raise _RefusedError(
                f"{node.name} inside an iterator's row expression is a nested aggregation, "
                "a later tier"
            )
        if node.name in _ITERATORS:
            if self.row_table is not None:
                raise _RefusedError(
                    f"{node.name} in a calculated column ranges over a whole table, "
                    "which a field expression cannot express"
                )
            return self.iterator(node, variables)
        if node.name in _AGGREGATES:
            if self.row_table is not None:
                raise _RefusedError(
                    f"{node.name} in a calculated column ranges over the whole table, "
                    "which a field expression cannot express"
                )
            return self.aggregate(node)
        if node.name in _COLUMN_AGGREGATES or node.name == "PERCENTILE.INC":
            if self.row_table is not None:
                raise _RefusedError(
                    f"{node.name} in a calculated column ranges over the whole table, "
                    "which a field expression cannot express"
                )
            if node.name == "PERCENTILE.INC":
                return self.percentile(node, variables)
            return self.column_aggregate(node)
        if node.name == "DIVIDE":
            return self.divide(node, variables)
        if node.name in _LOGIC_FUNCTIONS:
            return getattr(self, _LOGIC_FUNCTIONS[node.name])(node, variables)
        if node.name in _MATH_FUNCTIONS:
            return self.math(node, variables)
        if node.name in _MATH_REFUSED:
            raise _RefusedError(_MATH_REFUSED[node.name])
        if node.name in _TEXT_FUNCTIONS:
            return getattr(self, _TEXT_FUNCTIONS[node.name])(node, variables)
        if node.name in _TEXT_REFUSED:
            raise _RefusedError(_TEXT_REFUSED[node.name])
        if node.name in _DATE_FUNCTIONS:
            return getattr(self, _DATE_FUNCTIONS[node.name])(node, variables)
        if node.name in _DATE_REFUSED:
            raise _RefusedError(_DATE_REFUSED[node.name])
        if node.name in _SEMANTIC_MISMATCH:
            raise _RefusedError(_SEMANTIC_MISMATCH[node.name])
        raise _RefusedError(refusal_reason(node.name))

    def aggregate(self, node):
        if len(node.args) != 1 or not isinstance(node.args[0], Column):
            raise _RefusedError(
                "only an aggregate over one fully qualified column (Table[Column]) is translated"
            )
        column = node.args[0]
        resolved = self.resolve(column.table, column.column)
        if resolved is None:
            raise _RefusedError(
                f"column '{column.table}'[{column.column}] does not resolve to exactly one "
                "dataset field"
            )
        dataset, field, datatype = resolved
        if not _is_simple_identifier(dataset) or not _is_simple_identifier(field):
            raise _RefusedError("a dataset or field name needing SQL quoting is a later tier")
        # A count is a number; the other aggregates take the type of their column.
        kind = _NUM if node.name in _COUNTS else _column_kind(datatype)
        return _Sql(
            _AGGREGATES[node.name].format(f"{dataset}.{field}"), _ATOM, nullable=True, kind=kind
        )

    def column_field(self, node, function):
        """Resolve the one ``Table[Column]`` argument of an aggregate to (sql, kind)."""
        column = node.args[0] if node.args else None
        if not isinstance(column, Column):
            raise _RefusedError(
                f"{function} is translated only over one fully qualified column (Table[Column])"
            )
        resolved = self.resolve(column.table, column.column)
        if resolved is None:
            raise _RefusedError(
                f"column '{column.table}'[{column.column}] does not resolve to exactly one "
                "dataset field"
            )
        dataset, field, datatype = resolved
        if not _is_simple_identifier(dataset) or not _is_simple_identifier(field):
            raise _RefusedError("a dataset or field name needing SQL quoting is a later tier")
        return f"{dataset}.{field}", _column_kind(datatype)

    def column_aggregate(self, node):
        template, accepted, result = _COLUMN_AGGREGATES[node.name]
        if len(node.args) != 1:
            raise _RefusedError(
                f"{node.name} is translated only over one fully qualified column (Table[Column])"
            )
        field, kind = self.column_field(node, node.name)
        if kind not in accepted:
            raise _RefusedError(_aggregate_kind_reason(node.name, kind))
        return _Sql(template.format(c=field), _ATOM, nullable=True, kind=result)

    def percentile(self, node, variables):
        """PERCENTILE.INC(column, k) -> PERCENTILE_CONT(k) WITHIN GROUP (ORDER BY column).

        Both interpolate linearly between the values at position k * (n - 1) + 1 (the
        inclusive definition). ``k`` must be a literal between 0 and 1: DAX raises an error
        outside that range.
        """
        if len(node.args) != 2:
            raise _RefusedError("PERCENTILE.INC takes 2 arguments")
        field, kind = self.column_field(node, "PERCENTILE.INC")
        if kind != _NUM:
            raise _RefusedError(_aggregate_kind_reason("PERCENTILE.INC", kind))
        k = node.args[1]
        if not isinstance(k, Number) or not 0 <= float(k.text) <= 1:
            raise _RefusedError(
                "PERCENTILE.INC is translated only with a literal k between 0 and 1 (DAX "
                "raises an error outside that range)"
            )
        return _Sql(f"PERCENTILE_CONT({k.text}) WITHIN GROUP (ORDER BY {field})", _ATOM, True)

    def iterator(self, node, variables):
        """SUMX(Table, expression) and the other X-iterators -> the aggregate of the row
        expression, translated in the row context of ``Table``.

        DAX evaluates the expression once per row of the table (in the current filter
        context) and aggregates the results, skipping BLANK; SQL evaluates it per row of the
        dataset and skips NULL. Within the row, the same BLANK rules as everywhere else
        apply (``BLANK * x`` is BLANK, ``BLANK + x`` is ``x``). The expression may read
        only columns of the iterated table, so the aggregate reads exactly one dataset.
        """
        template, accepted = _ITERATORS[node.name]
        expected = 3 if node.name == "PERCENTILEX.INC" else 2
        if len(node.args) != expected:
            raise _RefusedError(f"{node.name} takes {expected} arguments")
        table = node.args[0]
        if isinstance(table, (Table, Name)) and not (
            isinstance(table, Name) and table.name.upper() in variables
        ):
            table_name = table.name
        else:
            raise _RefusedError(
                f"{node.name} is translated only over a table named directly; a table "
                "expression (FILTER, VALUES, ...) is a later tier"
            )
        inner = _Translator(self.resolve, None, iter_table=table_name)
        value = inner.translate(node.args[1], {})
        if inner.reads == 0:
            raise _RefusedError(
                f"the row expression of {node.name} reads no column of '{table_name}', so "
                "the aggregate reads no dataset and has no defined grain"
            )
        if value.kind not in accepted:
            raise _RefusedError(
                f"{node.name} of a {value.kind} row expression is not translated"
            )
        if node.name == "PERCENTILEX.INC":
            k = node.args[2]
            if not isinstance(k, Number) or not 0 <= float(k.text) <= 1:
                raise _RefusedError(
                    "PERCENTILEX.INC is translated only with a literal k between 0 and 1 "
                    "(DAX raises an error outside that range)"
                )
            text = f"PERCENTILE_CONT({k.text}) WITHIN GROUP (ORDER BY {value.text})"
        else:
            text = template.format(value.text)
        result = value.kind if node.name in ("MINX", "MAXX") else _NUM
        return _Sql(text, _ATOM, nullable=True, kind=result)

    def iter_column(self, node):
        """A column read in an iterator's row expression: the value on the current row."""
        if node.table.upper() != self.iter_table.upper():
            raise _RefusedError(
                f"'{node.table}'[{node.column}] is a column of another table than the "
                f"iterated '{self.iter_table}'; reading it needs RELATED, a later tier"
            )
        resolved = self.resolve(node.table, node.column)
        if resolved is None:
            raise _RefusedError(
                f"column '{node.table}'[{node.column}] does not resolve to exactly one "
                "dataset field"
            )
        dataset, field, datatype = resolved
        if not _is_simple_identifier(dataset) or not _is_simple_identifier(field):
            raise _RefusedError("a dataset or field name needing SQL quoting is a later tier")
        self.reads += 1
        return _Sql(f"{dataset}.{field}", _ATOM, nullable=True, kind=_column_kind(datatype))

    def divide(self, node, variables):
        if len(node.args) == 3:
            raise _RefusedError("DIVIDE with an alternate result is a later tier")
        if len(node.args) != 2:
            raise _RefusedError("DIVIDE takes a numerator and a denominator")
        numerator = self.number(node.args[0], variables)
        denominator = self.number(node.args[1], variables)
        text = f"{_wrap(numerator, _MUL)} / NULLIF({denominator.text}, 0)"
        return _Sql(text, _MUL, nullable=True)

    # --- operators --------------------------------------------------------------

    def binary(self, node, variables):
        if node.op == "*":
            left = self.number(node.left, variables)
            right = self.number(node.right, variables)
            text = f"{_wrap(left, _MUL)} * {_wrap(right, _MUL, strict=True)}"
            return _Sql(text, _MUL, nullable=left.nullable or right.nullable)
        if node.op == "/":
            if not _is_nonzero_number(node.right):
                raise _RefusedError(
                    "the / operator returns Infinity or NaN on a zero or BLANK divisor, "
                    "which has no portable form; DIVIDE is translated"
                )
            left = self.number(node.left, variables)
            text = f"{_wrap(left, _MUL)} / {_sql_number(node.right.text)}"
            return _Sql(text, _MUL, nullable=left.nullable)
        if node.op in ("+", "-"):
            return self.additive(node, variables)
        if node.op in _COMPARISONS:
            return self.comparison(node, variables)
        if node.op == "==":
            return self.strict_equality(node, variables)
        if node.op in ("&&", "||"):
            return self.logical(node.op, node.left, node.right, variables)
        if node.op == "&":
            return self.concat("&", (node.left, node.right), variables)
        raise _RefusedError(f"the {node.op} operator is a later tier")

    def additive(self, node, variables):
        left = self.number(node.left, variables)
        right = self.number(node.right, variables)
        # DAX treats a BLANK operand of + or - as 0, unless both are BLANK.
        left_text = _blank_as_zero(left)
        right_text = _blank_as_zero(right, strict=True)
        text = f"{left_text} {node.op} {right_text}"
        if left.nullable and right.nullable:
            guarded = (
                f"CASE WHEN {left.text} IS NULL AND {right.text} IS NULL THEN NULL "
                f"ELSE {text} END"
            )
            return _Sql(guarded, _ATOM, nullable=True)
        return _Sql(text, _ADD, nullable=False)

    def unary(self, node, variables):
        if node.op == "+":
            return self.translate(node.operand, variables)
        if node.op == "-" and isinstance(node.operand, Number):
            return _Sql(f"-{_sql_number(node.operand.text)}", _ATOM, nullable=False)
        if node.op == "NOT":
            operand = self.boolean(node.operand, variables)
            return _Sql(f"NOT {_wrap(operand, _NOT)}", _NOT, False, _BOOL)
        raise _RefusedError(f"the unary {node.op} operator on an expression is a later tier")

    # --- logic and conditionals ----------------------------------------------

    def number(self, node, variables):
        """Translate an operand of arithmetic or a comparison, which must be numeric."""
        fragment = self.translate(node, variables)
        if fragment.kind == _UNTYPED:
            raise _RefusedError(
                "arithmetic or comparison on an aggregate whose column has no numeric "
                "datatype in the model is not translated"
            )
        if fragment.kind not in (_NUM, _BLANK):
            raise _RefusedError(
                f"arithmetic or comparison on a {fragment.kind} value is a later tier"
            )
        return fragment

    def boolean(self, node, variables):
        """Translate an operand of AND / OR / NOT, which must be a never-BLANK boolean.

        DAX's three-valued logic with BLANK (TRUE AND BLANK is FALSE, BLANK OR BLANK is
        BLANK) does not match SQL NULL logic, so a boolean that may be BLANK is refused.
        """
        fragment = self.translate(node, variables)
        if fragment.kind != _BOOL or fragment.nullable:
            raise _RefusedError("a logical operand must be a comparison or TRUE / FALSE")
        return fragment

    def condition(self, node, variables):
        """Translate the test of IF or SWITCH(TRUE, ...) to a SQL condition.

        A numeric test is TRUE when it is non-zero; BLANK counts as 0, so it is FALSE.
        """
        fragment = self.translate(node, variables)
        if fragment.kind == _BOOL and not fragment.nullable:
            return fragment
        if fragment.kind in (_NUM, _BLANK):
            return _Sql(f"{_blank_as_zero(fragment)} <> 0", _CMP, False, _BOOL)
        raise _RefusedError(f"a {fragment.kind} value is not a condition")

    def comparison(self, node, variables):
        # DAX compares a BLANK number as 0 (BLANK = 0 is TRUE); SQL would give NULL.
        left = self.number(node.left, variables)
        right = self.number(node.right, variables)
        text = f"{_blank_as_zero(left)} {_COMPARISONS[node.op]} {_blank_as_zero(right)}"
        return _Sql(text, _CMP, False, _BOOL)

    def strict_equality(self, node, variables):
        # == does not coerce BLANK: BLANK == 0 is FALSE, BLANK == BLANK is TRUE.
        left = self.number(node.left, variables)
        right = self.number(node.right, variables)
        text = f"{_wrap(left, _ADD)} IS NOT DISTINCT FROM {_wrap(right, _ADD)}"
        return _Sql(text, _CMP, False, _BOOL)

    def logical(self, op, left_node, right_node, variables):
        left = self.boolean(left_node, variables)
        right = self.boolean(right_node, variables)
        sql_op, precedence = ("AND", _AND) if op in ("&&", "AND") else ("OR", _OR)
        text = f"{_wrap(left, precedence)} {sql_op} {_wrap(right, precedence, strict=True)}"
        return _Sql(text, precedence, False, _BOOL)

    def and_or(self, node, variables):
        if len(node.args) != 2:
            raise _RefusedError(f"{node.name} takes exactly two arguments")
        return self.logical(node.name, node.args[0], node.args[1], variables)

    def not_(self, node, variables):
        if len(node.args) != 1:
            raise _RefusedError("NOT takes exactly one argument")
        operand = self.boolean(node.args[0], variables)
        return _Sql(f"NOT {_wrap(operand, _NOT)}", _NOT, False, _BOOL)

    def true_false(self, node, variables):
        if node.args:
            raise _RefusedError(f"{node.name}() takes no arguments")
        return _Sql(node.name, _ATOM, False, _BOOL)

    def blank(self, node, variables):
        if node.args:
            raise _RefusedError("BLANK() takes no arguments")
        return _Sql("NULL", _ATOM, True, _BLANK)

    def isblank(self, node, variables):
        if len(node.args) != 1:
            raise _RefusedError("ISBLANK takes exactly one argument")
        value = self.translate(node.args[0], variables)
        if value.kind == _BOOL:
            raise _RefusedError("ISBLANK of a boolean is a later tier")
        return _Sql(f"{_wrap(value, _ADD)} IS NULL", _CMP, False, _BOOL)

    def coalesce(self, node, variables):
        if len(node.args) < 2:
            raise _RefusedError("COALESCE takes at least two arguments")
        values = [self.translate(arg, variables) for arg in node.args]
        kind = _common_kind(values)
        text = "COALESCE(" + ", ".join(v.text for v in values) + ")"
        return _Sql(text, _ATOM, all(v.nullable for v in values), kind)

    def if_(self, node, variables):
        if len(node.args) not in (2, 3):
            raise _RefusedError("IF takes a test, a value if true and an optional value if false")
        test = self.condition(node.args[0], variables)
        branches = [self.translate(arg, variables) for arg in node.args[1:]]
        kind = _common_kind(branches)
        # Without a value if false, DAX returns BLANK; CASE without ELSE returns NULL.
        otherwise = f" ELSE {branches[1].text}" if len(branches) == 2 else ""
        text = f"CASE WHEN {test.text} THEN {branches[0].text}{otherwise} END"
        nullable = len(branches) == 1 or any(b.nullable for b in branches)
        return _Sql(text, _ATOM, nullable, kind)

    def switch(self, node, variables):
        if not node.args or not _is_true(node.args[0]):
            raise _RefusedError(
                "SWITCH matching a value is a later tier (its BLANK matching is not "
                "documented); SWITCH(TRUE, ...) is translated"
            )
        rest = list(node.args[1:])
        if len(rest) < 2:
            raise _RefusedError("SWITCH(TRUE, ...) needs at least one condition and result")
        otherwise = rest.pop() if len(rest) % 2 == 1 else None
        whens, results = [], []
        for test_node, result_node in zip(rest[0::2], rest[1::2], strict=True):
            test = self.condition(test_node, variables)
            result = self.translate(result_node, variables)
            whens.append(f"WHEN {test.text} THEN {result.text}")
            results.append(result)
        if otherwise is not None:
            results.append(self.translate(otherwise, variables))
        kind = _common_kind(results)
        tail = f" ELSE {results[-1].text}" if otherwise is not None else ""
        text = "CASE " + " ".join(whens) + tail + " END"
        nullable = otherwise is None or any(r.nullable for r in results)
        return _Sql(text, _ATOM, nullable, kind)

    # --- math ------------------------------------------------------------------

    def math(self, node, variables):
        """Translate a math function whose arguments can never be BLANK.

        Microsoft does not document what the math functions return for a BLANK argument,
        so a translation for a possibly-BLANK argument would be a guess. With a never-BLANK
        argument (a literal, COALESCE(..., 0), a variable holding one) the result is plain
        math and the translation is exact.
        """
        arities, render = _MATH_FUNCTIONS[node.name]
        if len(node.args) not in arities:
            raise _RefusedError(f"{node.name} takes {_arity_text(arities)} argument(s)")
        if node.name == "ROUND":
            # The spec defines d as a number of decimal places. DAX also accepts a
            # negative d (rounding left of the decimal point), which the spec does not.
            digits = node.args[1]
            if not (isinstance(digits, Number) and _INTEGER.fullmatch(digits.text)):
                raise _RefusedError(
                    "ROUND is translated only with an integer literal number of decimal "
                    "places of at least 0: the spec defines ROUND(x, d) for d decimal places"
                )
        args = [self.number(arg, variables) for arg in node.args]
        if any(arg.nullable for arg in args):
            raise _RefusedError(
                f"{node.name} of a value that may be BLANK: its BLANK behaviour is not "
                "documented (wrap the argument in COALESCE(x, 0) to translate it)"
            )
        text, precedence = render([arg.text for arg in args], args)
        return _Sql(text, precedence, False)

    # --- text -------------------------------------------------------------------

    def text(self, node, variables, function):
        """Translate a text argument, which must be text and can never be BLANK."""
        fragment = self.translate(node, variables)
        if fragment.kind != _TEXT:
            raise _RefusedError(
                f"{function} of a {fragment.kind} value is a later tier (DAX converts it "
                "to text first)"
            )
        if fragment.nullable:
            raise _RefusedError(
                f"{function} of a value that may be BLANK: its BLANK behaviour is not "
                'documented (wrap the argument in COALESCE(x, "") to translate it)'
            )
        return fragment

    def left_right(self, node, variables):
        if len(node.args) not in (1, 2):
            raise _RefusedError(f"{node.name} takes 1 or 2 arguments")
        value = self.text(node.args[0], variables, node.name)
        count = _count(node.args[1], node.name, 0) if len(node.args) == 2 else "1"
        return _Sql(f"{node.name}({value.text}, {count})", _ATOM, False, _TEXT)

    def mid(self, node, variables):
        if len(node.args) != 3:
            raise _RefusedError("MID takes 3 arguments")
        value = self.text(node.args[0], variables, "MID")
        start = _count(node.args[1], "MID", 1)
        count = _count(node.args[2], "MID", 0)
        return _Sql(f"SUBSTRING({value.text}, {start}, {count})", _ATOM, False, _TEXT)

    def length(self, node, variables):
        if len(node.args) != 1:
            raise _RefusedError("LEN takes 1 argument")
        value = self.text(node.args[0], variables, "LEN")
        return _Sql(f"LENGTH({value.text})", _ATOM, False, _NUM)

    def letter_case(self, node, variables):
        if len(node.args) != 1:
            raise _RefusedError(f"{node.name} takes 1 argument")
        value = self.text(node.args[0], variables, node.name)
        return _Sql(f"{node.name}({value.text})", _ATOM, False, _TEXT)

    def substitute(self, node, variables):
        if len(node.args) == 4:
            raise _RefusedError("SUBSTITUTE of one occurrence (instance_num) is a later tier")
        if len(node.args) != 3:
            raise _RefusedError("SUBSTITUTE takes 3 arguments")
        if not isinstance(node.args[1], String) or not node.args[1].value:
            raise _RefusedError("SUBSTITUTE is translated only with a non-empty literal to find")
        value, old, new = (self.text(arg, variables, "SUBSTITUTE") for arg in node.args)
        return _Sql(f"REPLACE({value.text}, {old.text}, {new.text})", _ATOM, False, _TEXT)

    def concatenate(self, node, variables):
        if len(node.args) != 2:
            raise _RefusedError("CONCATENATE takes 2 arguments")
        return self.concat("CONCATENATE", node.args, variables)

    def concat(self, function, nodes, variables):
        left, right = (self.text(arg, variables, function) for arg in nodes)
        return _Sql(f"CONCAT({left.text}, {right.text})", _ATOM, False, _TEXT)

    # --- dates ------------------------------------------------------------------

    def date_value(self, node, variables, function, kinds=_DATE_KINDS):
        """Translate a date argument, which must be of one of ``kinds`` and never BLANK.

        DAX reads a BLANK date as 30 December 1899 (``YEAR(BLANK())`` is 1899) where SQL
        functions give NULL, so a date that may be BLANK is refused; COALESCE with a DATE
        literal, or a column declared not nullable, makes it translatable.
        """
        fragment = self.translate(node, variables)
        if fragment.kind not in kinds:
            wanted = " or ".join(sorted(kinds))
            raise _RefusedError(f"{function} of a {fragment.kind} value is not translated "
                                f"(it needs a {wanted} value)")
        if fragment.nullable:
            raise _RefusedError(
                f"{function} of a date that may be BLANK: DAX reads BLANK as 30 December "
                "1899 where SQL gives NULL (wrap it in COALESCE(x, DATE(...)) to translate it)"
            )
        return fragment

    def date_part(self, node, variables):
        """YEAR, QUARTER, MONTH, DAY of a date; HOUR, MINUTE, SECOND of a datetime."""
        if len(node.args) != 1:
            raise _RefusedError(f"{node.name} takes 1 argument")
        if node.name in ("HOUR", "MINUTE", "SECOND"):
            # On a date-only value DAX gives 0; the spec's HOUR takes a timestamp, and
            # engines differ on a DATE argument.
            value = self.date_value(node.args[0], variables, node.name, frozenset({_DATETIME}))
        else:
            value = self.date_value(node.args[0], variables, node.name)
        return _Sql(f"{node.name}({value.text})", _ATOM, False, _NUM)

    def date_literal(self, node, variables):
        """DATE(year, month, day) with integer literals -> a DATE typed literal."""
        if len(node.args) != 3:
            raise _RefusedError("DATE takes 3 arguments")
        if not all(isinstance(a, Number) and _INTEGER.fullmatch(a.text) for a in node.args):
            raise _RefusedError(
                "DATE is translated only with integer literals: the spec builds a date from "
                "a literal or an ISO string, not from parts"
            )
        year, month, day = (int(a.text) for a in node.args)
        try:
            value = datetime.date(year, month, day)
        except ValueError:
            value = None
        if value is None or year < 1900:
            # DAX adds 1900 to a year below 1900 and rolls an out-of-range month or day
            # over into the next one; written as a valid date it translates.
            raise _RefusedError(
                "DATE is translated only for a valid date from year 1900 on (DAX shifts "
                "earlier years and rolls out-of-range months and days over)"
            )
        return _Sql(f"DATE '{value.isoformat()}'", _ATOM, False, _DATE)

    def edate(self, node, variables):
        """EDATE(date, months) -> DATEADD(month, months, date) for a date-only value."""
        if len(node.args) != 2:
            raise _RefusedError("EDATE takes 2 arguments")
        # Whether DAX keeps the time of day of a datetime is not documented, so only a
        # date-only value is shifted.
        value = self.date_value(node.args[0], variables, "EDATE", frozenset({_DATE}))
        months = _integer_literal(node.args[1], "EDATE")
        return _Sql(f"DATEADD(month, {months}, {value.text})", _ATOM, False, _DATE)

    def eomonth(self, node, variables):
        """EOMONTH(date, months): the last day of the month ``months`` after ``date``.

        Emitted as the day before the first day of the month after that one. Truncating
        to the first of the month before adding months means no day ever overflows a
        shorter month, so the result does not depend on how an engine clamps month-ends.
        The CAST fixes the type: DATE_TRUNC returns a timestamp on some engines.
        """
        if len(node.args) != 2:
            raise _RefusedError("EOMONTH takes 2 arguments")
        value = self.date_value(node.args[0], variables, "EOMONTH")
        months = _integer_literal(node.args[1], "EOMONTH") + 1
        text = (f"CAST(DATEADD(day, -1, DATEADD(month, {months}, "
                f"DATE_TRUNC('month', {value.text}))) AS DATE)")
        return _Sql(text, _ATOM, False, _DATE)

    # --- row context ------------------------------------------------------------

    def row_column(self, name):
        entry = self.row_columns.get(name.upper())
        if entry is None:
            raise _RefusedError(
                f"[{name}] is not a column of '{self.row_table}' (a measure in a calculated "
                "column is evaluated with context transition, which a field expression "
                "cannot express)"
            )
        column_name, column = entry
        if column.dax is not None:
            return self.calculated_column(column_name, column)
        if column.kind is None:
            raise _RefusedError(
                f"column [{column_name}] has a type that is a later tier for calculated "
                "columns"
            )
        if not _is_simple_identifier(column.source):
            raise _RefusedError(
                f"column [{column_name}] reads a source column whose name needs SQL quoting, "
                "a later tier"
            )
        return _Sql(column.source, _ATOM, column.nullable, column.kind)

    def calculated_column(self, name, column):
        key = ("column", name.upper())
        if key in self.expanding:
            raise _RefusedError(f"calculated column [{name}] references itself")
        self.expanding.append(key)
        try:
            tree = parse_dax(column.dax)
        except DaxParseError as error:
            self.expanding.pop()
            raise _RefusedError(
                f"referenced column [{name}] could not be parsed ({error})"
            ) from None
        try:
            return self.translate(tree, {})
        except _RefusedError as refusal:
            if str(refusal).startswith("referenced column ["):
                raise
            raise _RefusedError(
                f"referenced column [{name}] is not translated: {refusal}"
            ) from None
        finally:
            self.expanding.pop()

    # --- composition ------------------------------------------------------------

    def measure_reference(self, name):
        key = name.upper()
        if key not in self.measures:
            raise _RefusedError(
                f"[{name}] is not a measure of this model (an unqualified column is not "
                "translated)"
            )
        if key in self.expanding:
            raise _RefusedError(f"measure [{name}] references itself")
        self.expanding.append(key)
        try:
            tree = parse_dax(self.measures[key])
        except DaxParseError as error:
            self.expanding.pop()
            raise _RefusedError(
                f"referenced measure [{name}] could not be parsed ({error})"
            ) from None
        try:
            return self.translate(tree, {})
        except _RefusedError as refusal:
            if str(refusal).startswith("referenced measure ["):
                # Already names the measure where it failed; wrapping it again at every
                # level would grow the diagnostic with the length of the chain.
                raise
            raise _RefusedError(
                f"referenced measure [{name}] is not translated: {refusal}"
            ) from None
        finally:
            self.expanding.pop()

    def var_block(self, node, variables):
        scope = dict(variables)
        for name, value in node.bindings:
            scope[name.upper()] = (value, dict(scope))
        return self.translate(node.body, scope)

    def variable(self, name, variables):
        binding = variables.get(name.upper())
        if binding is None:
            raise _RefusedError(f"'{name}' is not a variable defined in this measure")
        value, scope = binding
        return self.translate(value, scope)


#: DAX logic functions and the translator method for each.
_LOGIC_FUNCTIONS = {
    "IF": "if_",
    "IF.EAGER": "if_",  # same result as IF; only the evaluation order differs
    "SWITCH": "switch",
    "COALESCE": "coalesce",
    "BLANK": "blank",
    "ISBLANK": "isblank",
    "TRUE": "true_false",
    "FALSE": "true_false",
    "AND": "and_or",
    "OR": "and_or",
    "NOT": "not_",
}


def _call(name):
    """Render a same-named SQL function call."""
    return lambda texts, args: (f"{name}({', '.join(texts)})", _ATOM)


def _mod(texts, args):
    # DAX MOD takes the sign of the divisor and is documented as n - d * INT(n / d);
    # SQL MOD / % takes the sign of the dividend, so the identity is emitted instead.
    # The division is forced to be true division (an engine may truncate n / d for
    # integers, which would change the result).
    n, d = texts
    divisor = _wrap(args[1], _MUL, strict=True)
    return f"{_wrap(args[0], _ADD)} - {divisor} * FLOOR(CAST({n} AS DOUBLE) / {divisor})", _ADD


def _trunc(texts, args):
    digits = texts[1] if len(texts) == 2 else "0"
    return f"TRUNC({texts[0]}, {digits})", _ATOM


def _round(texts, args):
    # DAX ROUND breaks ties half away from zero, and so does the spec's ROUND(x, d)
    # (expression_language.md, Numeric Semantics), so the call is the same.
    return f"ROUND({texts[0]}, {texts[1]})", _ATOM


def _quotient(texts, args):
    # QUOTIENT returns the integer portion of the division, truncated toward zero; the
    # division is forced to be true division, as for MOD.
    divisor = _wrap(args[1], _MUL, strict=True)
    return f"TRUNC(CAST({texts[0]} AS DOUBLE) / {divisor}, 0)", _ATOM


def _log(texts, args):
    # DAX LOG(number, base) defaults to base 10; the spec writes LOG(base, x).
    if len(texts) == 1:
        return f"LOG10({texts[0]})", _ATOM
    return f"LOG({texts[1]}, {texts[0]})", _ATOM


#: Math functions translated for never-BLANK arguments: name -> (arities, renderer).
_MATH_FUNCTIONS = {
    "ABS": ({1}, _call("ABS")),
    "SIGN": ({1}, _call("SIGN")),
    "SQRT": ({1}, _call("SQRT")),
    "POWER": ({2}, _call("POWER")),
    "EXP": ({1}, _call("EXP")),
    "LN": ({1}, _call("LN")),
    "LOG": ({1, 2}, _log),
    "LOG10": ({1}, _call("LOG10")),
    "INT": ({1}, _call("FLOOR")),  # INT rounds down to the nearest integer
    "TRUNC": ({1, 2}, _trunc),
    "ROUND": ({2}, _round),
    "ROUNDDOWN": ({2}, _trunc),  # toward zero, like TRUNC
    "QUOTIENT": ({2}, _quotient),
    "MOD": ({2}, _mod),
    "PI": ({0}, _call("PI")),
    "SIN": ({1}, _call("SIN")),
    "COS": ({1}, _call("COS")),
    "TAN": ({1}, _call("TAN")),
    "ASIN": ({1}, _call("ASIN")),
    "ACOS": ({1}, _call("ACOS")),
    "ATAN": ({1}, _call("ATAN")),
    "DEGREES": ({1}, _call("DEGREES")),
    "RADIANS": ({1}, _call("RADIANS")),
}

#: Math functions refused for a semantic reason more specific than "not yet".
_MATH_REFUSED = {
    "ROUNDUP": "ROUNDUP (away from zero) is a later tier",
    "CEILING": "DAX CEILING rounds to a multiple of a significance, unlike SQL CEIL",
    "FLOOR": "DAX FLOOR rounds to a multiple of a significance, unlike SQL FLOOR",
}


#: DAX text functions and the translator method for each.
_TEXT_FUNCTIONS = {
    "LEFT": "left_right",
    "RIGHT": "left_right",
    "MID": "mid",
    "LEN": "length",
    "UPPER": "letter_case",
    "LOWER": "letter_case",
    "SUBSTITUTE": "substitute",
    "CONCATENATE": "concatenate",
}

#: Text functions refused for a semantic reason more specific than "not yet".
_TEXT_REFUSED = {
    "TRIM": "DAX TRIM also collapses inner runs of spaces; the spec's TRIM removes only "
    "leading and trailing whitespace, and the spec does not fix whether REGEXP_REPLACE "
    "replaces every match",
    "EXACT": "DAX EXACT is case-sensitive; whether a SQL text comparison is depends on "
    "the engine's collation, which the spec does not fix",
    "VALUE": "DAX VALUE raises an error on text that is not a number and parses by "
    "locale; CAST and TRY_CAST each differ from that",
    "FIND": "DAX FIND raises an error when the text is not found (unless NotFoundValue "
    "is given); POSITION returns 0",
    "SEARCH": "DAX SEARCH is case-insensitive with wildcards and raises an error when the "
    "text is not found; POSITION is neither",
    "CONTAINSSTRING": "DAX CONTAINSSTRING is case-insensitive and supports wildcards",
}


#: DAX date functions and the translator method for each.
_DATE_FUNCTIONS = {
    "YEAR": "date_part",
    "QUARTER": "date_part",
    "MONTH": "date_part",
    "DAY": "date_part",
    "HOUR": "date_part",
    "MINUTE": "date_part",
    "SECOND": "date_part",
    "DATE": "date_literal",
    "EDATE": "edate",
    "EOMONTH": "eomonth",
}

#: Date functions refused for a semantic reason more specific than "not yet".
_DATE_REFUSED = {
    "DATEDIFF": "DAX DATEDIFF counts the interval boundaries crossed; whether the spec's "
    "DATEDIFF counts boundaries or whole elapsed units is not specified, and engines "
    "differ (31 December to 1 January is 1 month or 0)",
}


#: X-iterators: name -> (SQL aggregate template, kinds of row expression it accepts).
_ITERATORS = {
    "SUMX": ("SUM({})", frozenset({_NUM})),
    "AVERAGEX": ("AVG({})", frozenset({_NUM})),
    "MINX": ("MIN({})", frozenset({_NUM, _DATE, _DATETIME})),
    "MAXX": ("MAX({})", frozenset({_NUM, _DATE, _DATETIME})),
    # DAX COUNTX counts the non-blank results that are not TRUE/FALSE, as COUNT does.
    "COUNTX": ("COUNT({})", frozenset({_NUM, _TEXT, _DATE, _DATETIME})),
    "MEDIANX": ("MEDIAN({})", frozenset({_NUM})),
    "PERCENTILEX.INC": (None, frozenset({_NUM})),
    "STDEVX.S": ("STDDEV_SAMP({})", frozenset({_NUM})),
    "STDEVX.P": ("STDDEV_POP({})", frozenset({_NUM})),
    "VARX.S": ("VAR_SAMP({})", frozenset({_NUM})),
    "VARX.P": ("VAR_POP({})", frozenset({_NUM})),
}


def _aggregate_kind_reason(function, kind):
    if kind == _UNTYPED:
        return (f"{function} over a column whose type the model does not give is not "
                "translated (whether DAX accepts its values cannot be checked)")
    if function == "COUNT" and kind == _BOOL:
        return ("DAX COUNT does not support TRUE/FALSE values, so it is not SQL COUNT over "
                "a boolean column (COUNTA is)")
    return f"{function} over a {kind} column is not translated"


def _integer_literal(node, function):
    """An integer literal argument, optionally negated, as an int."""
    negate = isinstance(node, Unary) and node.op == "-"
    operand = node.operand if isinstance(node, Unary) and node.op in ("+", "-") else node
    if isinstance(operand, Number) and _INTEGER.fullmatch(operand.text):
        return -int(operand.text) if negate else int(operand.text)
    raise _RefusedError(f"{function} is translated only with an integer literal number of months")


def _count(node, function, minimum):
    """A count or position argument: an integer literal of at least ``minimum``."""
    if isinstance(node, Number) and _INTEGER.fullmatch(node.text) and int(node.text) >= minimum:
        return str(int(node.text))
    raise _RefusedError(
        f"{function} is translated only with integer literal counts and positions "
        f"(at least {minimum}); DAX raises an error on a negative one"
    )


def _arity_text(arities):
    return " or ".join(str(n) for n in sorted(arities))


def _is_true(node):
    """True for the literal TRUE / TRUE() that selects the SWITCH(TRUE, ...) form."""
    return (isinstance(node, Name) and node.name.upper() == "TRUE") or (
        isinstance(node, Call) and node.name == "TRUE" and not node.args
    )


def _common_kind(fragments):
    """The single kind of the non-BLANK fragments, or refuse a mix (e.g. text and number)."""
    kinds = {f.kind for f in fragments if f.kind != _BLANK}
    if kinds == _DATE_KINDS:
        # A date and a datetime combine as a datetime (a date is its midnight).
        return _DATETIME
    if len(kinds) > 1:
        mixed = ", ".join(sorted(kinds))
        raise _RefusedError(f"results of different types ({mixed}) are a later tier")
    return kinds.pop() if kinds else _BLANK


def _column_kind(datatype):
    """The kind of SUM / AVERAGE / MIN / MAX over a column of the given Ossie datatype."""
    return _KIND_OF_DATATYPE.get(datatype, _UNTYPED)


def _is_simple_identifier(name):
    return bool(name) and _IDENTIFIER.fullmatch(name) is not None


def _sql_number(text):
    mantissa, exponent = _EXPONENT.match(text).groups()
    if mantissa.startswith("."):
        mantissa = "0" + mantissa
    if mantissa.endswith("."):
        mantissa += "0"
    return mantissa + exponent


def _is_nonzero_number(node):
    return isinstance(node, Number) and float(node.text) != 0


def _wrap(fragment, precedence, strict=False):
    """Parenthesise ``fragment`` if it binds more loosely than ``precedence``.

    ``strict`` is for the right operand of a left-associative operator, where equal
    precedence also needs parentheses (``a - (b - c)``).
    """
    if fragment.precedence < precedence or (strict and fragment.precedence == precedence):
        return f"({fragment.text})"
    return fragment.text


def _blank_as_zero(fragment, strict=False):
    if fragment.nullable:
        return f"COALESCE({fragment.text}, 0)"
    return _wrap(fragment, _ADD, strict=strict)
