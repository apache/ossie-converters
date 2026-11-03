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

"""Regression fixtures and mutations for the remaining interoperability traps."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest
import yaml

from interop.contracts import check_document, check_native_sources, table_parts
from interop.checks import Validator
from interop.manifest import (
    Manifest,
    ManifestError,
    _document_assertions,
    load_manifest,
)
from interop.report import write_report
from interop.runner import Runner, exit_code


ROOT = Path(__file__).resolve().parents[2]


def regression(name):
    return load_manifest(ROOT / "interop/manifest.yaml", ROOT).document_regressions[
        name
    ]


def document(name):
    return yaml.safe_load(regression(name).input.read_text())


@pytest.mark.parametrize(
    "name",
    [
        "dialect-databricks-only",
        "dialect-snowflake-only",
        "dialect-ossie-sql-2026-only",
        "keys-single",
        "keys-composite",
        "keys-unique-only",
        "keys-joined",
        "source-two-part",
        "source-three-part",
        "source-quoted-parts",
    ],
)
def test_independent_inputs_pass_core_validation_and_their_oracles(name):
    contract = regression(name)
    data, validation = Validator(ROOT).validate(contract.input)
    assert not validation["errors"], validation
    assert (
        check_document(data, contract.category, contract.assertions, input_stage=True)[
            "grade"
        ]
        == "OK"
    )
    assert set(contract.targets) == {"cube", "databricks", "dbt"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["datasets"][0]["fields"].pop(2),
        lambda data: data["datasets"][0]["fields"][2]["expression"]["dialects"][
            0
        ].update(expression="discounted_amount"),
        lambda data: data.update(metrics=[]),
        lambda data: data["metrics"][0]["expression"]["dialects"][0].update(
            expression="0"
        ),
        lambda data: data["metrics"][0]["expression"]["dialects"][0].update(
            expression="SUM(other.amount)"
        ),
        lambda data: data["metrics"][0]["expression"]["dialects"][0].update(
            dialect="DAX"
        ),
    ],
)
def test_missing_members_and_defaulted_or_wrong_expressions_fail(mutation):
    name = "dialect-databricks-only"
    data = document(name)
    mutation(data)
    assert (
        check_document(data, "dialect", regression(name).assertions)["grade"] == "FAIL"
    )


@pytest.mark.parametrize(
    "dialect", ["ANSI_SQL", "OSSIE_SQL_2026", "DATABRICKS", "SNOWFLAKE"]
)
def test_returned_dialect_labels_and_self_qualification_may_normalize(dialect):
    name = "dialect-databricks-only"
    data = document(name)
    for field in data["datasets"][0]["fields"]:
        field["expression"]["dialects"][0]["dialect"] = dialect
    data["datasets"][0]["fields"][2]["expression"]["dialects"][0]["expression"] = (
        "orders.amount * 0.9"
    )
    data["metrics"][0]["expression"]["dialects"][0].update(
        dialect=dialect, expression="sum(amount)"
    )
    assert check_document(data, "dialect", regression(name).assertions)["grade"] == "OK"


@pytest.mark.parametrize(
    "dialect", ["ANSI_SQL", "OSSIE_SQL_2026", "DATABRICKS", "SNOWFLAKE"]
)
@pytest.mark.parametrize("qualifier", ["ORDERS", "OrDeRs", '"orders"'])
def test_self_qualifier_ownership_normalizes_unquoted_case(dialect, qualifier):
    if dialect == "DATABRICKS":
        qualifier = qualifier.replace('"', "`")
    name = "dialect-databricks-only"
    data = document(name)
    data["metrics"][0]["expression"]["dialects"][0].update(
        dialect=dialect, expression=f"SUM({qualifier}.AMOUNT)"
    )
    data["datasets"][0]["fields"][2]["expression"]["dialects"][0].update(
        dialect=dialect, expression=f"{qualifier}.AMOUNT * 0.9"
    )
    assert check_document(data, "dialect", regression(name).assertions)["grade"] == "OK"


@pytest.mark.parametrize(
    "dialect", ["ANSI_SQL", "OSSIE_SQL_2026", "DATABRICKS", "SNOWFLAKE"]
)
@pytest.mark.parametrize(
    "qualifier", ['"ORDERS"', '"OrDeRs"', "customers", "analytics.orders"]
)
def test_qualifier_case_normalization_cannot_hide_other_owners(dialect, qualifier):
    if dialect == "DATABRICKS":
        qualifier = qualifier.replace('"', "`")
    name = "dialect-databricks-only"
    data = document(name)
    data["metrics"][0]["expression"]["dialects"][0].update(
        dialect=dialect, expression=f"SUM({qualifier}.amount)"
    )
    result = check_document(data, "dialect", regression(name).assertions)
    assert result["grade"] == "FAIL"
    assert any(
        "unexpected dataset qualifier" in (check["error"] or "")
        for check in result["checks"]
    )


def test_input_with_an_accidental_ansi_fallback_cannot_claim_dialect_only_coverage():
    name = "dialect-databricks-only"
    data = document(name)
    data["datasets"][0]["fields"][0]["expression"]["dialects"].append(
        {"dialect": "ANSI_SQL", "expression": "order_id"}
    )
    assert (
        check_document(data, "dialect", regression(name).assertions, input_stage=True)[
            "grade"
        ]
        == "FAIL"
    )


def test_primary_key_becoming_unique_is_reported_as_a_role_change():
    name = "keys-single"
    data = document(name)
    ds = data["datasets"][0]
    ds["unique_keys"].append(ds.pop("primary_key"))
    result = check_document(data, "keys", regression(name).assertions)
    assert result["grade"] == "FAIL"
    assert (
        next(
            check for check in result["checks"] if check["path"].endswith("primary_key")
        )["actual"]
        == []
    )


@pytest.mark.parametrize("name", ["keys-composite", "keys-unique-only"])
def test_composite_keys_cannot_be_flattened_into_independent_unique_columns(name):
    data = document(name)
    ds = data["datasets"][0]
    cols = ds.pop("primary_key", None) or ds.pop("unique_keys")[0]
    ds.setdefault("unique_keys", []).extend([[column] for column in cols])
    assert check_document(data, "keys", regression(name).assertions)["grade"] == "FAIL"


def test_key_column_order_is_not_a_false_preservation_failure():
    name = "keys-composite"
    data = document(name)
    data["datasets"][0]["primary_key"].reverse()
    assert check_document(data, "keys", regression(name).assertions)["grade"] == "OK"


def test_key_declaration_without_its_fields_is_not_preservation():
    name = "keys-single"
    data = document(name)
    data["datasets"][0]["fields"].pop(0)
    result = check_document(data, "keys", regression(name).assertions)
    assert result["grade"] == "FAIL"
    assert any(
        check["path"].endswith("key_fields") and check["grade"] == "FAIL"
        for check in result["checks"]
    )


def test_keys_are_checked_per_dataset_including_joined_dimensions():
    name = "keys-joined"
    data = document(name)
    data["datasets"][1].pop("primary_key")
    result = check_document(data, "keys", regression(name).assertions)
    assert any(
        check["path"] == "datasets.customer.primary_key" and check["grade"] == "FAIL"
        for check in result["checks"]
    )
    assert (
        next(
            check
            for check in result["checks"]
            if check["path"] == "datasets.orders.primary_key"
        )["grade"]
        == "OK"
    )


@pytest.mark.parametrize(
    "source",
    [
        "orders",
        "public.orders",
        "other.public.orders",
        "analytics.other.orders",
        "analytics.public.orders; other.public.orders",
    ],
)
def test_missing_or_changed_source_qualification_fails(source):
    name = "source-three-part"
    data = document(name)
    data["datasets"][0]["source"] = source
    assert (
        check_document(data, "sources", regression(name).assertions)["grade"] == "FAIL"
    )


def test_quoted_dots_are_identifier_content_not_extra_qualifiers():
    assert table_parts("`analytics.prod`.`sales.area`.`Orders`") == [
        "analytics.prod",
        "sales.area",
        "Orders",
    ]
    name = "source-quoted-parts"
    data = document(name)
    data["datasets"][0]["source"] = "analytics.prod.sales.area.Orders"
    assert (
        check_document(data, "sources", regression(name).assertions)["grade"] == "FAIL"
    )


def test_unquoted_case_normalization_preserves_identity():
    name = "source-three-part"
    data = document(name)
    data["datasets"][0]["source"] = "ANALYTICS.PUBLIC.ORDERS"
    assert check_document(data, "sources", regression(name).assertions)["grade"] == "OK"


@pytest.mark.parametrize("malformed", [False, True])
def test_native_dbt_slots_are_checked_even_when_the_source_string_round_trips(
    tmp_path, malformed
):
    name = "source-quoted-parts"
    contract = regression(name)
    assert (
        check_document(document(name), "sources", contract.assertions)["grade"] == "OK"
    )
    relation = {
        "database": "`analytics.prod`",
        "schema_name": "`sales.area`",
        "alias": "`Orders`",
        "relation_name": "`analytics.prod`.`sales.area`.`Orders`",
    }
    if malformed:
        relation.update(
            database="`analytics", schema_name="prod`", alias="`sales.area`.`Orders`"
        )
    path = tmp_path / "native.json"
    path.write_text(
        json.dumps({"semantic_models": [{"name": "orders", "node_relation": relation}]})
    )
    result = check_native_sources(path, "dbt", contract.assertions)
    assert result["grade"] == ("FAIL" if malformed else "OK")


@pytest.mark.parametrize("native_format", ["cube", "databricks", "dbt"])
def test_native_three_part_sources_pass_and_changed_catalog_fails(
    tmp_path, native_format
):
    contract = regression("source-three-part")
    if native_format == "cube":
        data = {"cubes": [{"name": "orders", "sql_table": "analytics.public.orders"}]}
        changed = {"cubes": [{"name": "orders", "sql_table": "other.public.orders"}]}
    elif native_format == "databricks":
        data = {"source": "analytics.public.orders"}
        changed = {"source": "other.public.orders"}
    else:
        data = {
            "semantic_models": [
                {
                    "name": "orders",
                    "node_relation": {
                        "database": "analytics",
                        "schema_name": "public",
                        "alias": "orders",
                    },
                }
            ]
        }
        changed = deepcopy(data)
        changed["semantic_models"][0]["node_relation"]["database"] = "other"
    path = tmp_path / ("native.json" if native_format == "dbt" else "native.yaml")
    path.write_text(json.dumps(data))
    assert (
        check_native_sources(path, native_format, contract.assertions)["grade"] == "OK"
    )
    path.write_text(json.dumps(changed))
    assert (
        check_native_sources(path, native_format, contract.assertions)["grade"]
        == "FAIL"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["document_regressions"]["keys-single"].update(
            category="unknown"
        ),
        lambda data: data["document_regressions"]["keys-single"].update(targets=[]),
        lambda data: data["document_regressions"]["keys-single"].update(
            targets=["cube", "cube"]
        ),
        lambda data: data["document_regressions"]["keys-single"].update(targets=[[]]),
        lambda data: data["document_regressions"]["keys-single"].update(
            targets=["unknown"]
        ),
        lambda data: data["document_regressions"]["keys-single"].update(
            input="interop/fixtures/contracts"
        ),
    ],
)
def test_document_regression_configuration_is_validated_before_execution(
    tmp_path, mutation
):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    mutation(data)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ManifestError):
        load_manifest(path, ROOT)


@pytest.mark.parametrize(
    "name, mutation",
    [
        (
            "keys-single",
            lambda data: data["datasets"]["orders"].update(primary_key="order_id"),
        ),
        (
            "keys-single",
            lambda data: data["datasets"]["orders"].update(
                unique_keys=[["a", "b"], ["b", "a"]]
            ),
        ),
        ("dialect-databricks-only", lambda data: data.update(dialect="DAX")),
        ("dialect-databricks-only", lambda data: data.update(metrics={})),
        ("source-three-part", lambda data: data.update(sources=[])),
        ("source-three-part", lambda data: data.update(version=True)),
    ],
)
def test_malformed_document_oracles_are_configuration_errors(tmp_path, name, mutation):
    contract = regression(name)
    data = deepcopy(contract.assertions)
    mutation(data)
    path = tmp_path / "oracle.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ManifestError):
        _document_assertions(path, contract.category)


@pytest.mark.parametrize(
    "name, mode, expected",
    [
        ("dialect-databricks-only", "valid", "OK"),
        ("dialect-databricks-only", "wrong-expression", "FAIL"),
        ("keys-single", "drop-key", "FAIL"),
        ("source-three-part", "wrong-native", "FAIL"),
        ("source-three-part", "wrong-source", "FAIL"),
        ("dialect-databricks-only", "empty", "EMPTY"),
        ("keys-single", "exit", "FAIL"),
        ("keys-single", "blocked", "SKIP"),
    ],
)
def test_document_runner_retains_failures_snapshots_and_strict_status(
    tmp_path, name, mode, expected
):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    contract = manifest.document_regressions[name]
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(document(name)))
    helper = tmp_path / "converter.py"
    helper.write_text(
        "import json, pathlib, sys\n"
        "action, mode, payload, inp, out = sys.argv[1:]\n"
        "data = json.loads(pathlib.Path(payload).read_text())\n"
        "if mode == 'exit': sys.exit(1)\n"
        "if action == 'export':\n"
        "    data = {'source': 'other.public.orders' if mode == 'wrong-native' else data['datasets'][0]['source'],\n"
        "            'dimensions': [] if mode == 'empty' else [{'name': 'amount', 'expr': 'amount'}]}\n"
        "else:\n"
        "    if mode == 'drop-key': data['datasets'][0].pop('primary_key')\n"
        "    if mode == 'wrong-source': data['datasets'][0]['source'] = 'other.public.orders'\n"
        "    if mode == 'wrong-expression': data['metrics'][0]['expression']['dialects'][0]['expression'] = '0'\n"
        "pathlib.Path(out).write_text(json.dumps(data))\n"
    )
    prefix = [sys.executable, str(helper)]
    target = replace(
        manifest.converters["databricks"],
        name="fake",
        cwd=tmp_path,
        export_command=prefix + ["export", mode, str(payload), "{input}", "{output}"],
        import_command=prefix + ["import", mode, str(payload), "{input}", "{output}"],
    )
    runner = Runner(ROOT, Manifest({"fake": target}, {}), tmp_path, prepare=False)
    runner.report["preparations"]["fake"] = {
        "grade": "SKIP" if mode == "blocked" else "OK",
        "reason": "not available",
        "commands": [],
        "diagnostics": [],
    }
    result = runner.document_regression(name, contract, "fake")
    assert result["grade"] == expected, result
    if mode in ("blocked", "empty", "exit"):
        assert len(result["commands"]) == (0 if mode == "blocked" else 1)
    if mode == "wrong-native":
        assert result["assertions"]["grade"] == "OK"  # Round trip alone would miss it.
        assert result["native_assertions"]["grade"] == "FAIL"
    runner.report["document_regressions"] = [result]
    write_report(runner.report, tmp_path)
    saved = json.loads((tmp_path / "results.json").read_text())["document_regressions"][
        0
    ]
    assert saved["grade"] == expected
    assert (tmp_path / saved["input"]).read_text() == contract.input.read_text()
    assert (
        tmp_path / saved["expectations"]
    ).read_text() == contract.expectations.read_text()
    assert (tmp_path / "document-regressions" / result["id"] / "result.json").exists()
    assert exit_code(runner.report) == 0
    assert exit_code(runner.report, strict=True) == (0 if expected == "OK" else 1)


def test_converter_subset_filters_document_targets(tmp_path, monkeypatch):
    from interop.__main__ import main

    def inspect(self):
        assert self.manifest.document_regressions
        assert all(
            contract.targets == ["cube", "dbt"]
            for contract in self.manifest.document_regressions.values()
        )
        return self.report

    monkeypatch.setattr(Runner, "run", inspect)
    assert (
        main(["--converters", "cube,dbt", "--output", str(tmp_path / "results")]) == 0
    )


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("OSSIE_INTEROP_INTEGRATION") != "1",
    reason="set OSSIE_INTEROP_INTEGRATION=1 with prepared converters and Java 21+",
)
@pytest.mark.parametrize(
    "name, target",
    [
        ("dialect-databricks-only", "cube"),
        ("dialect-databricks-only", "databricks"),
        ("dialect-databricks-only", "dbt"),
        ("dialect-snowflake-only", "cube"),
        ("dialect-ossie-sql-2026-only", "databricks"),
        ("keys-single", "dbt"),
        ("keys-composite", "cube"),
        ("keys-unique-only", "cube"),
        ("keys-joined", "dbt"),
        ("source-two-part", "dbt"),
        ("source-three-part", "databricks"),
        ("source-quoted-parts", "cube"),
    ],
)
def test_real_converter_preserves_declared_document_contracts(tmp_path, name, target):
    manifest = load_manifest(ROOT / "interop/manifest.yaml", ROOT)
    runner = Runner(
        ROOT,
        Manifest({target: manifest.converters[target]}, {}),
        tmp_path,
        prepare=False,
    )
    runner.report["preparations"][target] = runner.prepare(manifest.converters[target])
    result = runner.document_regression(
        name, manifest.document_regressions[name], target
    )
    assert result["grade"] == "OK", json.dumps(result, indent=2)
    assert result["input_assertions"]["grade"] == "OK"
    assert result["assertions"]["grade"] == "OK"
