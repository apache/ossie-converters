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
``_sql_to_dax``: it recognises the single-column aggregate measures whose
``OSSIE_SQL_2026`` equivalent is unambiguous, and refuses everything else.

The governing rule is the one ``_sql_to_dax`` states and ``#437`` argues for:
never emit a plausible-looking expression that was not authored for the target.
A measure this module declines is kept verbatim as DAX with a diagnostic; a
measure it mistranslated would deploy cleanly on another engine and quietly
return a wrong number. Refusal is the safe default; translation is the exception
that must be proven unambiguous.

What is translated
------------------
A single DAX aggregate over one fully qualified column, for the six aggregates
that reverse ``_sql_to_dax`` exactly, so an Ossie SQL model exported to Power BI
and re-imported round-trips to the same expression::

    SUM('Sales'[Amount])                 ->  SUM(Sales.Amount)
    AVERAGE('Sales'[Amount])             ->  AVG(Sales.Amount)
    MIN('Sales'[Amount])                 ->  MIN(Sales.Amount)
    MAX('Sales'[Amount])                 ->  MAX(Sales.Amount)
    COUNTA('Sales'[Customer])            ->  COUNT(Sales.Customer)
    DISTINCTCOUNTNOBLANK('Sales'[Cust])  ->  COUNT(DISTINCT Sales.Cust)

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
* ``COUNTROWS``, ``MEDIAN``, the statistical aggregates, iterators (``SUMX``),
  ``CALCULATE`` and the filter-context family, ``DIVIDE``, ``IF``, arithmetic,
  time intelligence, and anything with more than one argument or a nested call.
  These are later tiers or have no declaration-scoped portable form.
* A column that is not fully qualified as ``Table[Column]``, or a table or
  column name that is not a simple identifier. A name needing SQL quoting is
  left to a later tier so this first pass never has to get a quoting rule right.

This module performs no I/O and holds no model state. The caller supplies a
``resolve`` callback mapping a DAX (table, column) to the Ossie (dataset, field)
names, which is where model knowledge lives.
"""

from __future__ import annotations

import re

#: DAX aggregate -> OSSIE_SQL aggregate, for the exact reverses of _sql_to_dax.
#: COUNTA and DISTINCTCOUNTNOBLANK are what _sql_to_dax emits for SQL COUNT and
#: COUNT(DISTINCT); reversing those same functions keeps the round-trip exact.
_SIMPLE_AGGREGATES = {
    "SUM": "SUM",
    "AVERAGE": "AVG",
    "MIN": "MIN",
    "MAX": "MAX",
    "COUNTA": "COUNT",
}

#: DAX functions we recognise well enough to refuse with a specific reason,
#: rather than a generic "not translated", because they look translatable.
_KNOWN_UNSAFE = {
    "COUNT": "DAX COUNT does not support TRUE/FALSE values, so it is not the equivalent "
    "of SQL COUNT(x) for an arbitrary column (COUNTA is)",
    "DISTINCTCOUNT": "DAX DISTINCTCOUNT counts BLANK as a distinct value; SQL "
    "COUNT(DISTINCT x) excludes NULL, so the two differ by one on a nullable column",
    "COUNTROWS": "COUNTROWS counts rows, not a single column; it is a later tier",
    "MEDIAN": "MEDIAN is a later tier",
    "SUMX": "iterators evaluate per row and are a later tier",
    "AVERAGEX": "iterators evaluate per row and are a later tier",
    "CALCULATE": "CALCULATE manipulates filter context, which has no declaration-scoped form",
    "DIVIDE": "division between measures is a later tier",
}

# A simple identifier: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# One DAX function call: NAME ( ARG ). ARG is captured whole and validated
# separately so nested parentheses or extra arguments fail the ARG match.
_CALL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*)\)$", re.DOTALL)

# A fully qualified DAX column: Table[Column] or 'Table Name'[Column], where a
# quoted table escapes ' as '' and a column escapes ] as ]].
_QUALIFIED_COLUMN = re.compile(
    r"""^\s*
        (?:'((?:[^']|'')*)'|([A-Za-z_][A-Za-z0-9_]*))   # 1 quoted or 2 bare table
        \s*
        \[((?:[^\]]|\]\])*)\]                            # 3 column, ] escaped as ]]
        \s*$""",
    re.VERBOSE,
)


def _strip_measure(expression):
    """Normalise a measure body: drop a leading '=' and outer whitespace."""
    text = (expression or "").strip()
    if text.startswith("="):
        text = text[1:].strip()
    return text


def _parse_qualified_column(argument):
    """Return (table, column) for a fully qualified DAX column, else None."""
    match = _QUALIFIED_COLUMN.match(argument)
    if match is None:
        return None
    quoted_table, bare_table, column = match.groups()
    table = quoted_table.replace("''", "'") if quoted_table is not None else bare_table
    column = column.replace("]]", "]")
    return table, column


def _is_simple_identifier(name):
    return bool(name) and _IDENTIFIER.fullmatch(name) is not None


def _qualify(dataset, field):
    """Render an OSSIE_SQL ``dataset.field`` reference, or None if either name
    is not a simple identifier (a name needing quoting is a later tier)."""
    if not _is_simple_identifier(dataset) or not _is_simple_identifier(field):
        return None
    return f"{dataset}.{field}"


def translate_measure(expression, resolve):
    """Translate a single-column DAX aggregate measure to an OSSIE_SQL expression.

    ``resolve`` maps a DAX ``(table, column)`` to the Ossie ``(dataset, field)``
    names, or returns None when the column does not resolve to exactly one field.

    Returns ``(sql, None)`` on success or ``(None, reason)`` on refusal. The
    caller keeps the original DAX and attaches ``reason`` as a diagnostic when a
    measure is refused.
    """
    text = _strip_measure(expression)
    if not text:
        return None, "measure has no expression"

    call = _CALL.match(text)
    if call is None:
        return None, "only a single aggregate function call is translated"

    function = call.group(1).upper()
    argument = call.group(2).strip()

    distinct_target = function == "DISTINCTCOUNTNOBLANK"
    if function not in _SIMPLE_AGGREGATES and not distinct_target:
        reason = _KNOWN_UNSAFE.get(function, f"'{function}' is not a translated aggregate")
        return None, reason

    parsed = _parse_qualified_column(argument)
    if parsed is None:
        return None, (
            "only an aggregate over one fully qualified column (Table[Column]) is translated"
        )
    table, column = parsed

    resolved = resolve(table, column)
    if resolved is None:
        return None, f"column '{table}'[{column}] does not resolve to exactly one dataset field"
    dataset, field = resolved

    reference = _qualify(dataset, field)
    if reference is None:
        return None, "a dataset or field name needing SQL quoting is a later tier"

    if distinct_target:
        return f"COUNT(DISTINCT {reference})", None
    return f"{_SIMPLE_AGGREGATES[function]}({reference})", None
