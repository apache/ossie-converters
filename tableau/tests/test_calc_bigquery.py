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

from ossie_tableau.calc_catalog import CATALOG, PORTABLE, for_dialect
from ossie_tableau.calc_parser import Untranslatable, from_sql, translate
from ossie_tableau.warehouse import BIGQUERY

pytestmark = pytest.mark.unit

SAMPLES = {
    "SUM": "SUM([x])", "AVG": "AVG([x])", "MIN": "MIN([x])", "MAX": "MAX([x])", "COUNT": "COUNT([x])", "COUNTD": "COUNTD([x])",
    "MEDIAN": "MEDIAN([x])", "STDEV": "STDEV([x])", "VAR": "VAR([x])", "ATTR": "ATTR([x])", "ZN": "ZN([x])", "IFNULL": "IFNULL([x], 1)",
    "ISNULL": "ISNULL([x])", "DATETRUNC": "DATETRUNC('month', [d])", "DATEPART": "DATEPART('weekday', [d])",
    "DATEADD": "DATEADD('day', 3, [d])", "DATEDIFF": "DATEDIFF('day', [d], [e])", "YEAR": "YEAR([d])", "MONTH": "MONTH([d])",
    "DAY": "DAY([d])", "MAKEPOINT": "MAKEPOINT([a], [b])", "DATEPARSE": "DATEPARSE('yyyy-MM-dd', [s])", "DATENAME": "DATENAME('month', [d])",
    "TODAY": "TODAY()", "NOW": "NOW()", "LEFT": "LEFT([s], 2)", "RIGHT": "RIGHT([s], 2)", "MID": "MID([s], 2, 3)", "LEN": "LEN([s])",
    "CONTAINS": "CONTAINS([s], 'a')", "STARTSWITH": "STARTSWITH([s], 'a')", "ENDSWITH": "ENDSWITH([s], 'a')", "UPPER": "UPPER([s])",
    "LOWER": "LOWER([s])", "TRIM": "TRIM([s])", "REPLACE": "REPLACE([s], 'a', 'b')", "SPLIT": "SPLIT([s], '-', 2)", "ROUND": "ROUND([x], 2)",
    "ABS": "ABS([x])", "CEILING": "CEILING([x])", "FLOOR": "FLOOR([x])", "POWER": "POWER([x], 2)", "SQRT": "SQRT([x])", "LOG": "LOG([x])",
    "LN": "LN([x])", "EXP": "EXP([x])", "SIGN": "SIGN([x])", "PI": "PI()", "FIND": "FIND([s], 'a')", "REGEXP_REPLACE": "REGEXP_REPLACE([s], 'a', 'b')",
    "REGEXP_EXTRACT": "REGEXP_EXTRACT([s], '(a)')", "ISOWEEKDAY": "ISOWEEKDAY([d])", "INT": "INT([x])", "FLOAT": "FLOAT([x])", "STR": "STR([x])",
    "DATE": "DATE([s])", "DATETIME": "DATETIME([s])", "IIF": "IIF([x] > 1, 1, 2)",
}
UNMAPPABLE = {"MEDIAN"}
ROUND_TRIP = {"DATETRUNC", "DATEADD", "DATEDIFF", "MAKEPOINT", "DATEPARSE", "DATENAME", "NOW", "SPLIT", "PI", "FIND"}


def _bq(name):
    return for_dialect(next(e for e in CATALOG if e.name == name), "bigquery")


def test_every_catalogue_entry_is_decided_for_bigquery():
    for e in CATALOG:
        b = for_dialect(e, "bigquery")
        assert b.status in ("direct", "approximated", "unmappable"), e.name
        if b.status == "unmappable":
            assert b.note, f"{e.name}: unmappable without a reason"
        else:
            assert b.sql, e.name
        if e.status != "unmappable" and e.name not in PORTABLE["bigquery"] and "bigquery" not in e.overrides:
            assert b.status == "unmappable"


def test_samples_cover_the_catalogue():
    mappable = {e.name for e in CATALOG if e.status != "unmappable"}
    assert mappable <= set(SAMPLES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_translates_to_valid_bigquery(name):
    if name in UNMAPPABLE:
        with pytest.raises(Untranslatable):
            translate(SAMPLES[name], wh=BIGQUERY)
        return
    sql = translate(SAMPLES[name], wh=BIGQUERY).sql
    assert sqlglot.parse_one(sql, read="bigquery") is not None


def test_dateadd_interval_is_an_integer_literal():
    assert translate("DATEADD('day', 3, [d])", wh=BIGQUERY).sql == "DATETIME_ADD(CAST(d AS DATETIME), INTERVAL 3 DAY)"


@pytest.mark.parametrize("name", sorted(ROUND_TRIP))
def test_bigquery_sql_comes_back_to_tableau(name):
    sql = translate(SAMPLES[name], wh=BIGQUERY).sql
    back = from_sql(sql, wh=BIGQUERY)
    assert translate(back, wh=BIGQUERY).sql == sql


def test_weekday_maps_to_dayofweek():
    assert translate("DATEPART('weekday', [d])", wh=BIGQUERY).sql == "EXTRACT(DAYOFWEEK FROM CAST(d AS DATETIME))"


def test_dateparse_uses_strftime_formats():
    assert translate("DATEPARSE('dd/MM/yyyy HH:mm', [s])", wh=BIGQUERY).sql == "SAFE.PARSE_DATETIME('%d/%m/%Y %H:%M', s)"


@pytest.mark.parametrize("part,fmt", [
    ("year", "%Y"), ("month", "%B"), ("day", "%d"),
    ("hour", "%H"), ("minute", "%M"), ("second", "%S"),
])
def test_datename_verified_parts_bq(part, fmt):
    t = translate(f"DATENAME('{part}', [d])", wh=BIGQUERY)
    assert t.sql == f"FORMAT_DATETIME('{fmt}', CAST(d AS DATETIME))"
    assert from_sql(t.sql, wh=BIGQUERY) == f"DATENAME('{part}', [d])"


def test_dateparse_rejects_unknown_format_letters():
    with pytest.raises(Untranslatable):
        translate("DATEPARSE('yyyy-MM-dd Z', [s])", wh=BIGQUERY)


def test_unknown_date_part_is_never_guessed_in_reverse():
    with pytest.raises(Untranslatable):
        from_sql("EXTRACT(EPOCH FROM x)", wh=BIGQUERY)


def test_approximated_entries_record_their_note():
    t = translate("DATETRUNC('month', [d])", wh=BIGQUERY)
    assert t.notes and t.notes[0].startswith("DATETRUNC:")


def test_snowflake_output_is_unchanged():
    assert translate("DATETRUNC('month', [d])").sql == "DATE_TRUNC('MONTH', d)"
    assert translate("MEDIAN([x])").sql == "MEDIAN(x)"


@pytest.mark.parametrize("sql", ["COUNTIF(`x` > 1)", "SAFE_DIVIDE(`a`, `b`)", "FORMAT_DATE('%Y', `d`)", "DATE_TRUNC(`d`, MONTH)"])
def test_bigquery_native_functions_never_crash(sql):
    try:
        out = from_sql(sql, wh=BIGQUERY)
    except Untranslatable:
        return
    assert isinstance(out, str) and out


@pytest.mark.parametrize("sql", ["ARRAY_AGG(`x`)", "STRUCT(`a`, `b`)", "(SELECT x FROM UNNEST(`arr`) AS x)"])
def test_bigquery_nested_types_are_guarded(sql):
    try:
        from_sql(sql, wh=BIGQUERY)
    except Untranslatable:
        pass
