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

"""Each row is checked by hand against the Ossie expression language and the Databricks docs.

A sqlglot node type enters `expressions._PORTABLE` only together with a row here.
"""

import pytest

from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    has_window,
    identifier,
    measure_names,
    measure_reference,
    parse,
    translate,
)

TO_DATABRICKS = [
    ("SUM(x) / COUNT(DISTINCT y)", "SUM(x) / COUNT(DISTINCT y)"),
    ("COUNT(*)", "COUNT(*)"),
    ("AVG(x) + MIN(x) - MAX(x)", "AVG(x) + MIN(x) - MAX(x)"),
    ("STDDEV(x) + STDDEV_POP(x) + VAR_POP(x)", "STDDEV(x) + STDDEV_POP(x) + VAR_POP(x)"),
    ("VAR_SAMP(x)", "VARIANCE(x)"),
    ("MEDIAN(x)", "MEDIAN(x)"),
    ("APPROX_COUNT_DISTINCT(x)", "APPROX_COUNT_DISTINCT(x)"),
    ("SUM(x) FILTER (WHERE c > 1)", "SUM(x) FILTER(WHERE c > 1)"),
    ("CASE WHEN a > 0 THEN b ELSE c END", "CASE WHEN a > 0 THEN b ELSE c END"),
    ("CASE x WHEN 1 THEN 'a' END", "CASE x WHEN 1 THEN 'a' END"),
    ("COALESCE(x, 0) / NULLIF(y, 0)", "COALESCE(x, 0) / NULLIF(y, 0)"),
    ("IFNULL(x, 0)", "COALESCE(x, 0)"),
    ("IF(c, 1, 2)", "IF(c, 1, 2)"),
    ("IFF(c, 1, 2)", "IFF(c, 1, 2)"),
    ("NVL2(x, 1, 2)", "NVL2(x, 1, 2)"),
    ("ZEROIFNULL(x)", "ZEROIFNULL(x)"),
    (
        "x BETWEEN 1 AND 2 AND y IN (1, 2) AND z IS NOT NULL",
        "x BETWEEN 1 AND 2 AND y IN (1, 2) AND NOT z IS NULL",
    ),
    ("a IS DISTINCT FROM b", "a IS DISTINCT FROM b"),
    ("a || b", "a || b"),
    ("CAST(x AS VARCHAR)", "CAST(x AS STRING)"),
    ("CAST(x AS DECIMAL(10, 2))", "CAST(x AS DECIMAL(10, 2))"),
    ("TRY_CAST(x AS DATE)", "TRY_CAST(x AS DATE)"),
    ("DATE '2024-01-15'", "CAST('2024-01-15' AS DATE)"),
    ('"Order Date"', "`Order Date`"),
    ("'it''s'", "'it\\'s'"),
    ("'a\\b'", "'a\\\\b'"),
    ("s LIKE 'a%' OR s ILIKE 'b%'", "s LIKE 'a%' OR s ILIKE 'b%'"),
    ("CONCAT(a, b) || LOWER(s) || UPPER(s)", "CONCAT(a, b) || LOWER(s) || UPPER(s)"),
    ("LENGTH(s) + POSITION('a' IN s)", "LENGTH(s) + LOCATE('a', s)"),
    ("LEFT(s, 2) || RIGHT(s, 2) || SUBSTRING(s, 1, 2)", None),
    ("REPLACE(s, 'a', 'b') || SPLIT_PART(s, ',', 1)", None),
    ("STARTSWITH(s, 'a') AND ENDSWITH(s, 'b') AND CONTAINS(s, 'c')", None),
    ("ABS(x) + ROUND(x, 2) + FLOOR(x) + CEIL(x) + SIGN(x)", None),
    ("POWER(x, 2) + SQRT(x) + EXP(x) + LN(x) + LOG(2, x)", None),
    ("LOG10(x)", "LOG(10, x)"),
    ("SIN(x) + ATAN2(y, x) + PI() + RADIANS(x)", None),
    ("YEAR(x) + QUARTER(x) + MONTH(x) + DAY(x) + HOUR(x)", None),
    ("EXTRACT(YEAR FROM x)", "EXTRACT(YEAR FROM x)"),
    ("EXTRACT(DAYOFYEAR FROM x)", "EXTRACT(DOY FROM x)"),
    ("DATE_PART('month', x)", "DATE_PART('month', x)"),
    ("DATE_TRUNC('month', x)", "DATE_TRUNC('MONTH', x)"),
    ("DATEADD(day, 3, x)", "DATEADD(DAY, 3, x)"),
    ("TO_DATE(s)", "TO_DATE(s)"),
    ("CURRENT_DATE", "CURRENT_DATE()"),
    ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"),
    ("TRUNC(x, 2)", "CASE WHEN x < 0 THEN CEIL(x, 2) ELSE FLOOR(x, 2) END"),
    ("REGEXP_EXTRACT(s, 'a+')", "REGEXP_EXTRACT(s, 'a+', 0)"),
    ("TO_CHAR(d, 'YYYY-MM-DD HH24:MI')", "DATE_FORMAT(d, 'yyyy-MM-dd HH:mm')"),
    ("TO_DATE(s, 'DD/MM/YYYY')", "TO_DATE(s, 'dd/MM/yyyy')"),
    # Ossie orders NULLs last on ASC and first on DESC; Databricks is the reverse, so a
    # bare window ORDER BY gets an explicit placement to keep the meaning.
    (
        "ROW_NUMBER() OVER (PARTITION BY a ORDER BY b)",
        "ROW_NUMBER() OVER (PARTITION BY a ORDER BY b NULLS LAST)",
    ),
    (
        "LAG(x, 1, 0) OVER (ORDER BY a) + LEAD(x) OVER (ORDER BY a)",
        "LAG(x, 1, 0) OVER (ORDER BY a NULLS LAST) + LEAD(x) OVER (ORDER BY a NULLS LAST)",
    ),
    (
        "SUM(x) OVER (ORDER BY a ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        "SUM(x) OVER (ORDER BY a NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
    ),
    ("RANK() OVER (ORDER BY amount DESC)", "RANK() OVER (ORDER BY amount DESC NULLS FIRST)"),
    # An Ossie placement that matches the Databricks default is left off.
    ("RANK() OVER (ORDER BY amount NULLS FIRST)", "RANK() OVER (ORDER BY amount)"),
    ("AGG(orders.revenue) / 2", "MEASURE(orders.revenue) / 2"),
]

FROM_DATABRICKS = [
    ("SUM(x) / COUNT(DISTINCT y)", "SUM(x) / COUNT(DISTINCT y)"),
    ("`Order Date` || 'it\\'s'", "\"Order Date\" || 'it''s'"),
    # A backtick name that is a regular identifier prints unquoted (case-insensitive match).
    ("date_trunc('week', `o_orderdate`)", "DATE_TRUNC('WEEK', o_orderdate)"),
    # Databricks integer division becomes Ossie's TRUNC(x / y, 0).
    ("qty DIV 2", "TRUNC(qty / 2, 0)"),
    # Databricks orders NULLs first on ASC and last on DESC; made explicit for Ossie.
    ("RANK() OVER (ORDER BY amount)", "RANK() OVER (ORDER BY amount NULLS FIRST)"),
    ("RANK() OVER (ORDER BY amount DESC)", "RANK() OVER (ORDER BY amount DESC NULLS LAST)"),
    ("'a\\\\b'", "'a\\b'"),
    ("locate('a', s)", "POSITION('a' IN s)"),
    ("dateadd(day, 3, x)", "DATEADD(DAY, 3, x)"),
    ("date_add(x, 3)", "DATEADD(DAY, 3, x)"),
    ("date_trunc('month', x)", "DATE_TRUNC('MONTH', x)"),
    ("extract(DOY FROM x)", "EXTRACT(DAYOFYEAR FROM x)"),
    ("regexp_extract(s, 'a', 0)", "REGEXP_EXTRACT(s, 'a')"),
    ("current_date()", "CURRENT_DATE"),
    ("to_date(s)", "TO_DATE(s)"),
    ("iff(c, 1, 2)", "IF(c, 1, 2)"),
    ("nvl(x, 0)", "COALESCE(x, 0)"),
    ("SUM(x) FILTER (WHERE c)", "SUM(x) FILTER(WHERE c)"),
    ("MEASURE(revenue) / 2", "AGG(revenue) / 2"),
    ("CAST(x AS STRING)", "CAST(x AS STRING)"),
]

UNTRANSLATABLE = [
    ("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x)", OSSIE),
    ("MY_UDF(x)", OSSIE),
    ("TO_CHAR(d, 'Q')", OSSIE),
    ("count_if(x > 1)", DATABRICKS),
    ("try_divide(a, b)", DATABRICKS),
    ("regexp_extract(s, 'a(b)')", DATABRICKS),
    ("to_date(s, 'yyyy-MM-dd')", DATABRICKS),
    ("date_format(d, 'yyyy')", DATABRICKS),
    ("percentile_cont(0.5) WITHIN GROUP (ORDER BY x)", DATABRICKS),
]


@pytest.mark.parametrize(("ossie", "databricks"), TO_DATABRICKS)
def test_ossie_to_databricks(ossie, databricks):
    assert translate(parse(ossie, OSSIE), DATABRICKS) == (databricks or ossie)


@pytest.mark.parametrize(("databricks", "ossie"), FROM_DATABRICKS)
def test_databricks_to_ossie(databricks, ossie):
    assert translate(parse(databricks, DATABRICKS), OSSIE) == ossie


@pytest.mark.parametrize(("text", "dialect"), UNTRANSLATABLE)
def test_untranslatable(text, dialect):
    target = DATABRICKS if dialect == OSSIE else OSSIE
    with pytest.raises(Untranslatable):
        translate(parse(text, dialect), target)


def test_unparsable_text():
    with pytest.raises(Untranslatable):
        parse("SUM(", OSSIE)


def test_ansi_sql_parses_like_ossie_sql():
    assert translate(parse('"a b" + 1', "ANSI_SQL"), DATABRICKS) == "`a b` + 1"


def test_analysis():
    tree = parse("SUM(x) + MEASURE(m) + LAG(y) OVER (ORDER BY z)", DATABRICKS)
    assert has_window(tree)
    assert measure_names(tree) == {"m"}
    assert not has_window(parse("UPPER(s)", DATABRICKS))


def test_names():
    assert measure_reference("revenue", DATABRICKS) == "MEASURE(source.revenue)"
    assert measure_reference("Net Revenue", DATABRICKS) == "MEASURE(source.`Net Revenue`)"
    assert identifier("Order Date", OSSIE) == '"Order Date"'
    assert identifier("amount", OSSIE) == "amount"
