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

"""End-to-end round trips through the same serialization boundary the CLI uses
(OssieDocument -> YAML text -> re-parsed OssieDocument), for every fixture."""

import pytest
import yaml
from ossie import OssieDocument

from ossie_sigma.ossie_to_sigma import OssieToSigmaConverter
from ossie_sigma.sigma_to_ossie import SigmaToOssieConverter

from .helpers import load_fixture, normalize


FIXTURES = ["fixtureA_sigma.json", "fixtureB_sigma.json", "fixtureC_sigma.json"]


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_sigma_osi_sigma_roundtrip_through_yaml_serialization(fixture_name):
    spec = load_fixture(fixture_name)

    document = SigmaToOssieConverter().convert(spec).output
    yaml_text = document.to_ossie_yaml()

    serialized = yaml.safe_load(yaml_text)
    assert next(iter(serialized)) == "version"
    assert serialized["name"] == spec["name"]
    assert "semantic_model" not in serialized
    assert "dialects" not in serialized
    assert "vendors" not in serialized
    reparsed_document = OssieDocument.model_validate(serialized)
    reconstructed_spec = OssieToSigmaConverter().convert(reparsed_document).output

    assert normalize(reconstructed_spec) == normalize(spec)


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_osi_sigma_osi_roundtrip_preserves_portable_fields(fixture_name):
    """Sigma -> Ossie -> Sigma -> Ossie: the second Ossie document's portable
    (non-nonportable_extensions) content must match the first, even though the Sigma
    spec in between round-trips through JSON."""
    spec = load_fixture(fixture_name)

    document_1 = SigmaToOssieConverter().convert(spec).output
    spec_2 = OssieToSigmaConverter().convert(document_1).output
    document_2 = SigmaToOssieConverter().convert(spec_2).output

    def portable(document):
        return {
            "datasets": [
                (d.name, d.source, [(f.name, f.datatype) for f in d.fields or []]) for d in document.datasets
            ],
            "relationships": [
                (r.name, r.from_dataset, r.to, r.from_columns, r.to_columns) for r in document.relationships or []
            ],
            "metrics": [(m.name,) for m in document.metrics or []],
        }

    assert portable(document_1) == portable(document_2)


@pytest.mark.parametrize("expression_form", ["sql", "dialects"])
@pytest.mark.parametrize(
    ("formula", "expected_sql", "expected_back"),
    [
        ("Median([amount])",
         'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "orders"."amount")',
         "PercentileCont([amount], 0.5)"),
        ("PercentileCont([amount], 0.9)",
         'PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY "orders"."amount")',
         "PercentileCont([amount], 0.9)"),
        ("PercentileDisc([amount], 0.9)",
         'PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY "orders"."amount")',
         "PercentileDisc([amount], 0.9)"),
    ],
)
def test_percentile_metrics_round_trip_without_native_sigma_metadata(
    expression_form, formula, expected_sql, expected_back
):
    spec = {
        "kind": "data-model",
        "schemaVersion": 1,
        "name": "percentiles",
        "pages": [{
            "id": "page",
            "name": "Model",
            "elements": [{
                "id": "orders",
                "kind": "table",
                "name": "orders",
                "source": {
                    "kind": "warehouse-table",
                    "connectionId": "fixture-connection",
                    "path": ["db", "public", "orders"],
                },
                "columns": [{"id": "amount", "formula": "[amount]"}],
                "metrics": [{"id": "stat", "name": "stat", "formula": formula}],
            }],
        }],
    }
    imported = SigmaToOssieConverter().convert(spec)
    assert not imported.issues
    assert {
        d.dialect: d.expression for d in imported.output.metrics[0].expression.dialects
    }["ANSI_SQL"] == expected_sql
    data = yaml.safe_load(imported.output.to_ossie_yaml())

    # Remove every native formula and stash: a same-vendor round trip would
    # otherwise reuse the original formula and never exercise SQL translation.
    nodes = [data, *data["datasets"], *data["datasets"][0]["fields"], *data["metrics"]]
    for node in nodes:
        node.pop("nonportable_extensions", None)
        if "expression" in node:
            sql = next(d["expression"] for d in node["expression"]["dialects"] if d["dialect"] == "ANSI_SQL")
            node["expression"] = (
                {"sql": sql} if expression_form == "sql"
                else {"dialects": [{"dialect": "ANSI_SQL", "expression": sql}]}
            )
    document = OssieDocument.from_ossie_yaml(yaml.safe_dump(data))
    exported = OssieToSigmaConverter().convert(document)
    assert not exported.issues
    element = exported.output["pages"][0]["elements"][0]
    assert [metric["formula"] for metric in element["metrics"]] == [expected_back]

    reimported = SigmaToOssieConverter().convert(exported.output)
    assert not reimported.issues
    portable_expressions = {
        d.dialect: d.expression for d in reimported.output.metrics[0].expression.dialects
    }
    assert portable_expressions["ANSI_SQL"] == expected_sql
