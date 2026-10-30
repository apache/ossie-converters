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

"""Tableau function catalogue. Each entry is one Snowflake SQL template; the same template drives both directions.

Placeholders: `__a0`, `__a1`... are operand expressions; `UNIT_` is a date-part name (a Tableau string such as 'month').
status: direct (same result), approximated (result can differ at the edges; `note` says how), unmappable (listed so docs are complete).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Override:
    """A template written in the target dialect's own SQL, replacing the Snowflake one for that warehouse."""
    sql: str  # empty means unmappable on that warehouse
    status: str = "direct"
    note: str = ""
    result: str = ""  # empty keeps the entry's result type


@dataclass(frozen=True)
class Entry:
    name: str  # Tableau function name
    arity: tuple[int, ...]
    sql: str  # Snowflake template; empty for unmappable
    status: str
    result: str  # num | str | bool | date | any
    note: str = ""
    group: str = ""
    overrides: dict = field(default_factory=dict)  # sqlglot dialect name -> Override
    sql_dialect: str = "snowflake"  # dialect `sql` is written in (set by for_dialect)


_BQ = {
    "MEDIAN": Override("", note="BigQuery has no exact MEDIAN aggregate (APPROX_QUANTILES is approximate)"),
    "DATETRUNC": Override("DATETIME_TRUNC(CAST(__a1 AS DATETIME), UNIT_)", "approximated",
                          note="a date input comes back as a DATETIME; week starts on Sunday, which matches Tableau's default start-of-week only"),
    "DATEPART": Override("EXTRACT(UNIT_ FROM CAST(__a1 AS DATETIME))", "approximated",
                         note="weekday is Sunday=1 as in Tableau; BigQuery week numbers start at 0 and follow Sunday weeks, which can differ from Tableau"),
    "DATEADD": Override("DATETIME_ADD(CAST(__a2 AS DATETIME), INTERVAL __a1 UNIT_)", "approximated",
                        note="a date input comes back as a DATETIME"),
    "DATEDIFF": Override("DATETIME_DIFF(CAST(__a2 AS DATETIME), CAST(__a1 AS DATETIME), UNIT_)", "approximated",
                         note="week boundaries are Sunday-based; Tableau follows the start-of-week setting"),
    "YEAR": Override("EXTRACT(YEAR FROM __a0)"),
    "MONTH": Override("EXTRACT(MONTH FROM __a0)"),
    "DAY": Override("EXTRACT(DAY FROM __a0)"),
    "MAKEPOINT": Override("ST_GEOGPOINT(__a1, __a0)", note="Tableau takes (latitude, longitude); ST_GEOGPOINT takes (longitude, latitude), so the arguments are swapped"),
    "DATEPARSE": Override("SAFE.PARSE_DATETIME(__a0, __a1)", "approximated",
                          note="the format must be a literal built from yyyy yy MMMM MMM MM dd HH hh mm ss a and separators; a value that does not match gives NULL"),
    "DATENAME": Override("FORMAT_DATETIME('%B', CAST(__a1 AS DATETIME))", "approximated",
                         note="literal parts with a verified FORMAT_DATETIME token: year→%Y, month→%B, day→%d, hour→%H, minute→%M, second→%S; other parts are not translated"),
    "NOW": Override("CURRENT_DATETIME()", "approximated", note="BigQuery evaluates in UTC; Tableau uses the server time zone"),
    "CONTAINS": Override("(INSTR(__a0, __a1) > 0)"),
    "STARTSWITH": Override("STARTS_WITH(__a0, __a1)"),
    "ENDSWITH": Override("ENDS_WITH(__a0, __a1)"),
    "SPLIT": Override("SPLIT(__a0, __a1)[SAFE_OFFSET(__a2 - 1)]", "approximated", note="a non-literal token number is passed through as is; a negative one gives NULL (a literal negative token counts from the end exactly)"),
    "LOG": Override("LOG10(__a0)", note="Tableau LOG is base 10"),
    "FIND": Override("INSTR(__a0, __a1, __a2)", note="1-based position, 0 when absent, as in Tableau"),
    "REGEXP_REPLACE": Override("REGEXP_REPLACE(__a0, __a1, __a2)", "approximated", note="regex flavours differ between Tableau (ICU) and BigQuery (RE2)"),
    "REGEXP_EXTRACT": Override("REGEXP_EXTRACT(__a0, __a1)", "approximated",
                               note="regex flavours differ between Tableau (ICU) and BigQuery (RE2); BigQuery needs exactly one capture group"),
    "ISOWEEKDAY": Override("(MOD(EXTRACT(DAYOFWEEK FROM __a0) + 5, 7) + 1)"),
    "PI": Override("ACOS(-1)"),
    "DATETIME": Override("CAST(__a0 AS DATETIME)", "approximated",
                        note="BigQuery casts only ISO-8601 strings; a m/d/yyyy string literal is rewritten to ISO, and non-literal text tries ISO then m/d/yyyy (else NULL)"),
    "DATE": Override("CAST(__a0 AS DATE)", "approximated",
                     note="BigQuery casts only ISO-8601 strings; a m/d/yyyy string literal is rewritten to ISO, and non-literal text tries ISO then m/d/yyyy (else NULL)"),
}

# Entries whose Snowflake template transpiles to BigQuery unchanged (checked by tests/test_calc_bigquery.py).
PORTABLE: dict[str, frozenset[str]] = {"bigquery": frozenset((
    "SUM AVG MIN MAX COUNT COUNTD STDEV VAR ATTR ZN IFNULL ISNULL TODAY LEFT RIGHT MID LEN UPPER LOWER TRIM REPLACE ROUND ABS "
    "CEILING FLOOR POWER SQRT LN EXP SIGN INT FLOAT STR DATE IIF").split())}


def _e(group, name, arity, sql, status="direct", result="any", note="", **overrides):
    if name in _BQ:
        overrides["bigquery"] = _BQ[name]
    return Entry(name, tuple(arity) if isinstance(arity, (list, tuple)) else (arity,), sql, status, result, note, group, overrides)


CATALOG: list[Entry] = [
    _e("aggregate", "SUM", 1, "SUM(__a0)", result="num"),
    _e("aggregate", "AVG", 1, "AVG(__a0)", result="num"),
    _e("aggregate", "MIN", 1, "MIN(__a0)"),
    _e("aggregate", "MAX", 1, "MAX(__a0)"),
    _e("aggregate", "COUNT", 1, "COUNT(__a0)", result="num"),
    _e("aggregate", "COUNTD", 1, "COUNT(DISTINCT __a0)", result="num"),
    _e("aggregate", "MEDIAN", 1, "MEDIAN(__a0)", result="num"),
    _e("aggregate", "STDEV", 1, "STDDEV_SAMP(__a0)", result="num"),
    _e("aggregate", "VAR", 1, "VARIANCE(__a0)", result="num"),
    _e("aggregate", "ATTR", 1, "MIN(__a0)", "approximated", note="Tableau returns * when the values differ; MIN returns the smallest value"),
    _e("null", "ZN", 1, "COALESCE(__a0, 0)", result="num"),
    _e("null", "IFNULL", 2, "COALESCE(__a0, __a1)"),
    _e("null", "ISNULL", 1, "(__a0 IS NULL)", result="bool"),
    _e("date", "DATETRUNC", 2, "DATE_TRUNC('UNIT_', __a1)", "approximated", "date",
       "week starts on the Tableau data source's start-of-week setting (Sunday by default); Snowflake uses WEEK_START (Monday by default)"),
    _e("date", "DATEPART", 2, "DATE_PART(UNIT_, __a1)", "approximated", "num", "weekday numbering and week start differ between Tableau and Snowflake"),
    _e("date", "DATEADD", 3, "DATEADD(UNIT_, __a1, __a2)", result="date"),
    _e("date", "DATEDIFF", 3, "DATEDIFF(UNIT_, __a1, __a2)", "approximated", "num", "week boundaries follow the start-of-week setting in Tableau"),
    _e("date", "YEAR", 1, "YEAR(__a0)", result="num"),
    _e("date", "MONTH", 1, "MONTH(__a0)", result="num"),
    _e("date", "DAY", 1, "DAY(__a0)", result="num"),
    _e("spatial", "MAKEPOINT", 2, "ST_MAKEPOINT(__a1, __a0)", note="Tableau takes (latitude, longitude); ST_MAKEPOINT takes (longitude, latitude), so the arguments are swapped"),
    _e("date", "DATEPARSE", 2, "TRY_TO_TIMESTAMP(__a1, __a0)", "approximated", "date",
       "the format must be a literal built from yyyy yy MMMM MMM MM dd HH hh mm ss a and separators; a value that does not match gives NULL in Snowflake"),
    _e("date", "DATENAME", 2, "TO_CHAR(__a1, 'MMMM')", "approximated", "str",
       "literal parts with a verified TO_CHAR token: year→YYYY, month→MMMM, day→DD, hour→HH24, minute→MI, second→SS; other parts have no verified Snowflake format"),
    _e("date", "TODAY", 0, "CURRENT_DATE()", result="date"),
    _e("date", "NOW", 0, "CURRENT_TIMESTAMP()", "approximated", "date", "time zone is the session time zone in Snowflake and the server time zone in Tableau"),
    _e("string", "LEFT", 2, "LEFT(__a0, __a1)", result="str"),
    _e("string", "RIGHT", 2, "RIGHT(__a0, __a1)", result="str"),
    _e("string", "MID", 3, "SUBSTRING(__a0, __a1, __a2)", result="str"),
    _e("string", "LEN", 1, "LENGTH(__a0)", result="num"),
    _e("string", "CONTAINS", 2, "CONTAINS(__a0, __a1)", result="bool"),
    _e("string", "STARTSWITH", 2, "STARTSWITH(__a0, __a1)", result="bool"),
    _e("string", "ENDSWITH", 2, "ENDSWITH(__a0, __a1)", result="bool"),
    _e("string", "UPPER", 1, "UPPER(__a0)", result="str"),
    _e("string", "LOWER", 1, "LOWER(__a0)", result="str"),
    _e("string", "TRIM", 1, "TRIM(__a0)", result="str"),
    _e("string", "REPLACE", 3, "REPLACE(__a0, __a1, __a2)", result="str"),
    _e("string", "SPLIT", 3, "SPLIT_PART(__a0, __a1, __a2)", result="str"),
    _e("math", "ROUND", (1, 2), "ROUND(__a0, __a1)", "approximated", "num", "Tableau rounds half away from zero, as Snowflake does; negative digits are not translated"),
    _e("math", "ABS", 1, "ABS(__a0)", result="num"),
    _e("math", "CEILING", 1, "CEIL(__a0)", result="num"),
    _e("math", "FLOOR", 1, "FLOOR(__a0)", result="num"),
    _e("math", "POWER", 2, "POWER(__a0, __a1)", result="num"),
    _e("math", "SQRT", 1, "SQRT(__a0)", result="num"),
    _e("math", "LOG", 1, "LOG(10, __a0)", result="num", note="Tableau LOG is base 10"),
    _e("math", "LN", 1, "LN(__a0)", result="num"),
    _e("math", "EXP", 1, "EXP(__a0)", result="num"),
    _e("math", "SIGN", 1, "SIGN(__a0)", result="num"),
    _e("math", "PI", 0, "PI()", result="num"),
    _e("string", "FIND", (2, 3), "CHARINDEX(__a1, __a0, __a2)", result="num", note="1-based position, 0 when absent, as in Tableau"),
    _e("string", "REGEXP_REPLACE", 3, "REGEXP_REPLACE(__a0, __a1, __a2)", "approximated", "str", "regex flavours differ between Tableau (ICU) and Snowflake (POSIX ERE-like)"),
    _e("string", "REGEXP_EXTRACT", 2, "REGEXP_SUBSTR(__a0, __a1, 1, 1, 'e', 1)", "approximated", "str", "regex flavours differ between Tableau (ICU) and Snowflake"),
    _e("date", "ISOWEEKDAY", 1, "DAYOFWEEKISO(__a0)", result="num"),
    _e("cast", "INT", 1, "CAST(TRUNC(__a0) AS INTEGER)", result="num", note="Tableau INT truncates toward zero"),
    _e("cast", "FLOAT", 1, "CAST(__a0 AS DOUBLE)", result="num"),
    _e("cast", "STR", 1, "CAST(__a0 AS VARCHAR)", result="str"),
    _e("cast", "DATE", 1, "CAST(__a0 AS DATE)", result="date"),
    _e("cast", "DATETIME", 1, "CAST(__a0 AS TIMESTAMP)", result="date"),
    _e("logic", "IIF", 3, "IFF(__a0, __a1, __a2)"),
    _e("unmappable", "RAWSQL_REAL", (), "", "unmappable", note="pass-through SQL; never emitted as RAWSQL (use the rawsql loss)"),
    _e("unmappable", "REGEXP_MATCH", 2, "", "unmappable", note="regex flavours differ between Tableau (ICU) and Snowflake"),
    _e("unmappable", "MAKEDATE", 3, "", "unmappable", note="no portable equivalent without a dialect-specific function"),
]

BY_NAME: dict[str, Entry] = {e.name: e for e in CATALOG}

_UNVERIFIED = "no verified {dialect} translation yet"


def for_dialect(entry: Entry, dialect: str) -> Entry:
    """The entry as it applies to a target dialect: its override, the Snowflake template when it is known to be portable,
    otherwise an unmappable entry (never a guess)."""
    if dialect == "snowflake":
        return entry
    ov = entry.overrides.get(dialect)
    if ov is not None:
        return replace(entry, sql=ov.sql, status=ov.status if ov.sql else "unmappable", note=ov.note, result=ov.result or entry.result,
                       sql_dialect=dialect)
    if entry.status == "unmappable" or entry.name in PORTABLE.get(dialect, ()):
        return replace(entry, sql_dialect="snowflake")
    return replace(entry, sql="", status="unmappable", note=_UNVERIFIED.format(dialect=dialect))

