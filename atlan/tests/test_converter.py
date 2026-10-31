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

"""Shapes the golden cases do not cover: inputs that would otherwise convert to a metric that
returns a different number, naming, merging, write-back edits, and reading an Ossie file the
converter did not write."""

import json
import pathlib
import re

import pytest
import sqlglot
import yaml

import _golden as golden
from ossie_atlan import (ConversionError, IssueType, convert_ossie_to_registry, convert_registry_to_ossie,
                         load, write_back)
from ossie_atlan import converter as A

HERE = pathlib.Path(__file__).resolve().parent
TABLE = "TPCDS.PUBLIC.CUSTOMER"


def _rec(formula, params=(), **defn):
    rec = json.loads((golden.FIXTURES / "count_filtered" / "input.registry.json").read_text())
    d = rec["metadata"]["definition"]
    d["formula"] = formula
    d["source"] = {"sql": None, "alias": "m", "parameters": list(params), "table": TABLE, "entity": TABLE}
    d.update(defn)
    return rec


def _one(rec):
    (m, ds, rels), = A.convert(rec)
    return m["expression"]["dialects"][0]["expression"], ds, rels


# ---- refusals: each of these would convert to a different number, or crash, unless refused

@pytest.mark.parametrize("formula, reason", [
    (f"SELECT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} GROUP BY 1 HAVING COUNT(*) > 5", "HAVING"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = '{{customer_id}}'", "{template}"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = :cust", "undeclared parameter"),
    (f"SELECT COUNT(*) FROM {TABLE} c WHERE EXISTS (SELECT 1 FROM TPCDS.PUBLIC.STORE_SALES s WHERE s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK)", "subquery"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_SK IN (SELECT SS_CUSTOMER_SK FROM TPCDS.PUBLIC.STORE_SALES)", "subquery"),
    (f"SELECT DISTINCT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} GROUP BY 1", "DISTINCT"),
    (f"SELECT COUNT(*) FROM {TABLE} WHERE C_CUSTOMER_ID = {{customer_id}}", "template"),
    ("SUM(m.C_BALANCE) OVER (PARTITION BY m.C_BIRTH_COUNTRY)", "window function"),
    ("SUM(x.C_BALANCE)", "reads a table the metric does not declare"),
    (f"SELECT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC LIMIT 5", "LIMIT"),
])
def test_shapes_that_would_change_the_number_are_refused(formula, reason):
    with pytest.raises(A.NotConvertible, match=re.escape(reason)):
        A.convert(_rec(formula))


def test_filter_cannot_fold_into_an_aggregate_that_ranks_by_a_second_column():
    """MIN_BY(value, order) under a filter: wrapping only `value` leaves `order` unfiltered, so the
    globally earliest row would win. PERCENTILE_CONT ... WITHIN GROUP has the same shape."""
    with pytest.raises(A.NotConvertible, match="reads more than one column"):
        A.convert(_rec("MIN_BY(m.C_FIRST_NAME, m.C_BIRTH_YEAR)", filters=["m.C_BIRTH_COUNTRY = 'FRANCE'"]))
    with pytest.raises(A.NotConvertible, match="WITHIN GROUP"):
        A.convert(_rec("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.C_BALANCE)", filters=["m.C_BIRTH_COUNTRY = 'FRANCE'"]))
    e, _, _ = _one(_rec("MIN_BY(m.C_FIRST_NAME, m.C_BIRTH_YEAR)"))     # no filter: nothing to fold
    assert e == "MIN_BY(customer.c_first_name, customer.c_birth_year)"


def test_a_metric_built_from_other_metrics_is_refused_even_under_an_aggregate():
    rec = _rec("SUM(returned_sales) / NULLIF(SUM(store_sales_count), 0)", depends_on=["returned_sales", "store_sales_count"])
    with pytest.raises(A.NotConvertible, match="built from other metrics"):
        A.convert(rec)
    rec = _rec("SUM(returned_sales)", depends_on=["Returned Sales"])        # depends_on holds display names
    with pytest.raises(A.NotConvertible, match="built from other metrics"):
        A.convert(rec)
    rec = _rec("returned_sales / NULLIF(store_sales_count, 0)", depends_on=["returned_sales", "store_sales_count"])
    with pytest.raises(A.NotConvertible, match="built from other metrics"):
        A.convert(rec)


def test_join_with_a_non_key_condition_is_refused_not_crashed():
    rec = _rec("COUNT(*)", joins=[{"name": "s", "to": "TPCDS.PUBLIC.STORE_SALES", "cardinality": "many_to_one",
                                  "sql": "s.SS_CUSTOMER_SK = m.C_CUSTOMER_SK AND s.SS_IS_CURRENT = TRUE"}])
    with pytest.raises(A.NotConvertible, match="is not a key equality"):
        A.convert(rec)


def test_no_default_parameter_used_beyond_one_equality_is_refused():
    rec = _rec(f"SELECT COUNT(*) FROM {TABLE} WHERE C_BIRTH_YEAR > :y",
               [{"name": "acct", "datatype": "string", "default": None}],
               filters=["m.C_CUSTOMER_ID = :acct", "m.C_LOGIN LIKE :acct"])
    with pytest.raises(A.NotConvertible, match="used beyond"):
        A.convert(rec)


@pytest.mark.parametrize("record", [
    {"name": "x"},
    {"name": "x", "id": "metric_1", "metadata": {"definition": {"formula": None}}},
    {"name": "x", "id": "metric_1", "metadata": {"definition": {"formula": "COUNT(*)"}}},
    {"name": "x", "id": "metric_1", "metadata": {"definition": {"formula": "COUNT(*)", "source": {}}}},
])
def test_malformed_records_are_refused_not_crashed(record):
    with pytest.raises(A.NotConvertible):
        A.convert(record)
    doc, issues = convert_registry_to_ossie([record])
    assert doc["metrics"] == [] and len(issues.of_type(IssueType.METRIC_NOT_CONVERTIBLE)) == 1


# ---- windows: a fixed date range is parked, the rest of the filter is kept

def test_business_predicate_next_to_a_date_range_is_kept_and_the_range_is_parked():
    rec = _rec("COUNT(*)", filters=["m.C_FIRST_SALES_DATE >= '2026-01-01' AND m.C_PREFERRED_CUST_FLAG = 'Y'"])
    (m, _, _), = A.convert(rec)
    assert m["expression"]["dialects"][0]["expression"] == "COUNT(CASE WHEN (customer.c_preferred_cust_flag = 'Y') THEN 1 END)"
    assert A.agent_context(m)["window"] == ["customer.c_first_sales_date >= '2026-01-01'"]
    assert "customer.c_first_sales_date >= '2026-01-01'" in m["ai_context"]["instructions"]


@pytest.mark.parametrize("pred", [
    "m.C_SNAPSHOT_DATE = '2026-06-01'",                                   # one snapshot: the metric, not a window
    "m.C_FIRST_SALES_DATE >= DATEADD(day, -30, CURRENT_DATE)",            # relative to today: the metric
    "DATEDIFF(day, m.C_FIRST_SALES_DATE, :asof) <= 30",                   # the column and the date on one side
])
def test_predicates_that_are_the_metric_stay_in_it(pred):
    rec = _rec("COUNT(*)", [{"name": "asof", "datatype": "date", "default": "2026-06-16"}], filters=[pred])
    (m, _, _), = A.convert(rec)
    assert "window" not in A.agent_context(m)
    assert "CASE WHEN" in m["expression"]["dialects"][0]["expression"]


def test_parameter_names_match_as_tokens_not_prefixes():
    rec = _rec("COUNT(*)", [{"name": "d", "datatype": "date", "default": "2026-01-01"},
                            {"name": "dx", "datatype": "integer", "default": 3}],
               filters=["m.C_BIRTH_YEAR > :dx"])
    e, _, _ = _one(rec)
    assert e == "COUNT(CASE WHEN (customer.c_birth_year > 3) THEN 1 END)"


# ---- names and columns

def test_quoted_mixed_case_column_keeps_its_case():
    e, ds, _ = _one(_rec('SUM(m."MyCol")'))
    assert e == 'SUM(customer.mycol)'
    assert [(f["name"], f["expression"]["dialects"][0]["expression"]) for f in ds[0]["fields"]] == [("mycol", '"MyCol"')]


def test_group_by_select_alias_is_that_select_item():
    parts = A.convert(_rec(f"SELECT UPPER(C_BIRTH_COUNTRY) AS country, COUNT(*) AS n FROM {TABLE} GROUP BY country"))
    assert A.agent_context(parts[0][0])["dimensions"] == ["customer.country"]
    assert [f["name"] for f in parts[0][1][0]["fields"]] == ["country"]


def test_variant_paths_are_not_parameters_and_string_defaults_are_literals():
    e, _, _ = _one(_rec("SUM(m.V:amount)"))
    assert "GET_PATH(customer.v, 'amount')" in e
    e, _, _ = _one(_rec(f"SELECT COUNT(*) FROM {TABLE} WHERE C_LAST_NAME = :who",
                        [{"name": "who", "datatype": "string", "default": "O'Brien"}]))
    lits = [l.this for l in sqlglot.parse_one(e, read="snowflake").find_all(sqlglot.exp.Literal) if l.is_string]
    assert "O'Brien" in lits


def test_multi_column_distinct_keeps_every_column_under_a_filter():
    e, _, _ = _one(_rec(f"SELECT COUNT(DISTINCT C_FIRST_NAME, C_LAST_NAME) AS n FROM {TABLE} WHERE C_PREFERRED_CUST_FLAG = 'Y'"))
    assert e == ("COUNT(DISTINCT CASE WHEN (customer.c_preferred_cust_flag = 'Y') THEN customer.c_first_name END, "
                 "CASE WHEN (customer.c_preferred_cust_flag = 'Y') THEN customer.c_last_name END)")


def test_a_table_named_like_a_keyword_gets_its_schema_prefix():
    rec = _rec("COUNT(*)")
    rec["metadata"]["definition"]["source"].update({"table": "TPCDS.PUBLIC.ORDER", "entity": "TPCDS.PUBLIC.ORDER"})
    e, ds, _ = _one(rec)
    assert ds[0]["name"] == "public_order" and e == "COUNT(public_order.*)"


# ---- merging records into one file

def test_two_records_with_the_same_slugged_name_cannot_share_a_file():
    a, b = _rec("COUNT(*)"), _rec("COUNT(*)")
    a["name"], b["name"] = "Store visits", "Store Visits"
    with pytest.raises(ValueError, match="already used"):
        A.merge(A.convert(a) + A.convert(b))
    doc, issues = convert_registry_to_ossie([a, b])
    assert [m["name"] for m in doc["metrics"]] == ["store_visits"]
    assert [(i.issue_type, i.element_name) for i in issues] == [(IssueType.MERGE_CONFLICT, "store_visits")]


def test_two_tables_with_the_same_bare_name_do_not_merge_into_one_dataset():
    a, b = _rec("SUM(m.C_BALANCE)"), _rec("SUM(m.BALANCE)")
    b["name"], b["id"] = "crm_balance", "metric_synth_crm_balance"
    b["metadata"]["definition"]["source"].update({"table": "CRM.RAW.CUSTOMER", "entity": "CRM.RAW.CUSTOMER"})
    doc, issues = convert_registry_to_ossie([a, b])
    assert [d["source"] for d in doc["datasets"]] == ["TPCDS.PUBLIC.CUSTOMER"]
    assert [m["name"] for m in doc["metrics"]] == ["preferred_customer_count"]
    (issue,) = issues.of_type(IssueType.MERGE_CONFLICT)
    assert issue.element_name == "crm_balance" and "share a name" in issue.detail


def test_records_of_two_workspaces_cannot_share_a_file():
    a, b = _rec("COUNT(*)"), _rec("SUM(m.C_BALANCE)")
    b["name"], b["workspace_id"] = "other", "workspace_other"
    with pytest.raises(ConversionError, match="2 workspaces"):
        convert_registry_to_ossie([a, b])


# ---- write-back

def _doc(rec):
    (m, ds, rels), = A.convert(rec)
    return {"datasets": ds, "relationships": rels, "metrics": [m]}


def test_field_expression_edit_is_written_back_as_a_new_formula():
    rec = _rec("SUM(m.C_BALANCE)", filters=["m.C_PREFERRED_CUST_FLAG = 'Y'"])
    doc = _doc(rec)
    assert write_back(doc, "preferred_customer_count", rec) == {}
    f = next(f for f in doc["datasets"][0]["fields"] if f["name"] == "c_balance")
    for dl in f["expression"]["dialects"]:
        dl["expression"] = "C_BALANCE_INC_TAX"
    patch = write_back(doc, "preferred_customer_count", rec)["set"]
    assert patch["formula"] == "SUM(CASE WHEN (m.C_PREFERRED_CUST_FLAG = 'Y') THEN m.C_BALANCE_INC_TAX END)"
    assert patch["filters"] == [] and patch["mandatory_filters"] == []


def test_expression_edit_keeps_the_parked_window_and_the_parameter_pick():
    rec = _rec("COUNT(*)", [{"name": "start", "datatype": "date", "default": "2026-01-01"},
                            {"name": "acct", "datatype": "string", "default": None}],
               filters=["m.C_FIRST_SALES_DATE >= :start", "m.C_CUSTOMER_ID = :acct", "m.C_PREFERRED_CUST_FLAG = 'Y'"])
    doc = _doc(rec)
    m = doc["metrics"][0]
    for dl in m["expression"]["dialects"]:
        dl["expression"] = "2 * " + dl["expression"]
    patch = write_back(doc, m["name"], rec)["set"]
    assert patch["formula"].startswith("2 * COUNT(CASE WHEN (m.C_PREFERRED_CUST_FLAG = 'Y')")
    assert patch["filters"] == ["m.C_CUSTOMER_ID = :acct", "m.C_FIRST_SALES_DATE >= :start"]


def test_renaming_a_metric_writes_back_its_name():
    rec = _rec("COUNT(*)")
    doc = _doc(rec)
    doc["metrics"][0]["name"] = "renamed"
    assert write_back(doc, "renamed", rec)["set"] == {"name": "renamed"}


def test_split_part_description_edit_drops_the_generated_sentence():
    rec = _rec(f"SELECT COUNT(*) AS n, SUM(C_BALANCE) AS v FROM {TABLE}")
    doc = A.merge(A.convert(rec))
    v = doc["metrics"][1]
    assert v["description"].endswith("This part: v = SUM(CUSTOMER.C_BALANCE).")
    v["description"] = v["description"] + " Edited."
    assert write_back(doc, v["name"], rec)["set"] == {"description": rec["description"] + " Edited."}


def test_instructions_edited_to_empty_are_refused():
    rec = _rec(f"SELECT COUNT(*) FROM {TABLE} WHERE C_PREFERRED_CUST_FLAG = 'Y'", grain="customer")
    doc = _doc(rec)
    doc["metrics"][0]["ai_context"]["instructions"] = ""
    with pytest.raises(A.NotWritable, match="generated from agent_context"):
        write_back(doc, doc["metrics"][0]["name"], rec)


def test_stale_registry_version_is_a_conflict():
    rec = _rec("COUNT(*)")
    doc = _doc(rec)
    doc["metrics"][0]["description"] = "edited"
    with pytest.raises(A.Conflict):
        write_back(doc, doc["metrics"][0]["name"], rec, registry_version=rec["metadata"]["definition"]["version"] + 1)


def test_write_back_against_the_wrong_record_is_refused():
    rec = _rec("COUNT(*)")
    doc = _doc(rec)
    other = _rec("COUNT(*)")
    other["id"] = "metric_synth_other"
    with pytest.raises(A.NotWritable, match="was made from registry metric"):
        write_back(doc, doc["metrics"][0]["name"], other)


# ---- reverse on files the converter did not write

def test_reverse_parses_in_the_metric_dialect():
    rec = _rec("COUNT(DISTINCT m.`ss customer`)", dialect="bigquery")
    rec["metadata"]["definition"]["source"].update({"table": "tpcds.public.store_sales", "entity": "tpcds.public.store_sales"})
    doc = _doc(rec)
    back = convert_ossie_to_registry(doc, doc["metrics"][0]["name"])
    assert back["definition"]["dialect"] == "bigquery" and "`ss customer`" in back["definition"]["formula"]


def test_reverse_refuses_two_datasets_with_no_relationship_and_a_dataset_with_no_source():
    doc = {"datasets": [{"name": "a", "source": "DB.S.A", "fields": [{"name": "x", "expression": A._expr("X")}]},
                        {"name": "b", "source": "DB.S.B", "fields": [{"name": "y", "expression": A._expr("Y")}]}],
           "metrics": [{"name": "r", "expression": A._expr("SUM(a.x) / SUM(b.y)")}]}
    with pytest.raises(A.NotConvertible, match="no relationship"):
        convert_ossie_to_registry(doc, "r")
    doc["datasets"][1].pop("source")
    doc["metrics"][0]["expression"] = A._expr("SUM(b.y)")
    with pytest.raises(A.NotConvertible, match="no source table"):
        convert_ossie_to_registry(doc, "r")


def test_reverse_of_a_metric_on_the_joined_table_has_no_self_join():
    rec = _rec("SUM(s.SS_NET_PAID)", joins=[{"name": "s", "to": "TPCDS.PUBLIC.STORE_SALES", "cardinality": "many_to_one",
                                            "sql": "s.SS_ITEM_SK = m.C_ITEM_SK"}])
    rec["metadata"]["definition"]["source"].update({"table": "TPCDS.PUBLIC.ITEM", "entity": "TPCDS.PUBLIC.ITEM"})
    doc = _doc(rec)
    doc["metrics"][0]["custom_extensions"] = []            # as another tool would write it
    back = convert_ossie_to_registry(doc, doc["metrics"][0]["name"])["definition"]
    assert back["source"]["table"] == "TPCDS.PUBLIC.STORE_SALES" and back["joins"] == []
    assert back["formula"] == "SUM(m.SS_NET_PAID)"


def test_reverse_reads_the_core_spec_tpcds_example():
    """Ossie -> registry on apache/ossie's own TPC-DS example: every metric either becomes a
    definition valid under the registry schema or is refused with a reason."""
    jsonschema = pytest.importorskip("jsonschema")
    path = HERE.parents[1] / "ossie" / "examples" / "tpcds_semantic_model.yaml"
    if not path.exists():
        pytest.skip("apache/ossie submodule not checked out")
    v = jsonschema.Draft202012Validator(json.loads((HERE / "fixtures" / "metric_kind.json").read_text()))
    doc = load(yaml.safe_load(path.read_text()))
    outcomes = {}
    for m in doc["metrics"]:
        try:
            rec = convert_ossie_to_registry(doc, m["name"])
            assert not list(v.iter_errors(rec["definition"])), m["name"]
            outcomes[m["name"]] = "ok"
        except A.NotConvertible as e:
            outcomes[m["name"]] = str(e)[:40]
    ok = [k for k, o in outcomes.items() if o == "ok"]
    assert len(ok) >= len(outcomes) // 2, outcomes


# ---- round two: shapes found by a second adversarial pass

def test_date_looking_string_literal_is_not_a_window():
    e, _, _ = _one(_rec("COUNT(*)", filters=["m.ORDER_CODE >= '2026-01-15-A'"]))
    assert e == "COUNT(CASE WHEN (customer.order_code >= '2026-01-15-A') THEN 1 END)"


def test_parenthesised_conjunction_is_split_like_a_bare_one():
    (m, _, _), = A.convert(_rec("COUNT(*)", filters=["((m.A = 1) AND (m.D >= '2026-01-01'))"]))
    assert A.agent_context(m)["window"] == ["customer.d >= '2026-01-01'"]
    assert m["expression"]["dialects"][0]["expression"] == "COUNT(CASE WHEN (customer.a = 1) THEN 1 END)"


def test_count_dataset_star_round_trips():
    e, ds, _ = _one(_rec("COUNT(m.*)"))
    assert e == "COUNT(customer.*)" and not ds[0].get("fields")
    rec = _rec("COUNT(*)")
    back = convert_ossie_to_registry(_doc(rec), rec["name"])["definition"]
    assert back["formula"] == "COUNT(m.*)"
    (m, _, _), = A.convert({**rec, "metadata": {"definition": {**rec["metadata"]["definition"], "formula": back["formula"]}}})
    assert m["expression"]["dialects"][0]["expression"] == "COUNT(customer.*)"


def test_table_name_case_follows_the_dialect():
    rec = _rec("COUNT(*)", dialect="bigquery")
    rec["metadata"]["definition"]["source"].update({"table": "my_proj.my_dataset.my_table", "entity": "my_proj.my_dataset.my_table"})
    assert _one(rec)[1][0]["source"] == "my_proj.my_dataset.my_table"
    rec = _rec("COUNT(*)")
    rec["metadata"]["definition"]["source"].update({"table": 'tpcds.public."Customer"', "entity": 'tpcds.public."Customer"'})
    assert _one(rec)[1][0]["source"] == 'TPCDS.PUBLIC."Customer"'


def test_placeholder_inside_a_string_literal_is_refused():
    rec = _rec("COUNT(*)", [{"name": "tag", "datatype": "string", "default": "promo"}], filters=["m.TAG = '{{tag}}'"])
    with pytest.raises(A.NotConvertible, match="inside a string literal"):
        A.convert(rec)


def test_qualified_timeframe_column_is_one_field():
    rec = _rec("COUNT(*)", dimensions=[{"name": "day", "kind": "time", "sql": "m.C_D", "datatype": "date"}])
    rec["metadata"]["definition"]["source"]["timeframe_column"] = f"{TABLE}.C_D"
    (m, ds, _), = A.convert(rec)
    assert A.agent_context(m)["timeframe_field"] == "customer.day" and [f["name"] for f in ds[0]["fields"]] == ["day"]


def test_bare_dimension_with_required_join_lands_on_the_joined_table():
    rec = _rec("COUNT(*)", joins=[{"name": "s", "to": "TPCDS.PUBLIC.STORE", "cardinality": "many_to_one",
                                  "sql": "s.S_STORE_SK = m.C_STORE_SK"}],
               dimensions=[{"name": "state", "kind": "categorical", "sql": "S_STATE", "datatype": "string", "required_join": "s"}])
    (m, _, _), = A.convert(rec)
    assert A.agent_context(m)["dimensions"] == ["store.state"]


def test_write_back_keeps_a_jinja_parameter_pick_and_speaks_the_record_aliases():
    rec = json.loads((golden.FIXTURES / "account_param_as_dimension" / "input.registry.json").read_text())
    doc = A.merge(A.convert(rec))
    m = doc["metrics"][0]
    for dl in m["expression"]["dialects"]:
        dl["expression"] = "2 * " + dl["expression"]
    patch = write_back(doc, m["name"], rec)["set"]
    assert patch["filters"] == ["TPCDS.PUBLIC.STORE_RETURNS.SR_STORE_SK = :store_sk"]
    rec = _rec("COUNT(*)", filters=["sr.SR_STATUS <> 'void'", "sr.SR_D >= '2026-01-01'"])
    rec["metadata"]["definition"]["source"]["alias"] = "sr"
    doc = _doc(rec)
    for dl in doc["metrics"][0]["expression"]["dialects"]:
        dl["expression"] = "2 * " + dl["expression"]
    patch = write_back(doc, doc["metrics"][0]["name"], rec)["set"]
    assert patch["formula"] == "2 * COUNT(CASE WHEN (sr.SR_STATUS <> 'void') THEN 1 END)"
    assert patch["filters"] == ["sr.SR_D >= '2026-01-01'"]


def test_expression_edit_on_a_row_set_or_whole_query_record_moves_the_source_too():
    rec = json.loads((golden.FIXTURES / "count_filtered" / "input.registry.json").read_text())   # source.sql with a WHERE
    doc = _doc(rec)
    for dl in doc["metrics"][0]["expression"]["dialects"]:
        dl["expression"] = "COUNT(customer.*)"                                  # the editor removed the filter
    patch = write_back(doc, doc["metrics"][0]["name"], rec)["set"]
    assert patch["formula"] == "COUNT(m.*)" and patch["source.sql"] is None and patch["source.table"] == "TPCDS.PUBLIC.CUSTOMER"
    rec = _rec(f"SELECT C_BIRTH_COUNTRY, COUNT(*) AS n FROM {TABLE} s WHERE s.C_F = 'Y' GROUP BY 1")
    doc = _doc(rec)
    for dl in doc["metrics"][0]["expression"]["dialects"]:
        dl["expression"] = "2 * " + dl["expression"]
    patch = write_back(doc, doc["metrics"][0]["name"], rec)["set"]
    assert [d["sql"] for d in patch["dimensions"]] == ["s.C_BIRTH_COUNTRY"] and patch["source.alias"] == "s"


def test_deleting_the_datatype_in_ossie_is_no_patch():
    rec = _rec("COUNT(*)")
    doc = _doc(rec)
    doc["metrics"][0].pop("datatype", None)
    assert write_back(doc, doc["metrics"][0]["name"], rec) == {}


def test_reverse_picks_the_portable_dialect_entry_and_the_root_dataset():
    rec = _rec("COUNT(*)")
    doc = _doc(rec)
    doc["metrics"][0]["expression"]["dialects"].insert(0, {"dialect": "BIGQUERY", "expression": "COUNT(DISTINCT `x y`)"})
    assert convert_ossie_to_registry(doc, rec["name"])["definition"]["formula"] == "COUNT(m.*)"
    rec = _rec("COUNT(DISTINCT c.C_CUSTOMER_SK)", joins=[{"name": "c", "to": "TPCDS.PUBLIC.CUSTOMER", "cardinality": "many_to_one",
                                                          "sql": "c.C_CUSTOMER_SK = m.SS_CUSTOMER_SK"}])
    rec["metadata"]["definition"]["source"].update({"table": "TPCDS.PUBLIC.STORE_SALES", "entity": "TPCDS.PUBLIC.STORE_SALES"})
    back = convert_ossie_to_registry(_doc(rec), rec["name"])["definition"]       # reads only the joined table
    assert back["source"]["table"] == "TPCDS.PUBLIC.STORE_SALES" and back["formula"] == "COUNT(DISTINCT t1.C_CUSTOMER_SK)"
    assert back["joins"][0]["sql"] == "m.SS_CUSTOMER_SK = t1.C_CUSTOMER_SK"


def test_example_questions_keep_their_sql():
    rec = _rec("COUNT(*)", examples=[{"question": "Q-A"}, {"question": "Q-B", "sql": "SELECT 'B'"}])
    back = convert_ossie_to_registry(_doc(rec), rec["name"])["definition"]["examples"]
    assert [(e.get("question"), e["sql"]) for e in back] == [("Q-B", "SELECT 'B'"), ("Q-A", "")]


# ---- PR review: joins with unqualified columns, zero_denominator round trip, backticked names

def test_unqualified_column_in_a_query_that_joins_tables_is_refused():
    """SQL resolves a bare column against every joined table; guessing the base table would write
    `store_sales.c_birth_country`, a field that does not exist."""
    join = [{"name": "c", "to": TABLE, "cardinality": "many_to_one", "sql": "s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK"}]
    rec = _rec("SELECT C_BIRTH_COUNTRY, SUM(SS_NET_PAID) AS paid FROM TPCDS.PUBLIC.STORE_SALES s "
               f"JOIN {TABLE} c ON s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK GROUP BY C_BIRTH_COUNTRY", joins=join)
    with pytest.raises(A.NotConvertible, match="C_BIRTH_COUNTRY, SS_NET_PAID not qualified by a table"):
        A.convert(rec)
    rec = _rec("SELECT c.C_BIRTH_COUNTRY, SUM(s.SS_NET_PAID) AS paid FROM TPCDS.PUBLIC.STORE_SALES s "
               f"JOIN {TABLE} c ON s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK GROUP BY c.C_BIRTH_COUNTRY", joins=join)
    (m, ds, _), = A.convert(rec)                                    # qualified: converts
    assert m["expression"]["dialects"][0]["expression"] == "SUM(store_sales.ss_net_paid)"
    assert A.agent_context(m)["dimensions"] == ["customer.c_birth_country"]
    rec = _rec("COUNT(*)")                                          # a row-set source with a join
    rec["metadata"]["definition"]["source"] = {
        "alias": "m", "parameters": [], "entity": "TPCDS.PUBLIC.STORE_SALES",
        "sql": f"SELECT * FROM TPCDS.PUBLIC.STORE_SALES m JOIN {TABLE} c ON m.SS_CUSTOMER_SK = c.C_CUSTOMER_SK "
               "WHERE C_BIRTH_COUNTRY = 'FRANCE'"}
    with pytest.raises(A.NotConvertible, match="C_BIRTH_COUNTRY not qualified by a table"):
        A.convert(rec)
    src = rec["metadata"]["definition"]["source"]
    src["sql"] = src["sql"].split(" WHERE ")[0]
    for kw in ({"filters": ["C_BIRTH_COUNTRY = 'FRANCE'"]}, {"formula": "SUM(C_BALANCE)"}):
        bare = json.loads(json.dumps(rec))
        bare["metadata"]["definition"].update(kw)
        with pytest.raises(A.NotConvertible, match="not qualified by a table"):
            A.convert(bare)


def test_zero_denominator_round_trip_is_idempotent():
    """forward -> edit -> write-back -> forward gives the edited expression, wrapped once."""
    rec = _rec("SUM(m.C_BALANCE) / SUM(m.C_LIMIT)", aggregation={"type": "ratio", "zero_denominator": "zero"})
    doc = _doc(rec)
    m = doc["metrics"][0]
    assert m["expression"]["dialects"][0]["expression"] == \
        "COALESCE(SUM(customer.c_balance) / SUM(customer.c_limit), 0)"
    assert write_back(doc, m["name"], rec) == {}
    for dl in m["expression"]["dialects"]:
        dl["expression"] = dl["expression"].replace("SUM(customer.c_limit)", "(SUM(customer.c_limit) + 1)")
    patch = write_back(doc, m["name"], rec)["set"]
    assert patch["formula"] == "SUM(m.C_BALANCE) / (SUM(m.C_LIMIT) + 1)"         # the wrapper is not in the formula
    assert "aggregation.zero_denominator" not in patch
    again = json.loads(json.dumps(rec))
    again["metadata"]["definition"]["formula"] = patch["formula"]
    (m2, _, _), = A.convert(again)
    assert m2["expression"] == m["expression"]                                    # forward again: identical
    for dl in m["expression"]["dialects"]:                                         # wrapper removed in Ossie
        dl["expression"] = dl["expression"][len("COALESCE("):-len(", 0)")]
    assert write_back(doc, m["name"], rec)["set"]["aggregation.zero_denominator"] == "null"


def test_a_coalesce_the_author_wrote_is_part_of_the_formula():
    """Only a record whose zero_denominator is zero has its COALESCE(<expr>, 0) read as that
    setting; anywhere else the COALESCE is the author's SQL and must survive a write-back."""
    rec = _rec("COALESCE(SUM(m.C_BALANCE), 0)", aggregation={"type": "sum", "zero_denominator": "null"})
    doc = _doc(rec)
    for dl in doc["metrics"][0]["expression"]["dialects"]:
        dl["expression"] = "COALESCE(SUM(customer.c_balance) * 2, 0)"
    patch = write_back(doc, doc["metrics"][0]["name"], rec)["set"]
    assert patch["formula"] == "COALESCE(SUM(m.C_BALANCE) * 2, 0)" and "aggregation.zero_denominator" not in patch
    rec = _rec("COALESCE(MAX(m.C_FIRST_NAME), '0')")                   # a string '0' is never the setting
    back = convert_ossie_to_registry(_doc(rec), rec["name"])["definition"]
    assert back["formula"] == "COALESCE(MAX(m.C_FIRST_NAME), '0')" and "zero_denominator" not in back["aggregation"]
    rec = _rec("COALESCE(SUM(m.C_BALANCE) / SUM(m.C_LIMIT), 0)", aggregation={"type": "ratio", "zero_denominator": "zero"})
    assert _one(rec)[0] == "COALESCE(SUM(customer.c_balance) / SUM(customer.c_limit), 0)"   # not wrapped twice


def test_backticked_bigquery_names():
    rec = _rec("COUNT(DISTINCT `tbl`.cust_id)", dialect="bigquery")
    rec["metadata"]["definition"]["source"].update({"table": "`proj`.`ds`.`tbl`", "entity": "`proj`.`ds`.`tbl`"})
    (m, ds, _), = A.convert(rec)
    assert ds[0]["name"] == "tbl" and ds[0]["source"] == "`proj`.`ds`.`tbl`"     # quotes as written
    assert m["expression"]["dialects"][0]["expression"] == "COUNT(DISTINCT tbl.cust_id)"
    back = convert_ossie_to_registry(_doc(rec), rec["name"])["definition"]
    assert back["source"]["table"] == "`proj`.`ds`.`tbl`" and back["formula"] == "COUNT(DISTINCT m.cust_id)"
    for table, name, source in [("`proj.ds.tbl`", "tbl", "`proj`.`ds`.`tbl`"),         # the path in one pair
                                ("`proj`.`ds`.`my tbl`", "my_tbl", "`proj`.`ds`.`my tbl`"),
                                ("`proj`.`ds`.`order`", "ds_order", "`proj`.`ds`.`order`"),
                                ("`proj`.`ds`.`current_date`", "ds_current_date", "`proj`.`ds`.`current_date`")]:
        rec["metadata"]["definition"]["source"].update({"table": table, "entity": table})
        rec["metadata"]["definition"]["formula"] = "COUNT(*)"
        _, ds, _ = _one(rec)
        assert (ds[0]["name"], ds[0]["source"]) == (name, source), table
    snow = _rec("COUNT(*)")
    snow["metadata"]["definition"]["source"].update({"table": "`a`.`b`.`c`", "entity": "`a`.`b`.`c`"})
    with pytest.raises(A.NotConvertible, match="quoted in a way this dialect does not use"):
        A.convert(snow)



# ---- second verification round

@pytest.mark.parametrize("table", ["IDENTIFIER('x')", "@stage", "$1", "TABLE(FLATTEN(x))", "a.b.c.1tbl"])
def test_table_names_that_are_not_plain_references_are_refused(table):
    bad = _rec("COUNT(*)")
    bad["metadata"]["definition"]["source"].update({"table": table, "entity": table})
    bad["name"], bad["id"] = "bad", "metric_bad"
    with pytest.raises(A.NotConvertible, match="not a plain table reference|does not parse"):
        A.convert(bad)
    doc, issues = convert_registry_to_ossie([_rec("COUNT(*)"), bad])           # the rest of the batch converts
    assert [m["name"] for m in doc["metrics"]] == ["preferred_customer_count"]
    assert [i.element_name for i in issues] == ["bad"]


@pytest.mark.parametrize("dialect, table, source", [
    ("snowflake", 'db.sch."TO"', 'DB.SCH."TO"'),               # quotes are kept as written
    ("snowflake", 'db.sch."ACCOUNT"', 'DB.SCH."ACCOUNT"'),
    ("postgres", 'pub."do"', 'pub."do"'),
    ("snowflake", 'db.sch."Mixed"', 'DB.SCH."Mixed"'),
    ("snowflake", "db.sch.tbl", "DB.SCH.TBL"),                 # unquoted parts fold as the dialect folds
    ("bigquery", "proj.ds.Tbl", "proj.ds.Tbl"),                # BigQuery keeps table-name case
    ("bigquery", "`proj.ds.Tbl`", "`proj`.`ds`.`Tbl`"),
    ("tsql", "[srv].[db].[dbo].[orders]", "[srv].[db].[dbo].[orders]"),     # four parts
    ("snowflake", '"srv"."proj"."ds"."tbl"', '"srv"."proj"."ds"."tbl"'),
    ("redshift", 's."Orders"', 's."Orders"'),                 # quoted parts keep their case as written
    ("databricks", "`c`.`s`.`Tbl`", "`c`.`s`.`Tbl`"),
    ("bigquery", "`Orders`", "`Orders`"),
])
def test_table_name_spelling(dialect, table, source):
    rec = _rec("COUNT(*)", dialect=dialect)
    rec["metadata"]["definition"]["source"].update({"table": table, "entity": table})
    assert _one(rec)[1][0]["source"] == source


def test_a_declared_join_matches_its_sql_join_however_each_is_quoted():
    rec = _rec("COUNT(*)", dialect="bigquery",
               joins=[{"name": "c", "to": "`proj`.`ds`.`cust`", "cardinality": "many_to_one", "sql": "m.cid = c.id"}])
    rec["metadata"]["definition"]["source"].update({"table": None, "entity": "proj.ds.ss",
                                                    "sql": "proj.ds.ss m JOIN proj.ds.cust c ON m.cid = c.id"})
    (m, ds, rels), = A.convert(rec)
    assert [r["name"] for r in rels] == ["ss_to_cust"]


def test_whole_query_with_a_join_refuses_bare_columns_in_its_structured_fields():
    join = [{"name": "c", "to": TABLE, "cardinality": "many_to_one", "sql": "s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK"}]
    q = (f"SELECT SUM(s.SS_NET_PAID) AS paid FROM TPCDS.PUBLIC.STORE_SALES s "
         f"JOIN {TABLE} c ON s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK")
    with pytest.raises(A.NotConvertible, match="C_PREFERRED_CUST_FLAG not qualified by a table"):
        A.convert(_rec(q, joins=join, filters=["C_PREFERRED_CUST_FLAG = 'Y'"]))
    rec = _rec(q, joins=join)
    rec["metadata"]["definition"]["source"]["timeframe_column"] = "C_FIRST_SALES_DATE"
    with pytest.raises(A.NotConvertible, match="C_FIRST_SALES_DATE not qualified by a table"):
        A.convert(rec)
    e, _, _ = _one(_rec(q, joins=join, filters=["c.C_PREFERRED_CUST_FLAG = 'Y'"]))
    assert e == "SUM(CASE WHEN (customer.c_preferred_cust_flag = 'Y') THEN store_sales.ss_net_paid END)"


def test_required_join_must_name_a_join_of_the_record():
    rec = _rec("COUNT(*)", joins=[{"name": "s", "to": "TPCDS.PUBLIC.STORE", "cardinality": "many_to_one",
                                  "sql": "s.S_STORE_SK = m.C_STORE_SK"}],
               dimensions=[{"name": "state", "kind": "categorical", "sql": "S_STATE", "datatype": "string",
                            "required_join": "store"}])
    with pytest.raises(A.NotConvertible, match="required_join 'store' names no join"):
        A.convert(rec)



def test_bare_column_in_a_whole_query_on_clause_is_refused():
    join = [{"name": "c", "to": TABLE, "cardinality": "many_to_one", "sql": "s.SS_CUSTOMER_SK = c.C_CUSTOMER_SK"}]
    rec = _rec(f"SELECT SUM(s.SS_NET_PAID) AS paid FROM TPCDS.PUBLIC.STORE_SALES s JOIN {TABLE} c "
               "ON SS_CUSTOMER_SK = c.C_CUSTOMER_SK", joins=join)
    with pytest.raises(A.NotConvertible, match="SS_CUSTOMER_SK not qualified by a table"):
        A.convert(rec)


def test_filter_form_on_a_joined_dimension_names_that_table():
    rec = _rec("COUNT(*)", joins=[{"name": "s", "to": "TPCDS.PUBLIC.STORE", "cardinality": "many_to_one",
                                  "sql": "s.S_STORE_SK = m.C_STORE_SK"}],
               dimensions=[{"name": "state", "kind": "categorical", "sql": "s.S_STATE", "datatype": "string",
                            "filter_form": "S_STATE = :value"}])
    _, ds, _ = _one(rec)
    f = next(x for d in ds for x in d.get("fields", []) if x["name"] == "state")
    assert f["ai_context"] == "Filter as: store.state = :value."


@pytest.mark.parametrize("dialect, table", [("snowflake", "[x]"), ("tsql", "Sch.select.[user]")])
def test_quoting_the_dialect_does_not_use_is_refused(dialect, table):
    rec = _rec("COUNT(*)", dialect=dialect)
    rec["metadata"]["definition"]["source"].update({"table": table, "entity": table})
    with pytest.raises(A.NotConvertible):
        A.convert(rec)
