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

"""Defensive and error paths for both converters.

These are the branches that only run on malformed, unusual or hostile input. They are
covered deliberately: a converter whose error handling is never exercised tends to fail
in the least helpful way at the least helpful moment.
"""

import copy
import json
import warnings

import pytest
import yaml

from ossie_microsoft import convert_ossie_to_semantic_model
from ossie_microsoft._common import (
    DIALECT_ANSI,
    OSSIE_VERSION,
    VENDOR,
    ConversionError,
    make_expression,
    read_stash,
    warn,
    write_stash,
)
from ossie_microsoft.semantic_model_to_ossie import build_ossie_document


def _model(**overrides):
    semantic_model = {
        "name": "m",
        "datasets": [
            {
                "name": "T",
                "source": "t",
                "fields": [
                    {
                        "name": "C",
                        "datatype": "String",
                        "expression": make_expression("c", DIALECT_ANSI),
                    }
                ],
            }
        ],
    }
    semantic_model.update(overrides)
    return {"version": OSSIE_VERSION, **semantic_model}


def _convert(document):
    return convert_ossie_to_semantic_model(document)


def test_warning_rendering_does_not_reference_an_internal_warn_call(monkeypatch):
    shown = []
    monkeypatch.setattr(warnings, "showwarning", lambda *args, **kwargs: shown.append(args))

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warn("model", "something changed")

    assert shown[0][2:4] == ("ossie_microsoft", 1)


# ---------------------------------------------------------------------------
# The stash protocol
# ---------------------------------------------------------------------------


def test_a_stash_merges_into_an_existing_power_bi_entry():
    """Two writes must not leave two competing POWER_BI entries."""
    obj = {}
    write_stash(obj, {"a": 1})
    write_stash(obj, {"b": 2})
    assert len(obj["nonportable_extensions"]) == 1
    assert read_stash(obj) == {"b": 2}


def test_an_empty_stash_is_not_written():
    """A model with no Power BI specifics converts to clean, vendor-neutral Ossie."""
    obj = {}
    write_stash(obj, {})
    assert obj == {}


@pytest.mark.parametrize("data", ['{"lineageTag": "abc"}', "{not json", [1, 2], None, 0, False])
def test_a_stash_that_is_not_an_object_is_refused(data):
    obj = {"nonportable_extensions": [{"vendor_name": VENDOR, "data": data}]}
    with pytest.raises(ConversionError, match="nonportable_extensions data must be an object"):
        read_stash(obj)


def test_a_stash_without_data_is_refused():
    obj = {"nonportable_extensions": [{"vendor_name": VENDOR}]}
    with pytest.raises(ConversionError, match="nonportable_extensions data must be an object"):
        read_stash(obj)


def test_legacy_extensions_are_refused_with_migration_guidance():
    obj = {"custom_extensions": [{"vendor_name": VENDOR, "data": '{"lineageTag": "abc"}'}]}
    with pytest.raises(
        ConversionError, match="use 'nonportable_extensions' with object-valued data"
    ):
        _convert(_model(**obj))


def test_stash_payloads_are_detached_from_their_inputs():
    data = {"annotations": [{"name": "tag", "value": "original"}]}
    obj = {}
    write_stash(obj, data)
    before = copy.deepcopy(obj)
    data["annotations"][0]["value"] = "edited source"
    assert obj == before

    stash = read_stash(obj)
    stash["annotations"][0]["value"] = "edited result"
    assert obj == before
    assert read_stash(obj)["annotations"][0]["value"] == "original"


def test_a_malformed_extension_entry_is_ignored():
    assert read_stash({"nonportable_extensions": ["not a dict", {"vendor_name": "OTHER"}]}) == {}


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_a_non_dict_table_is_skipped():
    bim = {"name": "m", "model": {"tables": ["nonsense", {"name": None}]}}
    with pytest.raises(ValueError, match="no tables that can be exported"):
        build_ossie_document(bim)


def test_a_non_dict_column_is_skipped():
    bim = {
        "name": "m",
        "model": {"tables": [{"name": "T", "columns": ["nonsense", {"noName": 1}]}]},
    }
    model = build_ossie_document(bim)
    assert model["datasets"][0].get("fields") is None


def test_a_non_dict_measure_is_skipped():
    bim = {
        "name": "m",
        "model": {"tables": [{"name": "T", "measures": ["nonsense", {"noName": 1}]}]},
    }
    model = build_ossie_document(bim)
    assert model.get("metrics") is None


def test_a_non_dict_relationship_is_skipped():
    bim = {
        "name": "m",
        "model": {"tables": [{"name": "T"}], "relationships": ["nonsense"]},
    }
    assert build_ossie_document(bim).get("relationships") is None


@pytest.mark.parametrize(
    "datasets",
    [None, {}, [], "dataset", ["nonsense"], [{"name": "T"}, "nonsense"], [{}]],
)
def test_datasets_must_be_a_non_empty_list_of_named_objects(datasets):
    with pytest.raises(ValueError, match="non-empty list of named objects"):
        _convert(_model(datasets=datasets))


def test_a_non_dict_field_is_skipped():
    document = _model()
    document["datasets"][0]["fields"] = ["nonsense", {"noName": 1}]
    bim = _convert(document)
    assert bim["model"]["tables"][0]["columns"] == []


def test_a_non_dict_metric_is_skipped():
    bim = _convert(_model(metrics=["nonsense", {"noName": 1}]))
    assert "measures" not in bim["model"]["tables"][0]


# ---------------------------------------------------------------------------
# Constructs Power BI cannot express
# ---------------------------------------------------------------------------


def test_a_composite_unique_key_is_reported():
    """TMSL marks uniqueness per column; a composite constraint has no equivalent."""
    document = _model()
    document["datasets"][0]["unique_keys"] = [["C", "D"]]
    with pytest.warns(UserWarning, match="composite unique constraint"):
        bim = _convert(document)
    assert "isUnique" not in bim["model"]["tables"][0]["columns"][0]


def test_a_malformed_unique_key_is_ignored():
    document = _model()
    document["datasets"][0]["unique_keys"] = ["not a list"]
    _convert(document)


def test_an_unrecognized_datatype_is_reported_and_left_unspecified():
    document = _model()
    document["datasets"][0]["fields"][0]["datatype"] = "Fictional"
    with pytest.warns(UserWarning, match="unrecognized Apache Ossie data type"):
        bim = _convert(document)
    assert "dataType" not in bim["model"]["tables"][0]["columns"][0]


def test_a_relationship_to_a_missing_table_is_reported():
    document = _model(relationships=[
        {"name": "r", "from": "Nope", "from_columns": ["C"], "to": "T", "to_columns": ["C"]}
    ])
    with pytest.warns(UserWarning, match="is not in the model"):
        bim = _convert(document)
    assert "relationships" not in bim["model"]


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_a_duplicate_measure_name_is_qualified_by_its_table():
    """Ossie metrics are model-level, so two tables can collide on one name."""
    measure = {"name": "Total", "expression": "SUM(1)"}
    bim = {
        "name": "m",
        "model": {
            "tables": [
                {"name": "A", "measures": [dict(measure)]},
                {"name": "B", "measures": [dict(measure)]},
            ]
        },
    }
    with pytest.warns(UserWarning, match="duplicate measure name"):
        model = build_ossie_document(bim)
    assert [m["name"] for m in model["metrics"]] == ["Total", "B.Total"]


def test_a_qualified_measure_still_returns_to_its_original_name():
    """The rename must not leak into the Power BI model on the way back."""
    measure = {"name": "Total", "expression": "SUM(1)"}
    bim = {
        "name": "m",
        "model": {
            "tables": [
                {"name": "A", "measures": [dict(measure)]},
                {"name": "B", "measures": [dict(measure)]},
            ]
        },
    }
    with pytest.warns(UserWarning):
        document = build_ossie_document(bim)
        out = convert_ossie_to_semantic_model(document)
    by_name = {t["name"]: t for t in out["model"]["tables"]}
    assert [m["name"] for m in by_name["B"]["measures"]] == ["Total"]


def test_a_relationship_missing_an_endpoint_is_reported_and_preserved():
    bim = {
        "name": "m",
        "model": {
            "tables": [{"name": "T", "columns": [{"name": "C", "dataType": "string"}]}],
            "relationships": [{"name": "broken", "fromTable": "T"}],
        },
    }
    with pytest.warns(UserWarning, match="missing an endpoint"):
        model = build_ossie_document(bim)
    # Preserved, so a round trip back to Power BI does not delete it.
    assert read_stash(model)["excludedRelationships"][0]["name"] == "broken"


def test_a_duplicate_relationship_name_is_reported_and_preserved():
    relationship = {
        "name": "dup",
        "fromTable": "T",
        "fromColumn": "C",
        "toTable": "U",
        "toColumn": "C",
        "fromCardinality": "many",
        "toCardinality": "one",
    }
    bim = {
        "name": "m",
        "model": {
            "tables": [
                {"name": "T", "columns": [{"name": "C", "dataType": "string"}]},
                {"name": "U", "columns": [{"name": "C", "dataType": "string"}]},
            ],
            "relationships": [dict(relationship), dict(relationship)],
        },
    }
    with pytest.warns(UserWarning, match="duplicate relationship"):
        model = build_ossie_document(bim)
    assert len(model["relationships"]) == 1
    assert read_stash(model)["excludedRelationships"][0]["name"] == "dup"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_the_cli_writes_to_stdout_without_an_output_path(tmp_path, capsys):
    from ossie_microsoft.cli import main

    src = tmp_path / "m.bim"
    src.write_text(
        json.dumps({"name": "m", "model": {"tables": [{"name": "T"}]}}),
        encoding="utf-8",
    )
    assert main(["import", "-i", str(src)]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["name"] == "m"


def test_the_cli_rejects_a_model_without_an_exportable_dataset(tmp_path, capsys):
    from ossie_microsoft.cli import main

    src = tmp_path / "m.bim"
    out = tmp_path / "m.yaml"
    src.write_text(json.dumps({"name": "m", "model": {"tables": []}}), encoding="utf-8")

    assert main(["import", "-i", str(src), "-o", str(out)]) == 1
    assert "no tables that can be exported" in capsys.readouterr().err
    assert not out.exists()


def test_the_cli_reports_a_bad_file_without_a_traceback(tmp_path, capsys):
    from ossie_microsoft.cli import main

    assert main(["import", "-i", str(tmp_path / "missing.bim")]) == 1
    assert capsys.readouterr().err.startswith("Error:")


def test_the_cli_reports_invalid_json_without_a_traceback(tmp_path, capsys):
    from ossie_microsoft.cli import main

    src = tmp_path / "m.bim"
    src.write_text("{not json", encoding="utf-8")
    assert main(["import", "-i", str(src)]) == 1
    assert capsys.readouterr().err.startswith("Error:")
