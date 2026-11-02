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

"""Coverage of added formats, capability skips, and output isolation."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from interop.adapters import convert
from interop.checks import ContentError, native_content
from interop.manifest import ManifestError, load_manifest
from interop.runner import exit_code
from interop.tests.test_foundation import matrix  # noqa: F401 -- shared subprocess fixture

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name",
    [
        "gooddata",
        "honeydew",
        "microsoft",
        "nvidia",
        "omni",
        "orionbelt",
        "salesforce",
        "sigma",
        "thoughtspot",
        "wisdom",
        "wisdom-semantic-view",
    ],
)
def test_registered_native_sources_have_real_members(name):
    converter = load_manifest(ROOT / "interop/manifest.yaml", ROOT).converters[name]
    for path in converter.fixtures.values():
        assert (
            native_content(path, converter.native_format, converter.output)["members"]
            > 0
        )


@pytest.mark.parametrize(
    "fmt",
    [
        "gooddata",
        "hex",
        "honeydew",
        "microsoft",
        "nvidia",
        "omni",
        "orionbelt",
        "salesforce",
        "sigma",
        "snowflake",
        "thoughtspot",
        "wisdom",
        "semantic-view",
    ],
)
def test_metadata_and_logs_do_not_establish_native_content(tmp_path, fmt):
    directory = fmt in ("hex", "honeydew", "omni", "thoughtspot")
    file = tmp_path / (
        "empty.table.tml"
        if fmt == "thoughtspot"
        else "empty.view.yaml"
        if fmt == "omni"
        else "empty.yaml"
    )
    file.write_text(json.dumps({"name": "empty", "description": "no native members"}))
    (tmp_path / "issues.json").write_text('[{"warning": "not a model"}]')
    content = native_content(
        tmp_path if directory else file, fmt, "directory" if directory else "file"
    )
    assert content == {"files": [], "members": 0}


def test_aml_inspection_counts_source_members_but_not_dataset_or_log(tmp_path):
    (tmp_path / "orders.model.aml").write_text(
        "Model orders {\n  dimension id {\n definition: @sql id;;\n }\n}\n"
    )
    (tmp_path / "model.dataset.aml").write_text("Dataset orders { }\n")
    (tmp_path / "issues.yaml").write_text("- warning: dropped metric")
    content = native_content(tmp_path, "holistics", "directory")
    assert content["members"] == 1
    assert len(content["files"]) == 1


def test_malformed_added_native_model_is_failure(tmp_path):
    file = tmp_path / "native.json"
    file.write_text('{"pages": [null]}')
    with pytest.raises(ContentError):
        native_content(file, "sigma", "file")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["converters"]["hex"].pop("import_skip"),
        lambda d: d["converters"]["cube"].update(import_skip="unsupported"),
        lambda d: d["converters"]["hex"].update(
            fixtures={"bad": "cube/tests/fixtures/tpcds_cube"}
        ),
        lambda d: d["converters"]["polaris"].update(skip=""),
        lambda d: d["converters"]["holistics"].update(roundtrip_skip=False),
        lambda d: d["converters"]["omni"].update(output="file"),
    ],
)
def test_capability_contract_rejects_ambiguous_configuration(tmp_path, mutation):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    mutation(data)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ManifestError):
        load_manifest(path, ROOT)


@pytest.mark.parametrize("capability", ["skip", "import_skip", "roundtrip_skip"])
def test_unsupported_capability_is_skip_without_running_import(matrix, capability):  # noqa: F811
    converter, run, _ = matrix
    supported = converter("alpha")
    target = converter("beta", import_mode="fail")
    target = replace(
        target, **{capability: "explicit unsupported capability"}, fixtures={}
    )
    if capability == "import_skip":
        target = replace(target, import_command=None)
    runner, report = run([supported, target])
    pair = next(
        c for c in report["cases"] if c["tier"] == "B" and c["target"] == "beta"
    )
    assert pair["grade"] == "SKIP"
    assert not pair["commands"]
    assert "explicit unsupported capability" in pair["reason"]
    assert pair["expected_skip"] is True
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == 0
    if capability != "skip":
        accepted = next(
            c for c in report["cases"] if c["tier"] == "A" and c["target"] == "beta"
        )
        assert accepted["grade"] == "OK"
    else:
        assert not report["preparations"]["beta"]["commands"]


def test_salesforce_adapter_preserves_input_and_collects_single_output(
    tmp_path, monkeypatch
):
    original = tmp_path / "source.json"
    original.write_text('{"name":"fixture"}')
    output = tmp_path / "converted.yaml"

    def fake_run(argv, check):
        staged = Path(argv[-1])
        assert staged != original
        assert staged.read_text() == original.read_text()
        (staged.parent / "model.yaml").write_text("name: converted")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("interop.adapters.subprocess.run", fake_run)
    convert("salesforce", "import", original, output)
    assert original.read_text() == '{"name":"fixture"}'
    assert output.read_text() == "name: converted"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "converted.yaml",
        "source.json",
    ]


def test_salesforce_adapter_preserves_failed_exit_code(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}")
    monkeypatch.setattr(
        "interop.adapters.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=4),
    )
    with pytest.raises(SystemExit) as error:
        convert("salesforce", "import", source, tmp_path / "result.yaml")
    assert error.value.code == 4
    assert list(tmp_path.iterdir()) == [source]


def test_salesforce_adapter_rejects_missing_or_multiple_outputs(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}")
    monkeypatch.setattr(
        "interop.adapters.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0),
    )
    with pytest.raises(ValueError, match="expected one Salesforce output"):
        convert("salesforce", "import", source, tmp_path / "result.yaml")


def test_import_file_prerequisite_skips_sources_and_pairs_but_keeps_exports(matrix):  # noqa: F811
    converter, run, path = matrix
    prerequisite = path / "vendor-schema.json"
    target = replace(converter("beta"), import_requires_files=[prerequisite])
    runner, report = run([converter("alpha"), target])
    source = next(s for s in report["sources"] if s["owner"] == "beta")
    assert source["grade"] == "SKIP"
    assert source["expected_skip"] is False
    assert "missing import prerequisite" in source["reason"]
    assert not source["commands"]
    pair = next(
        c for c in report["cases"] if c["tier"] == "B" and c["target"] == "beta"
    )
    assert pair["grade"] == "SKIP" and not pair["commands"]
    assert any(
        c["grade"] == "OK"
        for c in report["cases"]
        if c["tier"] == "A" and c["target"] == "beta"
    )
    assert "vendor-schema.json" in report["capabilities"]["beta"]["import"]
    prerequisite.write_text("{}")
    assert runner.source("beta", "retry", next(iter(target.fixtures.values())))[1]


def test_import_prerequisite_cannot_escape_repository(tmp_path):
    data = yaml.safe_load((ROOT / "interop/manifest.yaml").read_text())
    data["converters"]["salesforce"]["import_requires_files"] = ["../outside.json"]
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ManifestError):
        load_manifest(path, ROOT)


def test_honeydew_calculated_attributes_and_metrics_are_native_members(tmp_path):
    for index, member_type in enumerate(("calculated_attribute", "metric")):
        (tmp_path / f"member{index}.yml").write_text(
            yaml.safe_dump({"type": member_type, "name": "amount", "sql": "amount"})
        )
    assert native_content(tmp_path, "honeydew", "directory")["members"] == 2


@pytest.mark.parametrize("key", ["columns", "formulas", "measures"])
def test_wisdom_counts_computed_fields_and_measures_without_physical_columns(
    tmp_path, key
):
    output = tmp_path / "native.json"
    output.write_text(
        json.dumps({"tables": [{"zsheet_json": {key: [{"name": "amount"}]}}]})
    )
    assert native_content(output, "wisdom", "file")["members"] == 1


def test_sigma_counts_metrics_without_physical_columns(tmp_path):
    output = tmp_path / "native.json"
    output.write_text(
        json.dumps({"pages": [{"elements": [{"metrics": [{"name": "revenue"}]}]}]})
    )
    assert native_content(output, "sigma", "file")["members"] == 1


def test_orionbelt_registration_enables_native_semantic_validation():
    converter = load_manifest(ROOT / "interop/manifest.yaml", ROOT).converters[
        "orionbelt"
    ]
    assert (
        converter.prepare[converter.prepare.index("--extra") + 1] == "obml-validation"
    )
