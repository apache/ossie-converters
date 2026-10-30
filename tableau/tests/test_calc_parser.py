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

import pytest
import sqlglot

from ossie_tableau.calc_catalog import BY_NAME, CATALOG
from ossie_tableau.calc_parser import Untranslatable, emit_tableau, from_sql, translate

pytestmark = pytest.mark.unit

# one sample per catalogue entry: Tableau formula -> Snowflake SQL
SAMPLES = {
    "SUM": ("SUM([a])", "SUM(a)"), "AVG": ("AVG([a])", "AVG(a)"), "MIN": ("MIN([a])", "MIN(a)"), "MAX": ("MAX([a])", "MAX(a)"),
    "COUNT": ("COUNT([a])", "COUNT(a)"), "COUNTD": ("COUNTD([a])", "COUNT(DISTINCT a)"), "MEDIAN": ("MEDIAN([a])", "MEDIAN(a)"),
    "STDEV": ("STDEV([a])", "STDDEV(a)"), "VAR": ("VAR([a])", "VARIANCE(a)"), "ATTR": ("ATTR([a])", "MIN(a)"),
    "ZN": ("ZN([a])", "COALESCE(a, 0)"), "IFNULL": ("IFNULL([a], [b])", "COALESCE(a, b)"), "ISNULL": ("ISNULL([a])", "(a IS NULL)"),
    "DATETRUNC": ("DATETRUNC('month', [d])", "DATE_TRUNC('MONTH', d)"), "DATEPART": ("DATEPART('year', [d])", "DATE_PART(YEAR, d)"),
    "DATEADD": ("DATEADD('day', 3, [d])", "DATEADD(DAY, 3, d)"), "DATEDIFF": ("DATEDIFF('day', [d], [e])", "DATEDIFF(DAY, d, e)"),
    "YEAR": ("YEAR([d])", "YEAR(d)"), "MONTH": ("MONTH([d])", "MONTH(d)"), "DAY": ("DAY([d])", "DAY(d)"),
    "MAKEPOINT": ("MAKEPOINT([lat], [lon])", "ST_MAKEPOINT(lon, lat)"),
    "DATEPARSE": ("DATEPARSE('yyyy-MM-dd HH:mm:ss', [s])", "TRY_TO_TIMESTAMP(s, 'YYYY-MM-DD HH24:MI:SS')"),
    "DATENAME": ("DATENAME('month', [d])", "TO_CHAR(d, 'MMMM')"),
    "TODAY": ("TODAY()", "CURRENT_DATE"), "NOW": ("NOW()", "CURRENT_TIMESTAMP()"),
    "LEFT": ("LEFT([s], 2)", "LEFT(s, 2)"), "RIGHT": ("RIGHT([s], 2)", "RIGHT(s, 2)"), "MID": ("MID([s], 2, 3)", "SUBSTRING(s, 2, 3)"),
    "LEN": ("LEN([s])", "LENGTH(s)"), "CONTAINS": ("CONTAINS([s], 'x')", "CONTAINS(s, 'x')"),
    "STARTSWITH": ("STARTSWITH([s], 'x')", "STARTSWITH(s, 'x')"), "ENDSWITH": ("ENDSWITH([s], 'x')", "ENDSWITH(s, 'x')"),
    "UPPER": ("UPPER([s])", "UPPER(s)"), "LOWER": ("LOWER([s])", "LOWER(s)"), "TRIM": ("TRIM([s])", "TRIM(s)"),
    "REPLACE": ("REPLACE([s], 'a', 'b')", "REPLACE(s, 'a', 'b')"), "SPLIT": ("SPLIT([s], ',', 2)", "SPLIT_PART(s, ',', 2)"),
    "ROUND": ("ROUND([a], 2)", "ROUND(a, 2)"), "ABS": ("ABS([a])", "ABS(a)"), "CEILING": ("CEILING([a])", "CEIL(a)"),
    "FLOOR": ("FLOOR([a])", "FLOOR(a)"), "POWER": ("POWER([a], 2)", "POWER(a, 2)"), "SQRT": ("SQRT([a])", "SQRT(a)"),
    "LOG": ("LOG([a])", "LOG(10, a)"), "LN": ("LN([a])", "LN(a)"),
    "INT": ("INT([a])", "CAST(TRUNC(a) AS INT)"), "FLOAT": ("FLOAT([a])", "CAST(a AS DOUBLE)"), "STR": ("STR([a])", "CAST(a AS VARCHAR)"),
    "DATE": ("DATE([a])", "CAST(a AS DATE)"), "DATETIME": ("DATETIME([a])", "CAST(a AS TIMESTAMP)"),
    "EXP": ("EXP([a])", "EXP(a)"), "SIGN": ("SIGN([a])", "SIGN(a)"), "PI": ("PI()", "PI()"),
    "FIND": ("FIND([s], 'x')", "CHARINDEX('x', s, 1)"),
    "REGEXP_REPLACE": ("REGEXP_REPLACE([s], 'a', 'b')", "REGEXP_REPLACE(s, 'a', 'b')"),
    "REGEXP_EXTRACT": ("REGEXP_EXTRACT([s], '(a)')", "REGEXP_SUBSTR(s, '(a)', 1, 1, 'e', 1)"),
    "ISOWEEKDAY": ("ISOWEEKDAY([d])", "DAYOFWEEKISO(d)"),
    "IIF": ("IIF([a] > 1, 'x', 'y')", "IFF(a > 1, 'x', 'y')"),
}
UNMAPPABLE = [e.name for e in CATALOG if e.status == "unmappable"]
MAPPED = [e.name for e in CATALOG if e.status != "unmappable"]


def test_every_mapped_entry_has_a_sample():
    assert sorted(MAPPED) == sorted(SAMPLES)


@pytest.mark.parametrize("name", MAPPED)
def test_forward_every_catalogue_entry(name):
    tab, sql = SAMPLES[name]
    t = translate(tab)
    assert t.sql == sql
    sqlglot.parse_one(t.sql, read="snowflake")  # valid Snowflake SQL
    assert bool(t.notes) == (BY_NAME[name].status == "approximated")


@pytest.mark.parametrize("name", MAPPED)
def test_reverse_every_catalogue_entry(name):
    tab, sql = SAMPLES[name]
    back = from_sql(sql)
    if name == "ATTR":
        assert back == "MIN([a])"  # many-to-one: ATTR has no SQL image of its own
    elif name == "ISNULL":
        assert back == "ISNULL([a])"
    else:
        # reverse of the SQL must be the same formula up to whitespace/quoting of the unit
        assert translate(back).sql == sql


@pytest.mark.parametrize("name", UNMAPPABLE)
def test_unmappable_entries_raise(name):
    with pytest.raises(Untranslatable):
        translate(f"{name}('x', [a], 1)")


@pytest.mark.parametrize("tab,sql,typ", [
    ("[a] + [b] * 2", "a + b * 2", "num"),
    ("([a] + [b]) * 2", "(a + b) * 2", "num"),
    ("[a] - ([b] - [c])", "a - (b - c)", "num"),
    ("[a] - [b] - [c]", "a - b - c", "num"),
    ("'x' + [s]", "'x' || s", "str"),
    ("[s] + 'x' + 'y'", "s || 'x' || 'y'", "str"),
    ("[a] == 1", "a = 1", "bool"),
    ("[a] != 1 OR [a] <> 2", "a <> 1 OR a <> 2", "bool"),
    ("NOT [a] > 1", "NOT (a > 1)", "bool"),
    ("[a] IN (1, 2, 3)", "a IN (1, 2, 3)", "bool"),
    ("-[a]", "-a", "num"),
    ("IF [a] > 1 THEN 1 ELSEIF [a] < 0 THEN 2 ELSE 3 END", "CASE WHEN a > 1 THEN 1 WHEN a < 0 THEN 2 ELSE 3 END", "num"),
    ("IF [a] THEN 1 END", "CASE WHEN a THEN 1 END", "num"),
    ("CASE [r] WHEN 'a' THEN 1 WHEN 'b' THEN 2 END", "CASE r WHEN 'a' THEN 1 WHEN 'b' THEN 2 END", "num"),
    ("#2020-01-02#", "CAST('2020-01-02' AS DATE)", "date"),
    ("#2020-01-02 03:04:05#", "CAST('2020-01-02 03:04:05' AS TIMESTAMP)", "date"),
    ("'it\\'s'", "'it\\'s'", "str"),
    ("[a] // comment\n + 1", "a + 1", "num"),
    ("/* c */ [a]", "a", "any"),
    ("null", "NULL", "any"),
    ("true AND false", "TRUE AND FALSE", "bool"),
    ("[ds].[a]", "ds.a", "any"),
    ("[A col]", '"A col"', "any"),
    ("ROUND([a])", "ROUND(a, 0)", "num"),
    ("sum([a])", "SUM(a)", "num"),
])
def test_grammar(tab, sql, typ):
    t = translate(tab)
    assert (t.sql, t.type) == (sql, typ)


@pytest.mark.parametrize("tab", [
    "RUNNING_SUM(SUM([b]))",
    "FOO([a])", "SUM([a]", "[a] +", "IF [a] THEN 1", "CASE END", "[Parameters].[P] * 2", "DATETRUNC('fortnight', [d])",
    "DATEPART([x], [d])", "DATENAME('weekday', [d])", "DATENAME('quarter', [d])", "DATEPARSE('d/M/yyyy', [s])", "SUM(1, 2)", "LOG([a], 2)", "[a] @ 1", "THEN",
])
def test_untranslatable_is_loud(tab):
    with pytest.raises(Untranslatable):
        translate(tab)


def test_aggregate_flag_and_resolver():
    assert translate("SUM([a]) / 2").aggregate and not translate("[a] / 2").aggregate
    def res(ds, n):
        from sqlglot import exp
        return (exp.column(n.upper(), table=ds), "str") if n == "s" else None
    assert translate("[s] + 'x'", res).sql == "S || 'x'"
    with pytest.raises(Untranslatable):
        translate("[zz]", res)


@pytest.mark.parametrize("sql,tab", [
    ("COALESCE(a, 5)", "IFNULL([a], 5)"), ("NULLIF(a, 0)", "IF [a] = 0 THEN NULL ELSE [a] END"),
    ("a BETWEEN 1 AND 5", "[a] >= 1 AND [a] <= 5"), ("CAST(a AS INTEGER)", "INT(ROUND([a]))"),
    ("a || b", "[a] + [b]"), ("IFF(a, 1, 2)", "IIF([a], 1, 2)"), ("NOT a IS NULL", "NOT ISNULL([a])"),
    ("CASE WHEN a THEN 1 END", "IF [a] THEN 1 END"), ("COUNT(DISTINCT a)", "COUNTD([a])"), ("a % 2", "[a] % 2"),
    ("TRUE", "true"), ("'it''s'", "'it\\'s'"),
    ("QUARTER(a)", "DATEPART('quarter', [a])"),
    ("DATE_TRUNC('quarter', a)", "DATETRUNC('quarter', [a])"),
])
def test_reverse_extras(sql, tab):
    assert from_sql(sql) == tab


def test_count_star_stays_star():
    assert from_sql("COUNT(*)") == "COUNT(*)"
    from ossie_tableau.calc_parser import translate
    assert translate("COUNT(*)").sql.replace(" ", "") == "COUNT(*)"


@pytest.mark.parametrize("sql", [
    "COUNT(DISTINCT a, b)", "COALESCE(a, b, c)", "a IS TRUE", "LOG(2, a)", "SUBSTRING(a, 1)", "RANK() OVER (ORDER BY a)",
    "SUM(a) OVER (ORDER BY b)", "a ILIKE 'x'", "CAST(a AS VARIANT)", "SELECT 1 FROM",
])
def test_reverse_unsupported_is_loud(sql):
    with pytest.raises(Untranslatable):
        from_sql(sql)


def test_reverse_custom_ref():
    from sqlglot import exp
    assert emit_tableau(sqlglot.parse_one("SUM(t.a)", read="snowflake"), lambda c: f"[{c.table}].[{c.name}]") == "SUM([t].[a])"
    assert exp  # keep import used


def test_power_operator_and_bare_field_names():
    assert translate("[a] ^ 2 * 3").sql == "POWER(a, 2) * 3"
    assert translate("Gender = 'F'", lambda ds, n: (sqlglot.exp.column(n), "any")).sql == "Gender = 'F'"


@pytest.mark.parametrize("sql,tab", [
    ("NOT x BETWEEN 1 AND 2", "NOT ([x] >= 1 AND [x] <= 2)"),
    ("x BETWEEN 1 AND 2 AND y = 3", "[x] >= 1 AND [x] <= 2 AND [y] = 3"),
    ("(x BETWEEN 1 AND 2) = TRUE", "([x] >= 1 AND [x] <= 2) = true"),
    ("CASE WHEN NOT x BETWEEN 1 AND 2 THEN 1 END", "IF NOT ([x] >= 1 AND [x] <= 2) THEN 1 END"),
])
def test_between_is_parenthesised_under_tighter_parent(sql, tab):
    assert from_sql(sql) == tab


def _typed(types):
    from sqlglot import exp
    return lambda ds, n: (exp.column(n.upper(), table=ds), types[n]) if n in types else None


@pytest.mark.parametrize("types,sql", [
    ({"a": "str", "b": "str"}, "A || B"), ({"a": "num", "b": "num"}, "A + B"),
    ({"a": "str", "b": "any"}, "A || B"), ({"a": "any", "b": "any"}, "A + B"),
])
def test_plus_follows_operand_types(types, sql):
    assert translate("[a] + [b]", _typed(types)).sql == sql


def test_plus_with_unknown_types_notes_assumption():
    assert any("operand types unknown" in n for n in translate("[a] + [b]", _typed({"a": "any", "b": "any"})).notes)
    assert not translate("[a] + [b]", _typed({"a": "num", "b": "num"})).notes


@pytest.mark.parametrize("part,fmt", [
    ("year", "YYYY"), ("month", "MMMM"), ("day", "DD"),
    ("hour", "HH24"), ("minute", "MI"), ("second", "SS"),
])
def test_datename_verified_parts(part, fmt):
    t = translate(f"DATENAME('{part}', [d])")
    assert t.sql == f"TO_CHAR(d, '{fmt}')"
    assert from_sql(t.sql) == f"DATENAME('{part}', [d])"
