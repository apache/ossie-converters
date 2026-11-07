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

import json

import pytest
import yaml
from ossie import OssieDataType, OssieDocument

from ossie_sap_csn import CsnToOssieConverter, OssieToCsnConverter

from .helpers import TPCDS_PATH, by_name, load_fixture, load_tpcds, validate_against_schema


def load_tpcds_text() -> str:
    return TPCDS_PATH.read_text()


def strip_additions(csn: dict) -> dict:
    """Drop what export always adds: its creator stamp and the meta.ossie envelope."""
    csn = json.loads(json.dumps(csn))
    csn["meta"].pop("ossie", None)
    if csn["meta"] == {"creator": "apache-ossie-sap-csn"}:
        del csn["meta"]
    return csn


def test_csn_survives_csn_ossie_csn():
    original = load_fixture("bookshop.csn.json")
    ossie = CsnToOssieConverter().convert(original, name="bookshop").output
    back = OssieToCsnConverter().convert(ossie).output
    got, want = strip_additions(back), json.loads(json.dumps(original))
    # A managed association without `keys` comes back with the keys CDS would infer.
    want["definitions"]["sap.capire.bookshop.Orders"]["elements"]["customers"]["keys"] = [{"ref": ["ID"]}]
    assert got == want


def test_csn_survives_through_yaml():
    original = load_fixture("bookshop.csn.json")
    ossie = CsnToOssieConverter().convert(original, name="bookshop").output
    reloaded = OssieDocument.model_validate(yaml.safe_load(ossie.to_ossie_yaml()))
    assert reloaded == ossie
    assert OssieToCsnConverter().convert(reloaded).output == OssieToCsnConverter().convert(ossie).output


@pytest.mark.parametrize("name", ["bookshop.csn.json", "cds_types.csn.json"])
def test_second_csn_to_ossie_pass_is_stable(name):
    original = load_fixture(name)
    first = CsnToOssieConverter().convert(original, name="bookshop").output
    csn = OssieToCsnConverter().convert(first).output
    second = CsnToOssieConverter().convert(csn, name="bookshop").output
    assert second == first


def test_tpcds_survives_ossie_csn_ossie():
    original = OssieDocument.model_validate(load_tpcds())
    csn = OssieToCsnConverter().convert(original).output
    back = CsnToOssieConverter().convert(csn).output
    validate_against_schema(back)

    assert back.name == original.name
    assert back.description == original.description
    assert back.ai_context == original.ai_context
    assert back.custom_extensions is not None and original.custom_extensions is not None
    assert [e for e in back.custom_extensions if e.vendor_name != "SAP_CSN"] == original.custom_extensions

    assert set(by_name(back.datasets)) == set(by_name(original.datasets))
    for name, want in by_name(original.datasets).items():
        got = by_name(back.datasets)[name]
        assert got.primary_key == want.primary_key
        assert got.unique_keys == want.unique_keys
        assert got.description == want.description
        assert got.ai_context == want.ai_context
        assert [(f.name, f.description, f.ai_context) for f in got.fields] == [
            (f.name, f.description, f.ai_context) for f in want.fields
        ]
        # CSN requires a type, so a field declared without one comes back as a String
        # (the export reports ASSUMED_DATATYPE); every declared datatype is kept.
        for got_field, want_field in zip(got.fields, want.fields):
            assert got_field.datatype == (want_field.datatype or OssieDataType.STRING)

    for name, want in by_name(original.relationships).items():
        got = by_name(back.relationships)[name]
        assert (got.from_dataset, got.to, got.from_columns, got.to_columns) == (
            want.from_dataset, want.to, want.from_columns, want.to_columns
        )


def sql(metric):
    return metric.expression.dialects[0].expression.replace(" ", "")


def test_tpcds_metrics_all_survive_whatever_their_home():
    original = OssieDocument.model_validate(load_tpcds())
    back = CsnToOssieConverter().convert(OssieToCsnConverter().convert(original).output).output
    got, want = by_name(back.metrics), by_name(original.metrics)
    assert set(got) == set(want)
    for name in want:
        assert sql(got[name]) == sql(want[name]), name
        assert got[name].description == want[name].description


def test_tpcds_dataset_metrics_keep_their_dataset_scope():
    original = OssieDocument.model_validate(load_tpcds())
    back = CsnToOssieConverter().convert(OssieToCsnConverter().convert(original).output).output
    for name, want in by_name(original.datasets).items():
        got = by_name(by_name(back.datasets)[name].metrics)
        assert {m: sql(got[m]) for m in got} == {m.name: sql(m) for m in want.metrics or []}, name
    assert "total_quantity" in by_name(by_name(original.datasets)["store_sales"].metrics)  # the example has one


@pytest.mark.parametrize("name", ["bookshop.csn.json", "cds_types.csn.json"])
def test_converted_fixture_validates_against_the_schema(name):
    validate_against_schema(CsnToOssieConverter().convert(load_fixture(name), name="m").output)


# --------------------------------------------------------------------------------------
# CDS types: what an element and a cast say, kept exactly
# --------------------------------------------------------------------------------------


def test_cds_types_survive_csn_ossie_csn():
    original = load_fixture("cds_types.csn.json")
    ossie = CsnToOssieConverter().convert(original, name="types").output
    assert strip_additions(OssieToCsnConverter().convert(ossie).output) == original


def readings(document):
    return by_name(by_name(document.datasets)["Readings"].fields)


def test_cast_sql_says_what_the_cds_type_means():
    fields = readings(CsnToOssieConverter().convert(load_fixture("cds_types.csn.json")).output)
    assert fields["takenAtSeconds"].expression.dialects[0].expression == "CAST(loggedAt AS TIMESTAMP(0))"
    assert fields["loggedAtFraction"].expression.dialects[0].expression == "CAST(takenAt AS TIMESTAMP)"
    assert fields["noteText"].expression.dialects[0].expression == "CAST(code AS CLOB)"
    assert fields["codeShort"].expression.dialects[0].expression == "CAST(note AS VARCHAR(3))"
    assert fields["amountRounded"].expression.dialects[0].expression == "CAST(amount AS DECIMAL(15, 0))"
    assert fields["codeCopy"].datatype == OssieDataType.STRING  # `type of Readings:code`


def drop_kept_cxn(document: OssieDocument) -> OssieDocument:
    """``document`` without the CXN the importer keeps, so export has only the SQL to go on."""
    raw = json.loads(document.to_ossie_json())
    for dataset in raw["datasets"]:
        for field in dataset.get("fields") or []:
            for extension in field.get("custom_extensions") or []:
                if extension["vendor_name"] == "SAP_CSN":
                    data = json.loads(extension["data"])
                    data.pop("value", None)
                    data.pop("value_sql", None)
                    extension["data"] = json.dumps(data)
    return OssieDocument.model_validate(raw)


def test_cast_types_survive_on_the_sql_alone_except_the_int32_synonym():
    original = load_fixture("cds_types.csn.json")
    ossie = drop_kept_cxn(CsnToOssieConverter().convert(original).output)
    elements = OssieToCsnConverter().convert(ossie).output["definitions"]["sap.types.Readings"]["elements"]
    for name in ("takenAtSeconds", "loggedAtFraction", "noteText", "codeShort", "amountRounded"):
        assert elements[name]["value"] == original["definitions"]["sap.types.Readings"]["elements"][name]["value"], name
    # CDS defines cds.Int32 as cds.Integer; only the kept CXN tells them apart.
    assert elements["counter32"]["value"] == {"ref": ["counter"], "cast": {"type": "cds.Integer"}}


def test_an_edited_expression_is_translated_not_replaced_by_the_kept_cxn():
    ossie = CsnToOssieConverter().convert(load_fixture("cds_types.csn.json")).output
    raw = json.loads(ossie.to_ossie_json())
    readings_raw = next(d for d in raw["datasets"] if d["name"] == "Readings")
    field = next(f for f in readings_raw["fields"] if f["name"] == "counter32")
    field["expression"]["dialects"][0]["expression"] = "CAST(counter AS INT) + 1"
    csn = OssieToCsnConverter().convert(OssieDocument.model_validate(raw)).output
    assert csn["definitions"]["sap.types.Readings"]["elements"]["counter32"]["value"] == {
        "xpr": [{"ref": ["counter"], "cast": {"type": "cds.Integer"}}, "+", {"val": 1}]
    }


def test_dataset_metrics_csn_cannot_hold_as_measures_survive_the_round_trip():
    raw = yaml.safe_load(load_tpcds_text())
    store_sales = next(d for d in raw["datasets"] if d["name"] == "store_sales")
    store_sales["metrics"].append(
        {"name": "row_count", "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "COUNT(*)"}]}}
    )
    original = OssieDocument.model_validate(raw)
    result = OssieToCsnConverter().convert(original)
    assert [m["name"] for m in result.output["definitions"]["store_sales"]["@Ossie.metrics"]] == ["row_count"]
    back = CsnToOssieConverter().convert(result.output).output
    assert {m.name: sql(m) for m in by_name(back.datasets)["store_sales"].metrics} == {
        "total_quantity": "SUM(ss_quantity)",
        "row_count": "COUNT(*)",
    }
    validate_against_schema(back)
