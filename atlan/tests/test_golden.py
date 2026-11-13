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

"""The golden cases in tests/fixtures/ are the converter's contract: input -> expected output.
Regenerate after an intended change with `uv run python tests/_golden.py --write` and review the diff."""

import importlib.util
import json
import pathlib

import pytest
import yaml

import _golden as golden
from ossie_atlan import (ConversionError, IssueType, NotWritable, cli, convert_ossie_to_records, convert_ossie_to_registry,
                         convert_registry_to_ossie, load, write_back)
from ossie_atlan import converter as A

HERE = pathlib.Path(__file__).resolve().parent
OSSIE = HERE.parents[1] / "ossie"          # the apache/ossie submodule at the repository root
OK = [n for n in golden.CASES if not n.startswith("refused")]
REFUSED = [n for n in golden.CASES if n.startswith("refused")]


def _ossie_yaml(name):
    return yaml.safe_load((golden.FIXTURES / name / "expected.ossie.yaml").read_text())


def _input(name):
    return json.loads((golden.FIXTURES / name / "input.registry.json").read_text())


@pytest.mark.parametrize("name", list(golden.CASES))
def test_case_matches_its_golden_files(name):
    want = golden.build(name)
    have = {p.name: p.read_text() for p in (golden.FIXTURES / name).glob("*")}
    assert set(have) == set(want), f"{name}: files differ"
    for fn in want:
        assert have[fn] == want[fn], f"{name}/{fn} differs from what the converter produces now"


@pytest.mark.parametrize("name", OK)
def test_expected_ossie_validates_against_the_core_spec(name):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = OSSIE / "core-spec" / "ossie-schema.json"
    if not schema_path.exists():
        pytest.skip("apache/ossie submodule not checked out")
    jsonschema.validate(_ossie_yaml(name), json.loads(schema_path.read_text()))


@pytest.mark.parametrize("name", OK)
def test_expected_ossie_passes_ossie_own_validator(name):
    """More than the schema: unique names, references, key arity and SQL parse, with the
    validator apache/ossie ships."""
    path = OSSIE / "validation" / "validate.py"
    if not path.exists():
        pytest.skip("apache/ossie submodule not checked out")
    spec = importlib.util.spec_from_file_location("ossie_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc = _ossie_yaml(name)
    schema = json.loads((OSSIE / "core-spec" / "ossie-schema.json").read_text())
    msgs = (mod.validate_schema(doc, schema) + mod.validate_unique_names(doc) + mod.validate_references(doc)
            + mod.validate_relationship_column_arity(doc) + mod.validate_sql(doc))
    errors = [m for m in msgs if not isinstance(m, mod.ValidationWarning)]
    assert errors == []


@pytest.mark.parametrize("name", OK)
def test_unedited_file_writes_back_nothing(name):
    wb = json.loads((golden.FIXTURES / name / "expected.writeback.json").read_text())
    assert all(v == {} for v in wb.values())


@pytest.mark.parametrize("name", OK)
def test_rebuilt_definition_validates_against_the_registry_schema(name):
    """Ossie -> registry output is a valid metric definition under the registry's own kind schema."""
    jsonschema = pytest.importorskip("jsonschema")
    kind = json.loads((HERE / "fixtures" / "metric_kind.json").read_text())
    v = jsonschema.Draft202012Validator(kind)
    back = json.loads((golden.FIXTURES / name / "expected.registry.json").read_text())
    for metric, rec in back.items():
        assert not list(v.iter_errors(rec["definition"])), metric


@pytest.mark.parametrize("name", REFUSED)
def test_refused_cases_say_why(name):
    with pytest.raises(A.NotConvertible) as e:
        A.convert(_input(name))
    assert str(e.value) == json.loads((golden.FIXTURES / name / "expected.refusal.json").read_text())["reason"]


def test_an_edit_in_ossie_becomes_a_versioned_registry_patch():
    d = golden.FIXTURES / "count_filtered"
    patch = json.loads((d / "expected.writeback_edit.json").read_text())
    edit = json.loads((d / "edit.json").read_text())
    assert patch["new_version"] == patch["expected_version"] + 1
    assert patch["set"] == {"description": edit["description"], "grain": edit["agent_context.grain"]}


def test_display_name_is_not_carried_and_the_registry_block_is_id_and_version():
    """The registry holds display_name under the id we already carry (decision: no second copy)."""
    m = _ossie_yaml("count_rows")["metrics"][0]
    ext = json.loads(m["custom_extensions"][0]["data"])
    assert ext["registry"] == {"id": "metric_synth_store_sales_count", "definition_version": 1}
    assert "display_name" not in json.dumps(ext)


def test_split_query_carries_example_sql_on_its_first_part_only():
    first, second = _ossie_yaml("ratio_round_window")["metrics"]
    c1 = json.loads(first["custom_extensions"][0]["data"])["agent_context"]
    c2 = json.loads(second["custom_extensions"][0]["data"])["agent_context"]
    assert c1["examples"] and "examples" not in c2 and "verified_sql" not in c2
    assert json.loads(first["custom_extensions"][0]["data"])["registry"]["id"] == \
        json.loads(second["custom_extensions"][0]["data"])["registry"]["id"]


def test_account_parameter_becomes_a_dimension():
    m = _ossie_yaml("account_param_as_dimension")["metrics"][0]
    dims = json.loads(m["custom_extensions"][0]["data"])["agent_context"]["dimensions"]
    assert any(x.endswith(".store") for x in dims)
    assert ":store_sk" not in m["expression"]["dialects"][0]["expression"]


def test_reverse_restores_parameters_and_the_window_is_text_only():
    """A rebuilt registry record keeps the parameter defaults; a fixed date range is not a
    structured key anywhere in Ossie, only a sentence in the instructions."""
    doc = _ossie_yaml("ratio_joins_dims_window")
    m0 = load(doc)["metrics"][0]
    assert A.agent_context(m0)["window"] == ["store_sales.ss_sold_timestamp >= :start_date",
                                             "store_sales.ss_sold_timestamp < DATEADD(DAY, 1, :end_date)"]
    assert ":start_date" not in m0["expression"]["dialects"][0]["expression"]
    assert "Date range fixed in the source query" in m0["ai_context"]["instructions"]
    rec = convert_ossie_to_registry(doc, "sales_per_shopper")
    assert [p["name"] for p in rec["definition"]["source"]["parameters"]] == ["start_date", "end_date"]
    assert rec["definition"]["filters"] == ["m.SS_SOLD_TIMESTAMP >= :start_date",
                                            "m.SS_SOLD_TIMESTAMP < DATEADD(DAY, 1, :end_date)"]   # restored, in the record's aliases


def test_all_cases_import_as_one_file_with_one_issue_per_refusal():
    recs = [_input(n) for n in golden.CASES]
    doc, issues = convert_registry_to_ossie(recs, name="all")
    refused = {i.element_name for i in issues.of_type(IssueType.METRIC_NOT_CONVERTIBLE)}
    assert refused == {_input(n)["name"] for n in REFUSED}
    # two cases define one name two ways (store_sales.sale_date with two expressions; customer
    # with two keys): one file cannot carry both, so the later record is left out with an issue
    conflicts = issues.of_type(IssueType.MERGE_CONFLICT)
    # (and the BigQuery case's store_sales is spelled in another dialect than the Snowflake ones)
    assert {c.element_name for c in conflicts} == {"sales_to_active_customers", "store_sale_errors",
                                                   "paid_by_birth_country"}
    assert "_merge_notes" not in doc and "merge_notes" not in json.dumps(doc)
    assert len(doc["metrics"]) == sum(len(_ossie_yaml(n)["metrics"]) for n in OK) - len(conflicts)


def test_cli_import_export_write_back(tmp_path, capsys):
    recs = tmp_path / "records.json"
    recs.write_text(json.dumps([_input("count_filtered"), _input("refused_row_listing")]))
    out = tmp_path / "model.yaml"
    assert cli.main(["import", "-i", str(recs), "-o", str(out), "--name", "m"]) == 0
    err = capsys.readouterr().err
    assert "[METRIC_NOT_CONVERTIBLE] customer_id_by_email" in err and "1 Ossie metrics from 2" in err
    assert cli.main(["export", "-i", str(out), "--metric", "preferred_customer_count"]) == 0
    assert json.loads(capsys.readouterr().out)["definition"]["formula"] == \
        "COUNT(CASE WHEN (m.C_PREFERRED_CUST_FLAG = 'Y') THEN 1 END)"
    assert cli.main(["export", "-i", str(out), "--metric", "nope"]) == 1
    assert "no metric named 'nope'" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "x"}]))
    assert cli.main(["import", "-i", str(bad)]) == 1
    assert "METRIC_NOT_CONVERTIBLE] x: record has no name or metadata.definition" in capsys.readouterr().err
    bad.write_text("{\"foo\": 1}")
    assert cli.main(["export", "-i", str(bad), "--metric", "x"]) == 1
    assert "not an Ossie document" in capsys.readouterr().err
    with pytest.raises(ConversionError, match="no metric named"):
        convert_ossie_to_registry(yaml.safe_load(out.read_text()), "nope")
    one = tmp_path / "one.json"
    one.write_text(json.dumps(_input("count_filtered")))
    assert cli.main(["write-back", "-i", str(out), "--metric", "preferred_customer_count", "--record", str(one)]) == 0
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize("name", OK)
def test_export_of_a_whole_file_imports_back_to_the_same_metrics(name):
    """export with no --metric: every metric as a registry record, which import reads back to
    the same metric expressions. A metric from the registry keeps its id and workspace; the
    parts of a split query share one id, so each part goes out as a new metric."""
    doc = _ossie_yaml(name)
    recs, issues = convert_ossie_to_records(doc)
    assert not issues
    split = len(doc["metrics"]) > 1
    for r in recs["artifacts"]:
        assert r["kind"] == "metric" and r["workspace_id"] == _input(name)["workspace_id"]
        assert r.get("id") == (None if split else _input(name)["id"])
    back, issues = convert_registry_to_ossie(recs["artifacts"], name=doc["name"])
    assert not issues
    assert {m["name"]: A.expression_text(m) for m in load(back)["metrics"]} == \
        {m["name"]: A.expression_text(m) for m in load(doc)["metrics"]}


def test_record_without_an_id_imports_as_a_new_metric_and_cannot_write_back():
    rec = {k: v for k, v in _input("count_filtered").items() if k != "id"}
    doc, issues = convert_registry_to_ossie([rec])
    assert not issues
    m = load(doc)["metrics"][0]
    assert "id" not in A._registry_of(m)
    assert A.expression_text(m) == A.expression_text(load(_ossie_yaml("count_filtered"))["metrics"][0])
    with pytest.raises(NotWritable, match="no registry id"):
        write_back(doc, m["name"], _input("count_filtered"))


def test_cli_export_of_a_whole_file(tmp_path, capsys):
    doc = _ossie_yaml("count_filtered")
    doc["metrics"].append({**doc["metrics"][0], "name": "broken",
                           "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM("}]}})
    src, out = tmp_path / "model.yaml", tmp_path / "records.json"
    src.write_text(yaml.safe_dump(doc))
    assert cli.main(["export", "-i", str(src), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "[METRIC_NOT_CONVERTIBLE] broken" in err and "1 registry records from 2 Ossie metrics" in err
    assert [r["name"] for r in json.loads(out.read_text())["artifacts"]] == ["preferred_customer_count"]
    doc["metrics"] = doc["metrics"][1:]
    src.write_text(yaml.safe_dump(doc))
    assert cli.main(["export", "-i", str(src)]) == 1
    assert "nothing to write: no metric converted" in capsys.readouterr().err


def _with_dataset_metric(expression, name="total_quantity"):
    doc = _ossie_yaml("count_rows")
    doc["datasets"][0]["fields"] = [{"name": "ss_quantity", "expression": {"dialects": [
        {"dialect": "ANSI_SQL", "expression": "SS_QUANTITY"}]}}]
    doc["datasets"][0]["metrics"] = [{"name": name, "description": "units sold", "expression": {
        "dialects": [{"dialect": "ANSI_SQL", "expression": expression}]}}]
    return doc


def test_dataset_scoped_metric_exports_with_its_own_dataset():
    """datasets[].metrics name their dataset's fields unqualified; export reads them as that
    dataset's columns, so the metric is not dropped."""
    recs, issues = convert_ossie_to_records(_with_dataset_metric("SUM(ss_quantity)"))
    assert not issues
    assert {r["name"]: r["metadata"]["definition"]["formula"] for r in recs["artifacts"]} == \
        {"store_sales_count": "COUNT(m.*)", "total_quantity": "SUM(m.SS_QUANTITY)"}
    assert convert_ossie_to_registry(_with_dataset_metric("SUM(ss_quantity)"), "total_quantity")[
        "definition"]["formula"] == "SUM(m.SS_QUANTITY)"


def test_dataset_scoped_metric_refusals():
    _, issues = convert_ossie_to_records(_with_dataset_metric("SUM(ss_quantity)", name="store_sales_count"))
    assert [(i.element_name, i.detail) for i in issues] == \
        [("store_sales_count", "2 metrics are named 'store_sales_count'; a registry name is unique")]
    _, issues = convert_ossie_to_records(_with_dataset_metric("store_sales_count / 2"))
    assert [i.element_name for i in issues] == ["total_quantity"]
    assert "built from other metrics" in issues.of_type(IssueType.METRIC_NOT_CONVERTIBLE)[0].detail


def test_cli_export_of_a_file_with_no_metrics(tmp_path, capsys):
    """Metrics are optional in Ossie; the registry holds only metrics, so the export is empty."""
    doc = _ossie_yaml("count_rows")
    del doc["metrics"]
    src, out = tmp_path / "model.yaml", tmp_path / "records.json"
    src.write_text(yaml.safe_dump(doc))
    assert cli.main(["export", "-i", str(src), "-o", str(out)]) == 0
    assert json.loads(out.read_text()) == {"artifacts": []}
    assert "0 registry records from 0 Ossie metrics" in capsys.readouterr().err


def test_dataset_scoped_metric_reads_its_own_field_over_a_metric_of_that_name():
    """the spec: a dataset metric names its dataset's fields unqualified, so a field `amount`
    is that field even when a model metric is also called `amount`"""
    doc = _with_dataset_metric("SUM(ss_quantity)")
    doc["metrics"].append({**doc["metrics"][0], "name": "ss_quantity",
                           "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "COUNT(store_sales.*)"}]}})
    recs, issues = convert_ossie_to_records(doc)
    assert not issues
    assert {r["name"]: r["metadata"]["definition"]["formula"] for r in recs["artifacts"]}["total_quantity"] == \
        "SUM(m.SS_QUANTITY)"


def test_expression_given_as_top_level_sql():
    """apache/ossie#617: an expression may carry only `sql` (OSSIE_SQL_2026), and `sql` wins over
    an OSSIE_SQL_2026 entry in `dialects`."""
    doc = _with_dataset_metric("SUM(ss_quantity)")
    doc["datasets"][0]["fields"][0]["expression"] = {"sql": "SS_QUANTITY"}
    doc["datasets"][0]["metrics"][0]["expression"] = {"sql": "SUM(ss_quantity)"}
    assert convert_ossie_to_registry(doc, "total_quantity")["definition"]["formula"] == "SUM(m.SS_QUANTITY)"
    doc["datasets"][0]["metrics"][0]["expression"]["dialects"] = [{"dialect": A.OSSIE_DIALECT, "expression": "COUNT(*)"}]
    assert convert_ossie_to_registry(doc, "total_quantity")["definition"]["formula"] == "SUM(m.SS_QUANTITY)"
    doc = _ossie_yaml("bound_placeholder_param")
    want = convert_ossie_to_registry(doc, "csat")["definition"]
    m = doc["metrics"][0]
    text = A.expression_text(m)
    m["expression"] = {"sql": text}
    assert convert_ossie_to_registry(doc, "csat")["definition"] == want
    m["expression"] = {"sql": text, "dialects": [{"dialect": A.OSSIE_DIALECT, "expression": "COUNT(*)"}]}
    assert convert_ossie_to_registry(doc, "csat")["definition"] == want
    m["expression"] = {}
    _, issues = convert_ossie_to_records(doc)
    assert [(i.element_name, i.detail) for i in issues] == [("csat", "expression has neither sql nor dialects")]


def test_agent_context_naming_a_removed_field_is_refused_not_a_crash(tmp_path, capsys):
    """A field renamed in the Ossie file while agent_context still names it has nothing to
    rebuild the dimension or time field from."""
    for case, old in [("bound_placeholder_param", "answer_date"), ("mandatory_filter_time_dim", "sr_returned_date")]:
        doc = _ossie_yaml(case)
        f = next(f for d in doc["datasets"] for f in d.get("fields", []) if f["name"] == old)
        f["name"] = "renamed"
        _, issues = convert_ossie_to_records(doc)
        details = [i.detail for i in issues]
        assert len(details) == 1 and f"declares a field {old!r}" in details[0], (case, details)
    src, out = tmp_path / "model.yaml", tmp_path / "records.json"
    src.write_text(yaml.safe_dump(doc))
    assert cli.main(["export", "-i", str(src), "-o", str(out)]) == 1
    assert "declares a field 'sr_returned_date'" in capsys.readouterr().err


def test_agent_context_time_field_must_name_a_declared_field():
    """A renamed time field is refused, where it was read as a raw column of the old name."""
    doc = _ossie_yaml("mandatory_filter_time_dim")
    ext = doc["metrics"][0]["custom_extensions"][0]
    data = json.loads(ext["data"])
    data["agent_context"].pop("dimensions", None)
    ext["data"] = json.dumps(data)
    f = next(f for d in doc["datasets"] for f in d.get("fields", []) if f["name"] == "sr_returned_date")
    f["name"] = "renamed"
    _, issues = convert_ossie_to_records(doc)
    assert ["declares a field 'sr_returned_date'" in i.detail for i in issues] == [True]
    data["agent_context"]["timeframe_field"] = "sr_returned_date"
    ext["data"] = json.dumps(data)
    _, issues = convert_ossie_to_records(doc)
    assert ["not a dataset.field reference" in i.detail for i in issues] == [True]
