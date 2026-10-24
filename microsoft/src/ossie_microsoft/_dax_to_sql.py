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
* Numeric literals, parentheses, unary ``+``, and ``-`` on a numeric literal.
* Measure references ``[Other Measure]``, expanded inline. At the top level of a
  measure there is no row context, so a referenced measure evaluates in the same
  filter context as the measure that references it, and inlining is exact.
* ``VAR`` / ``RETURN``, by substituting each variable where it is used (a variable is
  evaluated once, in the context where it is defined, which is the same context).

What is refused (and why, so the refusal is defensible)
-------------------------------------------------------
* ``COUNT`` and ``DISTINCTCOUNT``. DAX ``COUNT`` documents ``TRUE``/``FALSE``
  values as unsupported, so it is not the equivalent of SQL ``COUNT(x)`` for an
  arbitrary column, and ``DISTINCTCOUNT`` counts BLANK as a distinct value where
  SQL ``COUNT(DISTINCT x)`` excludes NULL. These are refused rather than guessed.
  Their counterparts ``COUNTA`` and ``DISTINCTCOUNTNOBLANK`` are what
  ``_sql_to_dax`` emits for SQL ``COUNT`` and ``COUNT(DISTINCT)``, and are translated.
  As ``_sql_to_dax`` notes, a DAX count over an empty set is BLANK where SQL gives 0;
  that convention is accepted in both directions.
* The ``/`` operator over anything but a non-zero literal: DAX returns Infinity or
  NaN when dividing by zero or BLANK, which has no portable form (``DIVIDE`` does).
* ``DIVIDE`` with an alternate result, ``^``, ``&``, comparisons, logical operators,
  and ``-`` applied to an expression: later tiers.
* Every other function, with the reason ``_dax_catalog`` records for it (a later
  tier of this converter, a feature the specification does not define yet, or no
  portable form at all).
* A column that is not fully qualified as ``Table[Column]``, a column that does not
  resolve to exactly one dataset field, and a table or column name that would need
  SQL quoting.

One reading is shared with ``_sql_to_dax`` rather than settled by the specification:
``OSSIE_SQL_2026`` division is taken to be true division, as DAX division is.

This module performs no I/O and holds no model state. The caller supplies a
``resolve`` callback mapping a DAX (table, column) to the Ossie (dataset, field)
names, and optionally the model's measures for measure references.
"""

from __future__ import annotations

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

#: Functions whose DAX semantics differ from the SQL function of the same name, so a
#: generic catalog reason would understate why they are refused.
_SEMANTIC_MISMATCH = {
    "COUNT": "DAX COUNT does not support TRUE/FALSE values, so it is not the equivalent "
    "of SQL COUNT(x) for an arbitrary column (COUNTA is)",
    "DISTINCTCOUNT": "DAX DISTINCTCOUNT counts BLANK as a distinct value; SQL "
    "COUNT(DISTINCT x) excludes NULL, so the two differ by one on a nullable column",
}

#: Every DAX function this module emits a translation for.
TRANSLATED_FUNCTIONS = frozenset(_AGGREGATES) | {"DIVIDE"}

# A simple identifier: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Precedence of an emitted SQL fragment, for parenthesising only where needed.
_ADD, _MUL, _ATOM = 1, 2, 3


class _RefusedError(Exception):
    """Raised inside the translator; its message is the refusal reason."""


@dataclass(frozen=True)
class _Sql:
    text: str
    precedence: int
    nullable: bool


def translate_measure(expression, resolve, measures=None):
    """Translate a DAX measure to an OSSIE_SQL expression.

    ``resolve`` maps a DAX ``(table, column)`` to the Ossie ``(dataset, field)``
    names, or returns None when the column does not resolve to exactly one field.
    ``measures`` maps measure names to their DAX, for expanding measure references;
    without it a measure reference is refused.

    Returns ``(sql, None)`` on success or ``(None, reason)`` on refusal. The
    caller keeps the original DAX and attaches ``reason`` as a diagnostic when a
    measure is refused.
    """
    try:
        tree = parse_dax(expression)
    except DaxParseError as error:
        return None, f"the DAX could not be parsed ({error})"
    translator = _Translator(resolve, measures)
    try:
        return translator.translate(tree, {}).text, None
    except _RefusedError as refusal:
        return None, str(refusal)


class _Translator:
    def __init__(self, resolve, measures):
        self.resolve = resolve
        self.measures = {name.upper(): dax for name, dax in (measures or {}).items()}
        self.expanding = []

    def translate(self, node, variables):
        if isinstance(node, Number):
            return _Sql(node.text, _ATOM, nullable=False)
        if isinstance(node, Call):
            return self.call(node, variables)
        if isinstance(node, Binary):
            return self.binary(node, variables)
        if isinstance(node, Unary):
            return self.unary(node, variables)
        if isinstance(node, Bracket):
            return self.measure_reference(node.name)
        if isinstance(node, Name):
            return self.variable(node.name, variables)
        if isinstance(node, Var):
            return self.var_block(node, variables)
        if isinstance(node, Column):
            raise _RefusedError(
                f"a bare column reference ('{node.table}'[{node.column}]) is not a measure; "
                "only aggregates over columns are translated"
            )
        raise _RefusedError(f"{type(node).__name__.lower()} expressions are a later tier")

    # --- functions --------------------------------------------------------------

    def call(self, node, variables):
        if node.name in _AGGREGATES:
            return self.aggregate(node)
        if node.name == "DIVIDE":
            return self.divide(node, variables)
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
        dataset, field = resolved
        if not _is_simple_identifier(dataset) or not _is_simple_identifier(field):
            raise _RefusedError("a dataset or field name needing SQL quoting is a later tier")
        return _Sql(_AGGREGATES[node.name].format(f"{dataset}.{field}"), _ATOM, nullable=True)

    def divide(self, node, variables):
        if len(node.args) == 3:
            raise _RefusedError("DIVIDE with an alternate result is a later tier")
        if len(node.args) != 2:
            raise _RefusedError("DIVIDE takes a numerator and a denominator")
        numerator = self.translate(node.args[0], variables)
        denominator = self.translate(node.args[1], variables)
        text = f"{_wrap(numerator, _MUL)} / NULLIF({denominator.text}, 0)"
        return _Sql(text, _MUL, nullable=True)

    # --- operators --------------------------------------------------------------

    def binary(self, node, variables):
        if node.op == "*":
            left = self.translate(node.left, variables)
            right = self.translate(node.right, variables)
            text = f"{_wrap(left, _MUL)} * {_wrap(right, _MUL, strict=True)}"
            return _Sql(text, _MUL, nullable=left.nullable or right.nullable)
        if node.op == "/":
            if not _is_nonzero_number(node.right):
                raise _RefusedError(
                    "the / operator returns Infinity or NaN on a zero or BLANK divisor, "
                    "which has no portable form; DIVIDE is translated"
                )
            left = self.translate(node.left, variables)
            text = f"{_wrap(left, _MUL)} / {node.right.text}"
            return _Sql(text, _MUL, nullable=left.nullable)
        if node.op in ("+", "-"):
            return self.additive(node, variables)
        raise _RefusedError(f"the {node.op} operator is a later tier")

    def additive(self, node, variables):
        left = self.translate(node.left, variables)
        right = self.translate(node.right, variables)
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
            return _Sql(f"-{node.operand.text}", _ATOM, nullable=False)
        raise _RefusedError(f"the unary {node.op} operator on an expression is a later tier")

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


def _is_simple_identifier(name):
    return bool(name) and _IDENTIFIER.fullmatch(name) is not None


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
