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

and math functions, but only for arguments that can never be BLANK (a literal,
``COALESCE(x, 0)``, a variable holding one), because Microsoft does not document what
they return for BLANK: ``ABS``, ``SIGN``, ``SQRT``, ``POWER``, ``EXP``, ``LN``, ``LOG10``,
``PI``, trigonometry, ``DEGREES`` / ``RADIANS``; ``LOG(number, base)`` -> ``LOG(base,
number)`` (base 10 when omitted, ``LOG10``); ``INT`` -> ``FLOOR``; ``TRUNC`` and ``ROUNDDOWN``
-> ``TRUNC(x, d)``; ``QUOTIENT`` -> ``TRUNC(CAST(n AS DOUBLE) / d, 0)``; and ``MOD``, whose
result takes the sign of the divisor, -> ``n - d * FLOOR(CAST(n AS DOUBLE) / d)`` (the
documented identity; SQL ``MOD`` takes the sign of the dividend). ``MOD`` and ``QUOTIENT``
force true division so an engine that truncates integer division gives the same result.

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
* ``DIVIDE`` with an alternate result, ``^``, ``&``, and ``-`` applied to an expression:
  later tiers.
* ``SWITCH`` matching a value (``SWITCH(x, 1, "a", ...)``): how BLANK matches a value is
  not documented, so only the ``SWITCH(TRUE, ...)`` form is translated.
* Logical operators over a boolean that may be BLANK: DAX's logic with BLANK
  (``TRUE AND BLANK`` is FALSE, ``BLANK OR BLANK`` is BLANK) is not SQL's NULL logic.
* Comparisons of text, and results that mix types (text in one branch, a number in
  another): later tiers.
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
    String,
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
TRANSLATED_FUNCTIONS = frozenset(_AGGREGATES) | frozenset(
    ("ABS", "SIGN", "SQRT", "POWER", "EXP", "LN", "LOG", "LOG10", "INT", "TRUNC",
     "ROUNDDOWN", "QUOTIENT", "MOD", "PI", "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
     "DEGREES", "RADIANS")
) | {
    "DIVIDE", "IF", "IF.EAGER", "SWITCH", "COALESCE", "BLANK", "ISBLANK",
    "TRUE", "FALSE", "AND", "OR", "NOT",
}

# A simple identifier: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Precedence of an emitted SQL fragment, for parenthesising only where needed.
_OR, _AND, _NOT, _CMP, _ADD, _MUL, _ATOM = 0, 1, 2, 3, 4, 5, 6

#: Fragment kinds. BLANK() is its own kind so it can stand in for a missing value of any type.
_NUM, _BOOL, _TEXT, _BLANK = "number", "boolean", "text", "blank"

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

    ``resolve`` maps a DAX ``(table, column)`` to the Ossie ``(dataset, field)``
    names, or returns None when the column does not resolve to exactly one field.
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


class _Translator:
    def __init__(self, resolve, measures):
        self.resolve = resolve
        self.measures = {name.upper(): dax for name, dax in (measures or {}).items()}
        self.expanding = []

    def translate(self, node, variables):
        if isinstance(node, Number):
            return _Sql(node.text, _ATOM, nullable=False)
        if isinstance(node, String):
            return _Sql("'" + node.value.replace("'", "''") + "'", _ATOM, False, _TEXT)
        if isinstance(node, Call):
            return self.call(node, variables)
        if isinstance(node, Binary):
            return self.binary(node, variables)
        if isinstance(node, Unary):
            return self.unary(node, variables)
        if isinstance(node, Bracket):
            return self.measure_reference(node.name)
        if isinstance(node, Name):
            if node.name.upper() in ("TRUE", "FALSE") and node.name.upper() not in variables:
                return _Sql(node.name.upper(), _ATOM, False, _BOOL)
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
        if node.name in _LOGIC_FUNCTIONS:
            return getattr(self, _LOGIC_FUNCTIONS[node.name])(node, variables)
        if node.name in _MATH_FUNCTIONS:
            return self.math(node, variables)
        if node.name in _MATH_REFUSED:
            raise _RefusedError(_MATH_REFUSED[node.name])
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
            text = f"{_wrap(left, _MUL)} / {node.right.text}"
            return _Sql(text, _MUL, nullable=left.nullable)
        if node.op in ("+", "-"):
            return self.additive(node, variables)
        if node.op in _COMPARISONS:
            return self.comparison(node, variables)
        if node.op == "==":
            return self.strict_equality(node, variables)
        if node.op in ("&&", "||"):
            return self.logical(node.op, node.left, node.right, variables)
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
            return _Sql(f"-{node.operand.text}", _ATOM, nullable=False)
        if node.op == "NOT":
            operand = self.boolean(node.operand, variables)
            return _Sql(f"NOT {_wrap(operand, _NOT)}", _NOT, False, _BOOL)
        raise _RefusedError(f"the unary {node.op} operator on an expression is a later tier")

    # --- logic and conditionals ----------------------------------------------

    def number(self, node, variables):
        """Translate an operand of arithmetic or a comparison, which must be numeric."""
        fragment = self.translate(node, variables)
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
        args = [self.number(arg, variables) for arg in node.args]
        if any(arg.nullable for arg in args):
            raise _RefusedError(
                f"{node.name} of a value that may be BLANK: its BLANK behaviour is not "
                "documented (wrap the argument in COALESCE(x, 0) to translate it)"
            )
        text, precedence = render([arg.text for arg in args], args)
        return _Sql(text, precedence, False)

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
    "ROUND": "DAX ROUND breaks ties half away from zero; the spec's ROUND does not say how "
    "ties are broken, so the result could differ on a tie",
    "ROUNDUP": "ROUNDUP (away from zero) is a later tier",
    "CEILING": "DAX CEILING rounds to a multiple of a significance, unlike SQL CEIL",
    "FLOOR": "DAX FLOOR rounds to a multiple of a significance, unlike SQL FLOOR",
}


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
    if len(kinds) > 1:
        mixed = ", ".join(sorted(kinds))
        raise _RefusedError(f"results of different types ({mixed}) are a later tier")
    return kinds.pop() if kinds else _BLANK


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
