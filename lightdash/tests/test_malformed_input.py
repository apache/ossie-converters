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

"""Input that is not the shape the converter reads is reported, not tripped
over.

Each case is a schema file an author could plausibly write by mistake (a
bare string where a block was meant, a list where a mapping was meant, a
model without a name). The converter must raise ``ConversionError`` naming
the element, and the CLI must turn that into an error message and a
non-zero exit rather than a traceback.
"""

import pytest
import yaml

from ossie_lightdash import ConversionError, LightdashToOssieConverter
from ossie_lightdash.cli import main


def _model(**overrides):
    model = {
        "name": "orders",
        "columns": [{"name": "amount", "meta": {"dimension": {"type": "number"}}}],
        "meta": {},
    }
    model.update(overrides)
    return model


MALFORMED = {
    "document is a list": ([_model()], "schema document"),
    "models is a mapping": ({"models": {"orders": _model()}}, "models"),
    "model is a string": ({"models": ["orders"]}, "model #1"),
    "model has no name": ({"models": [{"columns": []}]}, "model #1 has no name"),
    "model name is empty": ({"models": [_model(name="  ")]}, "model #1 has no name"),
    "meta is a string": ({"models": [_model(meta="hidden")]}, "meta of orders"),
    "config.meta is a list": ({"models": [_model(config={"meta": []})]}, "config.meta of orders"),
    "columns is a mapping": ({"models": [_model(columns={"amount": {}})]}, "columns of model orders"),
    "column has no name": ({"models": [_model(columns=[{"description": "x"}])]}, "column #1 of model orders has no name"),
    "dimension is a string": (
        {"models": [_model(columns=[{"name": "amount", "meta": {"dimension": "number"}}])]},
        "dimension of column amount",
    ),
    "column metrics is a list": (
        {"models": [_model(columns=[{"name": "amount", "meta": {"metrics": ["total"]}}])]},
        "metrics of column amount",
    ),
    "column metric is a string": (
        {"models": [_model(columns=[{"name": "amount", "meta": {"metrics": {"total": "sum"}}}])]},
        "metric total of column amount",
    ),
    "model metrics is a list": ({"models": [_model(meta={"metrics": ["total"]})]}, "metrics of model orders"),
    "model metric is a string": (
        {"models": [_model(meta={"metrics": {"total": "SUM(${TABLE}.amount)"}})]},
        "metric total of model orders",
    ),
    "joins is a string": ({"models": [_model(meta={"joins": "customers"})]}, "joins of model orders"),
    "join is a string": ({"models": [_model(meta={"joins": ["customers"]})]}, "join #1 of model orders"),
    "seeds is a mapping": ({"models": [], "seeds": {"a": 1}}, "seeds"),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_input_raises_conversion_error_naming_the_element(case):
    schema_yml, element = MALFORMED[case]
    with pytest.raises(ConversionError) as excinfo:
        LightdashToOssieConverter().convert(schema_yml, database="db", schema="public")
    assert element in str(excinfo.value)


def test_well_formed_input_with_empty_blocks_still_converts():
    schema_yml = {
        "models": [
            {"name": "orders", "meta": None, "columns": None},
            {"name": "customers", "meta": {"joins": None, "metrics": None}, "columns": []},
        ]
    }
    result = LightdashToOssieConverter().convert(schema_yml, database="db", schema="public")
    assert [dataset.name for dataset in result.output.datasets] == ["orders", "customers"]


def test_cli_import_reports_malformed_input_without_a_traceback(tmp_path, capsys):
    schema = tmp_path / "schema.yml"
    schema.write_text(
        yaml.safe_dump({"models": [_model(meta={"joins": "customers"})]}), encoding="utf-8"
    )
    with pytest.raises(SystemExit) as excinfo:
        main(["import", "-i", str(schema), "-o", str(tmp_path / "out.yaml"), "--schema", "public"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "joins of model orders must be a list" in err
    assert "Traceback" not in err
    assert not (tmp_path / "out.yaml").exists()


def test_cli_import_reports_unreadable_yaml(tmp_path, capsys):
    schema = tmp_path / "schema.yml"
    schema.write_text("models: [\n  - name: orders", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["import", "-i", str(schema), "-o", str(tmp_path / "out.yaml"), "--schema", "public"])
    assert excinfo.value.code == 2
    assert "not valid YAML" in capsys.readouterr().err


_VALID_DOCUMENT = {
    "version": "0.2.0.dev0",
    "name": "m",
    "datasets": [
        {
            "name": "a",
            "source": "s.a",
            "fields": [{"name": "id", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]}}],
        },
        {
            "name": "b",
            "source": "s.b",
            "fields": [{"name": "id", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]}}],
        },
    ],
}


@pytest.mark.parametrize(
    ("extra", "location"),
    [
        # The legacy wrapper around an otherwise valid flat document.
        ({"semantic_model": [{"name": "m", "datasets": []}]}, "semantic_model"),
        # A relationship that pairs no columns.
        (
            {"relationships": [{"name": "r", "from": "a", "to": "b", "from_columns": [], "to_columns": ["id"]}]},
            "relationships.0.from_columns",
        ),
    ],
)
def test_cli_export_reports_an_invalid_ossie_document_by_location(tmp_path, capsys, extra, location):
    path = tmp_path / "doc.yaml"
    path.write_text(yaml.safe_dump({**_VALID_DOCUMENT, **extra}), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["export", "-i", str(path), "-o", str(tmp_path / "project")])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "is not a valid Ossie document" in err
    assert location in err
    assert "Traceback" not in err
