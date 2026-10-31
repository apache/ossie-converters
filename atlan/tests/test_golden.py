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
from ossie_atlan import ConversionError, IssueType, cli, convert_ossie_to_registry, convert_registry_to_ossie, load
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
    assert "METRIC_NOT_CONVERTIBLE] x: record has no name, id or metadata.definition" in capsys.readouterr().err
    bad.write_text("{\"foo\": 1}")
    assert cli.main(["export", "-i", str(bad), "--metric", "x"]) == 1
    assert "not an Ossie document" in capsys.readouterr().err
    with pytest.raises(ConversionError, match="no metric named"):
        convert_ossie_to_registry(yaml.safe_load(out.read_text()), "nope")
    one = tmp_path / "one.json"
    one.write_text(json.dumps(_input("count_filtered")))
    assert cli.main(["write-back", "-i", str(out), "--metric", "preferred_customer_count", "--record", str(one)]) == 0
    assert json.loads(capsys.readouterr().out) == {}
