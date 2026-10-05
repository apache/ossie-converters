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

"""Tests for the Ataccama -> Ossie conversion (offline, using a recorded fixture).

Fixture items: BANK_TRANSACTIONS (a warehouse table with terms, DATE columns, and
rich DQ results) and aggregation (a derived output with no locations and a 0% DQ case).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ossie import (
    OssieAIContextObject,
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieDialectExpression,
    OssieDocument,
    OssieField,
)

from ataccama_ossie.ataccama_to_ossie import (
    _dataset_dq,
    _dataset_dq_warning,
    _quality_summary,
    ataccama_to_ossie,
    attribute_to_field,
    build_source,
    flatten_richtext,
)
from ataccama_ossie.models import CatalogAttribute, CatalogItem, CatalogItemBundle, CatalogLocation

FIXTURE = Path(__file__).parent / "fixtures" / "ataccama_bundles.json"
SCHEMA = Path(__file__).parents[2] / "ossie" / "core-spec" / "ossie-schema.json"


@pytest.fixture
def bundles() -> list[CatalogItemBundle]:
    raw = json.loads(FIXTURE.read_text())
    return [CatalogItemBundle.from_dict(b) for b in raw["bundles"]]


@pytest.fixture
def document(bundles: list[CatalogItemBundle]) -> OssieDocument:
    return ataccama_to_ossie(bundles, model_name="test_model", tenant="example-tenant")


def _dataset(document: OssieDocument, name: str) -> OssieDataset:
    return next(d for d in document.datasets if d.name == name)


def _field(dataset: OssieDataset, name: str) -> OssieField:
    return next(f for f in dataset.fields or [] if f.name == name)


def _ext(obj: OssieDataset | OssieField | OssieDocument) -> dict[str, Any]:
    return json.loads((obj.custom_extensions or [])[0].data)


def _schema_errors(document: OssieDocument) -> list[str]:
    schema = json.loads(SCHEMA.read_text())
    payload = json.loads(document.to_ossie_json())
    return [e.message for e in Draft202012Validator(schema).iter_errors(payload)]


def _instructions(obj: OssieDataset | OssieField) -> str:
    return (obj.ai_context.instructions if obj.ai_context else None) or ""


# --- structure & core mapping ---


def test_document_is_schema_valid(document: OssieDocument) -> None:
    assert not _schema_errors(document)


def test_model_structure(document: OssieDocument) -> None:
    assert document.name == "test_model"
    assert {d.name for d in document.datasets} == {"BANK_TRANSACTIONS", "aggregation"}


def test_attribute_becomes_quoted_ansi_field(document: OssieDocument) -> None:
    ds = _dataset(document, "BANK_TRANSACTIONS")
    assert len(ds.fields) == 13
    field = _field(ds, "TRANSACTION_ID")
    assert field.expression.dialects == [
        OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression='"TRANSACTION_ID"')
    ]
    assert _ext(field)["data_type"] == "STRING"


def test_date_columns_are_time_dimensions(document: OssieDocument) -> None:
    ds = _dataset(document, "BANK_TRANSACTIONS")
    assert _field(ds, "BIRTH_DATE").dimension.is_time is True
    assert _field(ds, "TRANSACTION_DATE").dimension.is_time is True
    assert _field(ds, "ACCOUNT_BALANCE").dimension is None


def test_source_from_locations_and_limitation(document: OssieDocument) -> None:
    # DB-backed item: reversed locations + name form a qualified path
    assert _dataset(document, "BANK_TRANSACTIONS").source == "DEMO_ENV.CLOUD_DEMO.BANK_TRANSACTIONS"
    # derived item with no locations: source degrades to the bare name
    assert _dataset(document, "aggregation").source == "aggregation"


def test_terms_become_ai_context(document: OssieDocument) -> None:
    ds = _dataset(document, "BANK_TRANSACTIONS")
    assert set(ds.ai_context.synonyms) >= {"Personal Data", "Finance"}
    email = _field(ds, "EMAIL")
    assert email.ai_context.synonyms == ("E-mail",)
    assert "email" in email.ai_context.instructions.lower()


def test_ai_context_is_an_object_by_default(document: OssieDocument) -> None:
    assert isinstance(_dataset(document, "BANK_TRANSACTIONS").ai_context, OssieAIContextObject)


def test_flat_ai_context_emits_strings(bundles: list[CatalogItemBundle]) -> None:
    document = ataccama_to_ossie(bundles, model_name="test_model", flat_ai_context=True)
    ds = _dataset(document, "BANK_TRANSACTIONS")
    assert isinstance(ds.ai_context, str)
    assert "Synonyms: " in ds.ai_context
    email = _field(ds, "EMAIL")
    assert isinstance(email.ai_context, str)
    assert "email" in email.ai_context.lower()
    assert email.ai_context.endswith("Synonyms: E-mail.")
    # the string branch of the schema's oneOf is just as valid as the object branch
    assert not _schema_errors(document)


def test_flat_ai_context_keeps_absent_context_absent(bundles: list[CatalogItemBundle]) -> None:
    document = ataccama_to_ossie(bundles, flat_ai_context=True)
    untermed = _field(_dataset(document, "BANK_TRANSACTIONS"), "TRANSACTION_ID")
    assert untermed.ai_context is None


def test_governance_metadata_preserved(document: OssieDocument) -> None:
    ext = _ext(_dataset(document, "BANK_TRANSACTIONS"))
    assert ext["catalog_item_urn"].startswith("urn:ata:")
    assert "connection_urn" in ext and "stewardship_group_urn" in ext


# --- data quality enrichment ---


def test_dataset_dq_overall_and_dimensions(document: OssieDocument) -> None:
    dq = _ext(_dataset(document, "BANK_TRANSACTIONS"))["dq"]
    assert dq["passed"] == 1784 and dq["failed"] == 472
    assert dq["pass_rate_pct"] == 79.1
    dims = {d["name"]: d for d in dq["dimensions"]}
    assert dims["Completeness"]["pass_rate_pct"] == 98.8
    assert dims["Validity"]["pass_rate_pct"] == 79.1
    # dimensions with no evaluated records (Uniqueness/Accuracy = 0/0) are dropped
    assert "Uniqueness" not in dims and "Accuracy" not in dims
    assert dq["results_link"].startswith("https://")
    # active-finding count is always reported (0 = no open issues)
    assert dq["active_findings"] == 0


def test_dataset_dq_threshold(document: OssieDocument) -> None:
    # BANK_TRANSACTIONS has a configured overall bar of 75%; 79.1% is above it.
    dq = _ext(_dataset(document, "BANK_TRANSACTIONS"))["dq"]
    assert dq["threshold_pct"] == 75
    assert dq["below_threshold"] is False


def test_no_threshold_when_monitor_has_none(document: OssieDocument) -> None:
    # aggregation's monitor has no overall threshold configured.
    dq = _ext(_dataset(document, "aggregation"))["dq"]
    assert "threshold_pct" not in dq and "below_threshold" not in dq


def test_field_level_dq(document: OssieDocument) -> None:
    ds = _dataset(document, "BANK_TRANSACTIONS")
    dq = _ext(_field(ds, "TRANSACTION_ID"))["dq"]
    assert dq == {"passed": 2251, "failed": 5, "pass_rate_pct": 99.8}


def test_zero_pass_rate_case(document: OssieDocument) -> None:
    dq = _ext(_dataset(document, "aggregation"))["dq"]
    assert dq["passed"] == 0 and dq["failed"] == 4
    assert dq["pass_rate_pct"] == 0.0


def test_no_dq_keys_when_dq_absent(bundles: list[CatalogItemBundle]) -> None:
    b = bundles[0]
    b.dq_results = None  # simulate --no-dq / item without a monitor
    ds = ataccama_to_ossie([b]).datasets[0]
    assert "dq" not in _ext(ds)
    assert all("dq" not in _ext(f) for f in ds.fields)


# --- opt-in DQ AI warnings ---


def test_ai_warnings_off_by_default(document: OssieDocument) -> None:
    # Default conversion must not inject warnings into ai_context.
    ds = _dataset(document, "BANK_TRANSACTIONS")
    assert "Data-quality warning" not in _instructions(ds)
    assert "Data-quality warning" not in _instructions(_field(ds, "EMAIL"))


def test_ai_warning_appended_when_below_ataccama_threshold() -> None:
    # A dataset Ataccama flags as below its own configured threshold gets a warning.
    bundle = CatalogItemBundle(
        item=CatalogItem(urn="urn:ata:t:catalog:catalog-item:1", name="risky"),
        dq_results={"overallQuality": {"passedCount": 50, "failedCount": 50}, "overallDqFindings": []},
        dq_threshold_pct=75,
    )
    ds = ataccama_to_ossie([bundle], dq_ai_warnings=True).datasets[0]
    instr = _instructions(ds)
    assert "Data-quality warning" in instr and "configured 75% quality threshold" in instr


def test_no_ai_warning_without_ataccama_signal(bundles: list[CatalogItemBundle]) -> None:
    # BANK_TRANSACTIONS is above its 75% bar with no active findings; aggregation has no
    # threshold configured. Neither should be flagged — Ataccama is the source of truth.
    doc = ataccama_to_ossie(bundles, dq_ai_warnings=True)
    assert "Data-quality warning" not in _instructions(_dataset(doc, "BANK_TRANSACTIONS"))
    assert "Data-quality warning" not in _instructions(_dataset(doc, "aggregation"))


def test_dataset_dq_warning_triggers() -> None:
    # below Ataccama's own configured threshold
    below = _dataset_dq_warning({"pass_rate_pct": 60.0, "threshold_pct": 75, "below_threshold": True})
    assert below and "configured 75% quality threshold" in below
    # passing, no active findings -> no warning (no converter-side fallback exists)
    assert _dataset_dq_warning({"pass_rate_pct": 99.0, "active_findings": 0}) is None
    # a dataset with no configured threshold is never flagged on pass rate alone
    assert _dataset_dq_warning({"pass_rate_pct": 10.0}) is None
    # active findings alone
    af = _dataset_dq_warning({"pass_rate_pct": 99.0, "active_findings": 2})
    assert af and "2 active data-quality finding(s)" in af


# --- keys & relationships ---


def _keyed_bundle(name, primary_key=None, unique_indexes=None, foreign_keys=None):
    return CatalogItemBundle(
        item=CatalogItem(urn=f"urn:ata:t:catalog:catalog-item:{name}", name=name),
        primary_key=primary_key,
        unique_indexes=unique_indexes or [],
        foreign_keys=foreign_keys or [],
    )


def test_primary_key_is_not_restated_as_a_unique_key() -> None:
    b = _keyed_bundle("line_items", primary_key={"name": "pk", "columns": ["order_id", "line_no"]})
    ds = ataccama_to_ossie([b]).datasets[0]
    assert ds.primary_key == ["order_id", "line_no"]  # composite, order preserved
    # a primary key is unique by definition, so unique_keys carries only other keys
    assert ds.unique_keys is None


def test_no_keys_when_item_has_no_primary_key() -> None:
    ds = ataccama_to_ossie([_keyed_bundle("events")]).datasets[0]
    assert ds.primary_key is None and ds.unique_keys is None


def test_unique_indexes_become_unique_keys() -> None:
    b = _keyed_bundle(
        "users",
        primary_key={"name": "pk", "columns": ["id"]},
        unique_indexes=[{"name": "ux_email", "columns": ["email"]}],
    )
    ds = ataccama_to_ossie([b]).datasets[0]
    assert ds.primary_key == ["id"]
    assert ds.unique_keys == [["email"]]


def test_unique_index_backing_the_primary_key_is_dropped() -> None:
    b = _keyed_bundle(
        "users",
        primary_key={"name": "pk", "columns": ["id"]},
        unique_indexes=[{"name": "ux_pk", "columns": ["id"]}],
    )
    assert ataccama_to_ossie([b]).datasets[0].unique_keys is None


def test_reordered_unique_index_is_still_the_primary_key() -> None:
    b = _keyed_bundle(
        "line_items",
        primary_key={"name": "pk", "columns": ["order_id", "line_no"]},
        unique_indexes=[{"name": "ux_pk", "columns": ["line_no", "order_id"]}],
    )
    # uniqueness does not depend on column order, so this index is the primary key
    assert ataccama_to_ossie([b]).datasets[0].unique_keys is None


def test_duplicate_unique_indexes_are_collapsed() -> None:
    b = _keyed_bundle(
        "users",
        unique_indexes=[{"name": "ux_a", "columns": ["email"]}, {"name": "ux_b", "columns": ["email"]}],
    )
    assert ataccama_to_ossie([b]).datasets[0].unique_keys == [["email"]]


def test_unique_index_without_a_primary_key() -> None:
    b = _keyed_bundle("sessions", unique_indexes=[{"name": "ux_token", "columns": ["token"]}])
    ds = ataccama_to_ossie([b]).datasets[0]
    assert ds.primary_key is None
    assert ds.unique_keys == [["token"]]


def test_relationship_emitted_when_target_in_set() -> None:
    orders = _keyed_bundle("orders", primary_key={"name": "pk", "columns": ["id"]})
    line_items = _keyed_bundle(
        "line_items",
        foreign_keys=[
            {"name": "fk_order", "columns": ["order_id"], "referenced_table": "orders", "referenced_columns": ["id"]}
        ],
    )
    doc = ataccama_to_ossie([orders, line_items])
    (rel,) = doc.relationships
    assert (rel.name, rel.from_dataset, rel.to) == ("fk_order", "line_items", "orders")
    assert (rel.from_columns, rel.to_columns) == (["order_id"], ["id"])
    # the alias 'from' is what reaches the document, and the whole model stays schema-valid
    assert json.loads(doc.to_ossie_json())["relationships"][0]["from"] == "line_items"
    assert not _schema_errors(doc)


def test_relationship_skipped_when_target_not_in_set() -> None:
    line_items = _keyed_bundle(
        "line_items",
        foreign_keys=[
            {"name": "fk", "columns": ["order_id"], "referenced_table": "orders", "referenced_columns": ["id"]}
        ],
    )
    doc = ataccama_to_ossie([line_items])  # 'orders' not included
    assert doc.relationships is None


def test_relationship_names_deduplicated() -> None:
    orders = _keyed_bundle("orders")
    child = _keyed_bundle(
        "child",
        foreign_keys=[
            {"name": "fk", "columns": ["a"], "referenced_table": "orders", "referenced_columns": ["id"]},
            {"name": "fk", "columns": ["b"], "referenced_table": "orders", "referenced_columns": ["id"]},
        ],
    )
    rels = ataccama_to_ossie([orders, child]).relationships
    assert [r.name for r in rels] == ["fk", "fk_2"]


def test_fixture_items_have_no_keys(document: OssieDocument) -> None:
    # BANK_TRANSACTIONS / aggregation have no PK/FK, so no key or relationship output.
    for ds in document.datasets:
        assert ds.primary_key is None and ds.unique_keys is None
    assert document.relationships is None


# --- helper unit tests ---


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("plain text", "plain text"),
        ('[{"type":"paragraph","children":[{"text":"hi"}]}]', "hi"),
        ([{"type": "paragraph", "children": [{"text": "a "}, {"text": "b"}]}], "a b"),
    ],
)
def test_flatten_richtext(value, expected) -> None:
    assert flatten_richtext(value) == expected


@pytest.mark.parametrize(
    "oq,expected",
    [
        (None, None),
        ({"passedCount": 3, "failedCount": 1}, {"passed": 3, "failed": 1, "pass_rate_pct": 75.0}),
        ({"passedCount": 0, "failedCount": 0}, {"passed": 0, "failed": 0}),  # not evaluated -> no rate
    ],
)
def test_quality_summary(oq, expected) -> None:
    assert _quality_summary(oq) == expected


def test_below_threshold_flag_true() -> None:
    dq = _dataset_dq({"overallQuality": {"passedCount": 60, "failedCount": 40}}, threshold_pct=75)
    assert dq["pass_rate_pct"] == 60.0
    assert dq["threshold_pct"] == 75
    assert dq["below_threshold"] is True


@pytest.mark.parametrize(
    "data_type,expected",
    [
        ("STRING", OssieDataType.STRING),
        ("INTEGER", OssieDataType.INTEGER),
        ("LONG", OssieDataType.INTEGER),  # Ossie has no wider integer
        ("FLOAT", OssieDataType.FLOAT),
        ("BOOLEAN", OssieDataType.BOOLEAN),
        ("DATE", OssieDataType.DATE),
        ("DATETIME", OssieDataType.DATE_TIME),
        (None, None),
    ],
)
def test_datatype_mapping(data_type, expected) -> None:
    attr = CatalogAttribute(urn="urn:ata:t:catalog:catalog-attribute:1", name="c", data_type=data_type)
    assert attribute_to_field(attr, terms={}, used=set()).datatype == expected


def test_unrecognized_datatype_is_omitted_with_a_warning() -> None:
    # Ossie omits datatype when the type is unknown rather than inventing one.
    attr = CatalogAttribute(urn="urn:ata:t:catalog:catalog-attribute:1", name="payload", data_type="VARIANT")

    with pytest.warns(UserWarning, match=r"'payload' has unrecognized Ataccama dataType 'VARIANT'"):
        field = attribute_to_field(attr, terms={}, used=set())

    assert field.datatype is None
    assert _ext(field)["data_type"] == "VARIANT"  # still preserved verbatim in the extension


def test_is_time_inferred_from_datatype() -> None:
    attr = CatalogAttribute(urn="urn:ata:t:catalog:catalog-attribute:1", name="created_at", data_type="DATETIME")
    assert attribute_to_field(attr, terms={}, used=set()).is_time_dimension() is True


def test_duplicate_dataset_names_are_disambiguated() -> None:
    items = [
        CatalogItemBundle(item=CatalogItem(urn=f"urn:ata:t:catalog:catalog-item:{i}", name="Orders")) for i in range(2)
    ]
    names = [d.name for d in ataccama_to_ossie(items).datasets]
    assert names == ["Orders", "Orders_2"]


def test_build_source_uses_reversed_locations_then_name() -> None:
    item = CatalogItem(
        urn="urn:ata:t:catalog:catalog-item:1",
        name="customers",
        locations=[CatalogLocation(name="sales"), CatalogLocation(name="Workspaces")],
    )
    assert build_source(item) == "Workspaces.sales.customers"


def test_build_source_quotes_segments_containing_dots() -> None:
    item = CatalogItem(
        urn="urn:ata:t:catalog:catalog-item:1",
        name="orders.csv",
        locations=[CatalogLocation(name="sales.2024"), CatalogLocation(name="s3")],
    )
    assert build_source(item) == 's3."sales.2024"."orders.csv"'


def test_build_source_escapes_embedded_quotes() -> None:
    item = CatalogItem(
        urn="urn:ata:t:catalog:catalog-item:1",
        name='odd"name',
        locations=[CatalogLocation(name="sales")],
    )
    assert build_source(item) == 'sales."odd""name"'


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("customers", "customers"),  # a bare identifier stays bare, so it keeps case-folding
        ("CONTENT_ENV", "CONTENT_ENV"),
        ("_private", "_private"),
        ("dollar$v", '"dollar$v"'),  # legal unquoted in Snowflake, but not in Databricks
        ("ORDER", '"ORDER"'),  # a reserved word needs quoting despite its characters
        ("select", '"select"'),
        ("TX_2022-07", '"TX_2022-07"'),  # a dash would otherwise read as subtraction
        ("2022_SALES", '"2022_SALES"'),  # an identifier may not start with a digit
        ("US SEC_FAILS TO DELIVER", '"US SEC_FAILS TO DELIVER"'),
        ("orders.csv", '"orders.csv"'),  # a dot would otherwise add a level
        ("new\nline", '"new\nline"'),
        ("semi;colon", '"semi;colon"'),
        ("Ünïcode", '"Ünïcode"'),
    ],
)
def test_source_segments_are_quoted_unless_bare_identifiers(segment: str, expected: str) -> None:
    item = CatalogItem(urn="urn:ata:t:catalog:catalog-item:1", name=segment)
    assert build_source(item) == expected


@pytest.mark.parametrize(
    "passed,failed,threshold,expected",
    [
        (7496, 2504, 75, True),  # 74.96% rounds up to 75.0%, but fails.
        (7504, 2496, 75.02, False),  # 75.04% rounds down to 75.0%, but passes.
        (75, 25, 75, False),  # Exactly at the threshold passes.
        (0, 100, 0, False),
        (0, 100, 1, True),
        (100, 0, 100, False),
    ],
)
def test_threshold_uses_unrounded_rate(passed, failed, threshold, expected) -> None:
    dq = _dataset_dq({"overallQuality": {"passedCount": passed, "failedCount": failed}}, threshold)
    assert dq["pass_rate_pct"] == round(passed / (passed + failed) * 100, 1)
    assert dq["below_threshold"] is expected
    assert bool(_dataset_dq_warning(dq)) is expected


@pytest.mark.parametrize("overall", [None, {}, {"passedCount": 0, "failedCount": 0}])
def test_threshold_without_evaluated_checks(overall) -> None:
    dq = _dataset_dq({"overallQuality": overall}, threshold_pct=75)
    assert dq["threshold_pct"] == 75
    assert "pass_rate_pct" not in dq
    assert "below_threshold" not in dq
    assert _dataset_dq_warning(dq) is None
