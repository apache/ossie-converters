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

from ossie_sap_csn.cxn import UnsupportedExpression, cxn_to_sql, sql_to_cxn
from ossie_sap_csn.datatypes import CDS_TO_SQL_TYPE


def _bare(table, column):
    return [column] if table is None else [table, column]


@pytest.mark.parametrize(
    "cxn, sql",
    [
        ({"ref": ["a"]}, "a"),
        ({"val": "it's"}, "'it''s'"),
        ({"val": 3}, "3"),
        ({"val": None}, "NULL"),
        ({"val": True}, "TRUE"),
        ({"xpr": [{"ref": ["a"]}, "*", {"val": 2}]}, "a * 2"),
        ({"xpr": [{"xpr": [{"ref": ["a"]}, "+", {"ref": ["b"]}]}, "*", {"ref": ["c"]}]}, "(a + b) * c"),
        ({"xpr": [{"ref": ["a"]}, "is", "null"]}, "a IS NULL"),
        ({"xpr": ["not", {"ref": ["a"]}]}, "NOT a"),
        ({"xpr": [{"ref": ["a"]}, "in", {"list": [{"val": 1}, {"val": 2}]}]}, "a IN (1, 2)"),
        (
            {"xpr": ["case", "when", {"xpr": [{"ref": ["a"]}, ">", {"val": 0}]}, "then", {"val": "pos"}, "else", {"val": "neg"}, "end"]},
            "CASE WHEN (a > 0) THEN 'pos' ELSE 'neg' END",
        ),
        ({"func": "upper", "args": [{"ref": ["a"]}]}, "UPPER(a)"),
        ({"func": "count", "args": ["*"]}, "COUNT(*)"),
        ({"func": "count", "args": [{"xpr": ["distinct", {"ref": ["a"]}]}]}, "COUNT(DISTINCT a)"),
        ({"ref": ["a"], "cast": {"type": "cds.Integer"}}, "CAST(a AS INT)"),
        ({"ref": ["assoc", "name"]}, "Target.name"),
    ],
)
def test_cxn_to_sql(cxn, sql):
    assert cxn_to_sql(cxn, lambda path: f"Target.{path[1]}") == sql


@pytest.mark.parametrize(
    "sql",
    [
        "a",
        "a * 2",
        "(a + b) * c",
        "a IS NULL",
        "NOT a",
        "a IN (1, 2)",
        "a BETWEEN 1 AND 5",
        "a LIKE 'x%'",
        "CASE WHEN a > 0 THEN 'pos' ELSE 'neg' END",
        "UPPER(a)",
        "COALESCE(a, b, 0)",
        "COUNT(DISTINCT a)",
        "CAST(a AS INT)",
        "a || 'x'",
    ],
)
def test_sql_survives_a_round_trip_through_cxn(sql):
    cxn = sql_to_cxn(sql, _bare)
    again = cxn_to_sql(cxn)
    assert sql_to_cxn(again, _bare) == cxn


@pytest.mark.parametrize(
    "sql",
    [
        "SUM(a) OVER (PARTITION BY b)",
        "(SELECT 1)",
        "CAST(a AS GEOGRAPHY)",
        "a IS NOT DISTINCT FROM b",
    ],
)
def test_untranslatable_sql_is_refused_not_guessed(sql):
    with pytest.raises(UnsupportedExpression):
        sql_to_cxn(sql, _bare)


@pytest.mark.parametrize(
    "cxn",
    [
        {"ref": ["$session", "user"]},
        {"ref": [{"id": "a", "where": []}]},
        {"func": "f", "args": {"x": {"ref": ["a"]}}},
        {"SELECT": {}},
        {"ref": ["a", "b", "c"]},
        # Malformed shapes are refused too, never a crash.
        {"ref": []},
        {"ref": "a"},
        {"ref": ["a"], "cast": "cds.Integer"},
        {"ref": ["a"], "cast": None},
        {"xpr": [{"ref": ["a"]}, "in", {"list": 5}]},
        {"xpr": "a"},
        {"func": "f", "args": [{"xpr": 3}]},
        {"func": 5, "args": []},
        # Tokens and keys whose meaning a rendering would drop or invent.
        {"xpr": [{"ref": ["a"]}, "?:", {"val": 1}]},
        {"xpr": [{"ref": ["a"]}, "@@", {"ref": ["b"]}]},
        {"func": "rank", "args": [], "xpr": ["over", {"xpr": ["order by", {"ref": ["a"]}]}]},
        {"ref": ["p"], "param": True},
    ],
)
def test_untranslatable_cxn_is_refused_not_guessed(cxn):
    with pytest.raises(UnsupportedExpression):
        cxn_to_sql(cxn)


@pytest.mark.parametrize("name, rendered", [("from", '"from"'), ("plus", "plus"), ("my col", '"my col"'), ('a"b', '"a""b"')])
def test_identifiers_that_are_not_plain_words_are_quoted(name, rendered):
    assert cxn_to_sql({"ref": [name]}) == rendered


def test_quoted_identifier_round_trips_through_sql():
    sql = cxn_to_sql({"xpr": [{"ref": ["select"]}, "+", {"val": 1}]})
    assert sql == '"select" + 1'
    assert sql_to_cxn(sql, _bare) == {"xpr": [{"ref": ["select"]}, "+", {"val": 1}]}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_literals_are_refused(value):
    with pytest.raises(UnsupportedExpression):
        cxn_to_sql({"val": value})


def test_parse_error_text_carries_no_ansi_escapes():
    with pytest.raises(UnsupportedExpression) as info:
        sql_to_cxn("this is not sql !!!", _bare)
    assert "\x1b" not in str(info.value)


# cds.Int32 is CDS's other name for cds.Integer, so SQL cannot tell them apart; the
# importer keeps the original CXN for it (see test_roundtrip).
_DISTINCT_CAST_TYPES = sorted(set(CDS_TO_SQL_TYPE) - {"cds.Int32"})


@pytest.mark.parametrize("cds", _DISTINCT_CAST_TYPES)
def test_every_cast_type_reads_back_as_itself(cds):
    cxn = {"ref": ["a"], "cast": {"type": cds}}
    assert sql_to_cxn(cxn_to_sql(cxn), _bare) == cxn


def test_cast_sql_types_are_distinct_except_for_the_int32_synonym():
    rendered = [CDS_TO_SQL_TYPE[t] for t in _DISTINCT_CAST_TYPES]
    assert len(set(rendered)) == len(rendered)
    assert CDS_TO_SQL_TYPE["cds.Int32"] == CDS_TO_SQL_TYPE["cds.Integer"]


@pytest.mark.parametrize(
    "cast, sql",
    [
        ({"type": "cds.DateTime"}, "CAST(a AS TIMESTAMP(0))"),  # seconds
        ({"type": "cds.Timestamp"}, "CAST(a AS TIMESTAMP)"),  # fractional seconds
        ({"type": "cds.LargeString"}, "CAST(a AS CLOB)"),
        ({"type": "cds.String", "length": 100}, "CAST(a AS VARCHAR(100))"),
        ({"type": "cds.Decimal", "precision": 10}, "CAST(a AS DECIMAL(10))"),
        ({"type": "cds.Decimal", "precision": 10, "scale": 2}, "CAST(a AS DECIMAL(10, 2))"),
    ],
)
def test_cast_facets_and_precision_survive_sql(cast, sql):
    cxn = {"ref": ["a"], "cast": cast}
    assert cxn_to_sql(cxn) == sql
    assert sql_to_cxn(sql, _bare) == cxn


@pytest.mark.parametrize(
    "cast",
    [
        {"type": "cds.Decimal", "scale": 2},  # SQL type parameters are positional
        {"type": "cds.Integer", "length": 5},
        {"type": "cds.Decimal", "precision": "10"},
        {"type": "cds.Decimal", "precision": 10, "scale": "floating"},
        {"type": "cds.UUID"},
    ],
)
def test_cast_facets_without_an_sql_rendering_are_refused(cast):
    with pytest.raises(UnsupportedExpression):
        cxn_to_sql({"ref": ["a"], "cast": cast})


@pytest.mark.parametrize("sql", ["CAST(a AS TIMESTAMP(3))", "CAST(a AS INT(11))", "CAST(a AS VARCHAR(MAX))", "CAST(CAST(a AS INT) AS VARCHAR)"])
def test_sql_cast_with_no_cds_equivalent_is_refused(sql):
    with pytest.raises(UnsupportedExpression):
        sql_to_cxn(sql, _bare)
