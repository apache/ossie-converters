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

"""Shapes the golden cases do not cover: refusals that would otherwise change the number, naming,
merging, and reading Ossie files that other converters wrote."""

import json
import pathlib
import re

import pytest
import sqlglot
import yaml

import _golden as golden
from ossie_atlan import converter as A
from ossie_atlan import load

HERE = pathlib.Path(__file__).resolve().parent
TABLE = "TPCDS.PUBLIC.CUSTOMER"


def _rec(formula, params=()):
    rec = json.loads((golden.FIXTURES / "count_filtered" / "input.registry.json").read_text())
    rec["metadata"]["definition"]["formula"] = formula
    rec["metadata"]["definition"]["source"] = {"sql": None, "alias": "m", "parameters": list(params),
                                               "table": TABLE, "entity": TABLE}
    return rec


@pytest.mark.parametrize("formula, reason", [
    (f"SELECT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} GROUP BY 1 HAVING COUNT(*) > 5", "HAVING"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = '{{customer_id}}'", "{template}"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = :cust", "undeclared parameter"),
    (f"SELECT COUNT(*) FROM {TABLE} c WHERE EXISTS (SELECT 1 FROM TPCDS.PUBLIC.STORE_SALES s WHERE s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK)", "subquery"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_SK IN (SELECT SS_CUSTOMER_SK FROM TPCDS.PUBLIC.STORE_SALES)", "subquery"),
    (f"SELECT DISTINCT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} GROUP BY 1", "DISTINCT"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = {{customer_id}}", "template"),
])
def test_shapes_that_would_change_the_number_are_refused(formula, reason):
    """Each of these converts to something that returns a different number, or crashes, unless refused."""
    with pytest.raises(A.NotConvertible, match=re.escape(reason)):
        A.convert(_rec(formula))


def test_variant_paths_are_not_parameters_and_string_defaults_are_literals():
    (m, _, _), = A.convert(_rec("SUM(m.V:amount)"))
    assert "GET_PATH(customer.v, 'amount')" in m["expression"]["dialects"][0]["expression"]
    (m, _, _), = A.convert(_rec(f"SELECT COUNT(*) FROM {TABLE} WHERE C_LAST_NAME = :who",
                                [{"name": "who", "datatype": "string", "default": "O'Brien"}]))
    lits = [l.this for l in sqlglot.parse_one(m["expression"]["dialects"][0]["expression"], read="snowflake")
            .find_all(sqlglot.exp.Literal) if l.is_string]
    assert "O'Brien" in lits                     # a string literal, not a quoted identifier


def test_multi_column_distinct_keeps_every_column_under_a_filter():
    (m, _, _), = A.convert(_rec(f"SELECT COUNT(DISTINCT C_FIRST_NAME, C_LAST_NAME) AS n FROM {TABLE} WHERE C_PREFERRED_CUST_FLAG = 'Y'"))
    e = m["expression"]["dialects"][0]["expression"]
    assert "customer.c_first_name" in e and "customer.c_last_name" in e


def test_unaliased_group_expression_gets_a_name():
    parts = A.convert(_rec(f"SELECT UPPER(C_BIRTH_COUNTRY), COUNT(*) AS n FROM {TABLE} GROUP BY 1"))
    assert all(d.split(".", 1)[1] for d in A._agent_context(parts[0][0])["dimensions"])


def test_a_metric_built_from_other_metrics_says_so():
    rec = _rec("returned_sales / NULLIF(store_sales_count, 0)")
    rec["metadata"]["definition"]["depends_on"] = ["returned_sales", "store_sales_count"]
    with pytest.raises(A.NotConvertible, match="built from other metrics"):
        A.convert(rec)


def test_instructions_edited_to_empty_are_refused():
    rec = _rec(f"SELECT COUNT(*) FROM {TABLE} WHERE C_PREFERRED_CUST_FLAG = 'Y'")
    rec["metadata"]["definition"]["grain"] = "customer"
    (m, ds, rels), = A.convert(rec)
    m["ai_context"]["instructions"] = ""
    with pytest.raises(A.NotWritable, match="generated from agent_context"):
        A.write_back({"datasets": ds, "relationships": rels, "metrics": [m]}, m["name"], rec)


def test_two_records_with_the_same_slugged_name_cannot_share_a_file():
    a, b = _rec("COUNT(*)"), _rec("COUNT(*)")
    a["name"], b["name"] = "Store visits", "Store Visits"
    with pytest.raises(ValueError, match="already used"):
        A.merge(A.convert(a) + A.convert(b))
    doc = A.merge(A.convert(a) + A.convert(b), on_conflict="skip")
    assert [m["name"] for m in doc["metrics"]] == ["store_visits"]
    assert any("left out" in n for n in doc["custom_extensions"][0]["data"]["registry"]["merge_notes"])


def test_stale_registry_version_is_a_conflict():
    rec = _rec("COUNT(*)")
    (m, ds, rels), = A.convert(rec)
    doc = {"datasets": ds, "relationships": rels, "metrics": [m]}
    m["description"] = "edited"
    with pytest.raises(A.Conflict):
        A.write_back(doc, m["name"], rec, registry_version=rec["metadata"]["definition"]["version"] + 1)


SIBLING_FIXTURES = [HERE.parents[1] / "cube" / "tests" / "fixtures" / "tpcds_ossie.yaml"]


@pytest.mark.parametrize("path", SIBLING_FIXTURES, ids=lambda p: p.parts[-4])
def test_reverse_reads_ossie_files_written_by_other_converters(path):
    """Ossie -> registry on a file another converter produced: every metric either becomes a
    definition valid under the registry schema or is refused with a reason, and most convert."""
    jsonschema = pytest.importorskip("jsonschema")
    if not path.exists():
        pytest.skip(f"{path.parts[-4]} fixture not present")
    kind = json.loads((HERE / "fixtures" / "metric_kind.json").read_text())
    v = jsonschema.Draft202012Validator(kind["manifest"]["properties"]["definition"])
    doc = load(yaml.safe_load(path.read_text()))
    ok = refused = 0
    for m in doc["metrics"]:
        try:
            rec = A.to_registry(doc, m["name"])
        except A.NotConvertible:
            refused += 1
            continue
        ok += 1
        assert not list(v.iter_errors(rec["definition"])), m["name"]
    assert ok > refused, (ok, refused)
