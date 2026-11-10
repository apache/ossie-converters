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

from pathlib import Path

import pytest
import yaml
from ossie import (
    OssieDataset,
    OssieDialect,
    OssieDialectExpression,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
)

from ossie import OssieRelationship
from pydantic import ValidationError

from ossie_sigma.converter_issues import ConverterIssueType
from ossie_sigma.ossie_to_sigma import OssieToSigmaConverter, _stable_id
from ossie_sigma.sigma_to_ossie import SigmaToOssieConverter

from .helpers import load_fixture, normalize

EXAMPLES_DIR = Path(__file__).parent.parent.parent / "ossie" / "examples"


def test_roundtrip_fixture_a_is_byte_identical():
    spec = load_fixture("fixtureA_sigma.json")
    document = SigmaToOssieConverter().convert(spec).output
    reconstructed = OssieToSigmaConverter().convert(document).output
    assert normalize(reconstructed) == normalize(spec)


def test_roundtrip_fixture_b_is_byte_identical():
    spec = load_fixture("fixtureB_sigma.json")
    document = SigmaToOssieConverter().convert(spec).output
    reconstructed = OssieToSigmaConverter().convert(document).output
    assert normalize(reconstructed) == normalize(spec)


def test_foreign_origin_document_synthesizes_valid_spec():
    """An Ossie document never touched by Sigma (no SIGMA custom_extensions) must
    still convert to a structurally valid Sigma spec, with synthesized ids and
    formulas best-effort translated from ANSI SQL."""
    document = OssieDocument.model_validate(
        yaml.safe_load((EXAMPLES_DIR / "tpcds_semantic_model.yaml").read_text())
    )
    result = OssieToSigmaConverter().convert(document)
    spec = result.output

    assert spec["kind"] == "data-model"
    assert spec["pages"]
    element_names = {e["name"] for p in spec["pages"] for e in p["elements"]}
    assert "store_sales" in element_names

    store_sales = next(e for p in spec["pages"] for e in p["elements"] if e["name"] == "store_sales")
    assert all("id" in c and "formula" in c for c in store_sales["columns"])
    # Plain passthrough columns get no explicit `name` (matches Sigma's own convention).
    plain_column = next(c for c in store_sales["columns"] if c["formula"] == "[ss_sold_date_sk]")
    assert "name" not in plain_column

    # Single-dataset metrics are attached to their owning element ...
    assert any(m["name"] == "total_sales" for m in store_sales.get("metrics", []))
    # ... while genuinely cross-dataset metrics are dropped with a recorded issue,
    # not silently discarded and not incorrectly attached to one dataset.
    issue_types = {i.issue_type for i in result.issues}
    assert ConverterIssueType.CROSS_DATASET_METRIC_DROPPED in issue_types


def test_ids_are_deterministic_across_repeated_conversions():
    document = OssieDocument.model_validate(
        yaml.safe_load((EXAMPLES_DIR / "tpcds_semantic_model.yaml").read_text())
    )
    spec_1 = OssieToSigmaConverter().convert(document).output
    spec_2 = OssieToSigmaConverter().convert(document).output
    assert normalize(spec_1) == normalize(spec_2)


def test_synthesized_ids_are_stable_across_processes():
    """uuid5 over a fixed namespace, not hash()/uuid4 — a re-export that minted new ids
    would silently orphan every Sigma object referencing the old ones, so this pins the
    exact values rather than only asserting two in-process runs agree (PYTHONHASHSEED
    randomization would not show up in a same-process comparison)."""
    assert _stable_id("element", "store_sales") == "83ae98f0bda0511baf98cd58fd394974"
    assert _stable_id("column", "store_sales", "ss_sold_date_sk") == "48b667ffa9535d08bcc1d0a48b878a99"
    assert _stable_id("metric", "store_sales", "total_sales") == "9d4cc3056b0c5c698664c4803f12dd72"


def test_relationship_ids_are_scoped_by_owning_dataset():
    """Two unrelated relationships sharing a name, on different table pairs, must not
    collide onto the same synthesized Sigma relationship id."""
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(name="orders", source="db.public.orders"),
            OssieDataset(name="shipments", source="db.public.shipments"),
            OssieDataset(name="customers", source="db.public.customers"),
            OssieDataset(name="carriers", source="db.public.carriers"),
        ],
        relationships=[
            OssieRelationship(
                name="Parent", **{"from": "orders"}, to="customers", from_columns=["x"], to_columns=["y"]
            ),
            OssieRelationship(
                name="Parent", **{"from": "shipments"}, to="carriers", from_columns=["x"], to_columns=["y"]
            ),
        ],
    )

    spec = OssieToSigmaConverter().convert(document).output
    rel_ids = [
        rel["id"]
        for page in spec["pages"]
        for element in page["elements"]
        for rel in element.get("relationships", [])
    ]
    assert len(rel_ids) == 2
    assert len(set(rel_ids)) == 2, "relationships with the same name on different dataset pairs must not collide"


def test_legacy_semantic_model_wrapper_is_rejected():
    with pytest.raises(ValidationError, match="semantic_model"):
        OssieDocument(semantic_model=[])


def test_relationship_column_arity_mismatch_is_recorded_not_silently_truncated():
    """from_columns/to_columns are independently constrained in the OSI schema (each
    only needs to be non-empty), so a compound-key relationship with unequal lengths
    is legal input. zip() truncates to the shorter array; that must be a recorded
    issue, not a silent drop of the extra key column(s)."""
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(name="orders", source="db.public.orders"),
            OssieDataset(name="regions", source="db.public.regions"),
        ],
        relationships=[
            OssieRelationship(
                name="OrderRegion",
                **{"from": "orders"},
                to="regions",
                from_columns=["region_id", "sub_id"],
                to_columns=["region_id"],
            ),
        ],
    )

    result = OssieToSigmaConverter().convert(document)

    issue_types = {i.issue_type for i in result.issues}
    assert ConverterIssueType.RELATIONSHIP_COLUMN_ARITY_MISMATCH in issue_types

    rel = next(
        r for p in result.output["pages"] for e in p["elements"] for r in e.get("relationships", [])
    )
    # The mismatch is still recorded rather than crashing the conversion, but only
    # one key pair can be formed from a 2-vs-1 mismatch.
    assert len(rel["keys"]) == 1


def test_relationship_arity_mismatch_element_names_are_scoped_by_owning_dataset():
    """Relationship identity is already scoped by (dataset_name, rel.name) (see
    test_relationship_ids_are_scoped_by_owning_dataset); an arity-mismatch issue's
    element_name must be scoped the same way, or two unrelated relationships sharing a
    name on different table pairs become indistinguishable in the issue list."""
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(name="orders", source="db.public.orders"),
            OssieDataset(name="customers", source="db.public.customers"),
            OssieDataset(name="shipments", source="db.public.shipments"),
            OssieDataset(name="carriers", source="db.public.carriers"),
        ],
        relationships=[
            OssieRelationship(
                name="Parent",
                **{"from": "orders"},
                to="customers",
                from_columns=["region_id", "sub_id"],
                to_columns=["region_id"],
            ),
            OssieRelationship(
                name="Parent",
                **{"from": "shipments"},
                to="carriers",
                from_columns=["region_id", "sub_id"],
                to_columns=["region_id"],
            ),
        ],
    )

    result = OssieToSigmaConverter().convert(document)

    arity_issues = [
        i for i in result.issues if i.issue_type == ConverterIssueType.RELATIONSHIP_COLUMN_ARITY_MISMATCH
    ]
    assert len(arity_issues) == 2
    element_names = {i.element_name for i in arity_issues}
    assert len(element_names) == 2, "arity-mismatch issues for same-named relationships must not collide"


def test_model_level_metadata_round_trips_through_ossie_and_back():
    spec = load_fixture("fixtureA_sigma.json")
    spec.update(
        createdAt="2024-01-01T00:00:00Z",
        createdBy="user-1",
        updatedAt="2024-02-01T00:00:00Z",
        updatedBy="user-2",
        ownerId="user-1",
        url="https://app.sigmacomputing.com/data-model/11111111",
    )
    document = SigmaToOssieConverter().convert(spec).output
    reconstructed = OssieToSigmaConverter().convert(document).output
    for key in ("createdAt", "createdBy", "updatedAt", "updatedBy", "ownerId", "url"):
        assert reconstructed[key] == spec[key]


@pytest.mark.parametrize(
    ("dialect", "amount", "metric_sql"),
    [
        (OssieDialect.OSSIE_SQL_2026, "amount", "SUM(orders.amount)"),
        (OssieDialect.DATABRICKS, "`amount`", "SUM(`orders`.`amount`)"),
        (OssieDialect.SNOWFLAKE, '"amount"', 'SUM("orders"."amount")'),
        (OssieDialect.BIGQUERY, "`amount`", "SUM(`orders`.`amount`)"),
        (OssieDialect.SNOWFLAKE, '"amount"', 'SUM("customers"."public"."orders"."amount")'),
        (OssieDialect.BIGQUERY, "`amount`", "SUM(`customers.public.orders`.amount)"),
    ],
)
def test_sql_only_dialect_exports_fields_and_places_metrics(dialect, amount, metric_sql):
    def expression(sql):
        return OssieExpression(dialects=[OssieDialectExpression(dialect=dialect, expression=sql)])

    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(
                name="customers",
                source="db.public.customers",
                fields=[OssieField(name="customer_id", expression=expression("customer_id"))],
            ),
            OssieDataset(
                name="orders",
                source="db.public.orders",
                fields=[
                    OssieField(name="amount", expression=expression(amount)),
                    OssieField(name="discount", expression=expression(f"{amount} * 0.9")),
                ],
            )
        ],
        metrics=[OssieMetric(name="total", expression=expression(metric_sql))],
    )
    result = OssieToSigmaConverter().convert(document)
    customers, element = result.output["pages"][0]["elements"]
    assert not customers.get("metrics")
    assert [column["formula"] for column in element["columns"]] == ["[amount]", "([amount] * 0.9)"]
    assert [metric["formula"] for metric in element["metrics"]] == ["Sum([amount])"]
    assert not result.issues


def test_untranslatable_expression_omits_the_column_instead_of_faking_a_formula():
    """`formula` is required on every Sigma column and the data model API validates the
    whole document before applying any of it, so a placeholder would fail the entire
    upload rather than degrade one column."""
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(
                name="orders",
                source="db.public.orders",
                fields=[
                    OssieField(
                        name="ok",
                        expression=OssieExpression(
                            dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression="amount")]
                        ),
                    ),
                    OssieField(
                        name="untranslatable",
                        expression=OssieExpression(
                            dialects=[
                                OssieDialectExpression(
                                    dialect=OssieDialect.ANSI_SQL,
                                    expression="SUM(amount) OVER (PARTITION BY region)",
                                )
                            ]
                        ),
                    ),
                    OssieField(
                        name="no_usable_dialect",
                        expression=OssieExpression(
                            dialects=[OssieDialectExpression(dialect=OssieDialect.MDX, expression="[Measures].[X]")]
                        ),
                    ),
                ],
            )
        ],
        metrics=[
            OssieMetric(
                name="untranslatable_metric",
                expression=OssieExpression(
                    dialects=[
                        OssieDialectExpression(
                            dialect=OssieDialect.ANSI_SQL,
                            expression="SUM(orders.amount) OVER (PARTITION BY orders.region)",
                        )
                    ]
                ),
            )
        ],
    )

    result = OssieToSigmaConverter().convert(document)
    element = result.output["pages"][0]["elements"][0]

    assert [c["name"] for c in element["columns"]] == ["ok"]
    assert not element.get("metrics")
    assert all(c.get("formula") for c in element["columns"])
    assert (
        sum(1 for i in result.issues if i.issue_type is ConverterIssueType.EXPRESSION_NOT_TRANSLATABLE) == 3
    )


@pytest.mark.parametrize(
    ("sql", "expected_formula"),
    [
        (
            "CASE WHEN SUM(orders.hours) > 0 THEN SUM(orders.amount) ELSE NULL END",
            "If((Sum([hours]) > 0), Sum([amount]), Null())",
        ),
        (
            "CASE WHEN SUM(orders.hours) > 0 THEN SUM(orders.amount) END",
            "If((Sum([hours]) > 0), Sum([amount]))",
        ),
        (
            "SUM(orders.amount) / NULLIF(SUM(orders.hours), 0)",
            "(Sum([amount]) / NullIf(Sum([hours]), 0))",
        ),
    ],
)
def test_null_returning_ansi_metrics_are_kept(sql, expected_formula):
    """A metric that returns NULL for some inputs (a gate or a guarded ratio) must
    reach the Sigma spec instead of being omitted as not translatable."""
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(
                name="orders",
                source="db.public.orders",
                fields=[
                    OssieField(
                        name=column,
                        expression=OssieExpression(
                            dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=column)]
                        ),
                    )
                    for column in ("amount", "hours")
                ],
            )
        ],
        metrics=[
            OssieMetric(
                name="gated",
                expression=OssieExpression(
                    dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=sql)]
                ),
            )
        ],
    )

    result = OssieToSigmaConverter().convert(document)
    element = result.output["pages"][0]["elements"][0]

    assert [m["formula"] for m in element.get("metrics", [])] == [expected_formula]
    assert not any(i.issue_type is ConverterIssueType.EXPRESSION_NOT_TRANSLATABLE for i in result.issues)


def test_a_sql_only_expression_is_reported_not_translatable_instead_of_crashing():
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(
                name="orders",
                source="db.sch.orders",
                fields=[OssieField(name="amount", expression=OssieExpression(sql="orders.amount"))],
            )
        ],
        metrics=[OssieMetric(name="revenue", expression=OssieExpression(sql="SUM(orders.amount)"))],
    )

    result = OssieToSigmaConverter().convert(document)
    element = result.output["pages"][0]["elements"][0]

    assert not element["columns"]
    assert not element.get("metrics")
    assert [
        i.element_name for i in result.issues if i.issue_type is ConverterIssueType.EXPRESSION_NOT_TRANSLATABLE
    ] == ["orders.amount"]


def test_synthesized_spec_carries_a_schema_version():
    """`schemaVersion` is required by the create/update endpoints."""
    document = OssieDocument.model_validate(
        yaml.safe_load((EXAMPLES_DIR / "tpcds_semantic_model.yaml").read_text())
    )
    assert OssieToSigmaConverter().convert(document).output["schemaVersion"] == 1


def test_datatypes_only_ever_emit_the_two_documented_format_kinds():
    document = OssieDocument(
        name="m",
        datasets=[
            OssieDataset(
                name="t",
                source="db.public.t",
                fields=[
                    OssieField(
                        name=datatype.lower(),
                        datatype=datatype,
                        expression=OssieExpression(
                            dialects=[
                                OssieDialectExpression(
                                    dialect=OssieDialect.ANSI_SQL, expression=datatype.lower()
                                )
                            ]
                        ),
                    )
                    for datatype in (
                        "String",
                        "Integer",
                        "Decimal",
                        "Float",
                        "Boolean",
                        "Date",
                        "Time",
                        "DateTime",
                        "DateTimeTz",
                    )
                ],
            )
        ],
    )

    columns = OssieToSigmaConverter().convert(document).output["pages"][0]["elements"][0]["columns"]
    kinds = {c["formula"].strip("[]"): c["format"]["kind"] for c in columns if "format" in c}
    assert kinds == {
        "integer": "number",
        "decimal": "number",
        "float": "number",
        "date": "date",
        "datetime": "date",
        "datetimetz": "date",
    }
    # String/Boolean/Time have no Sigma display format; emitting an invented `kind`
    # would be rejected by the data model API for the whole document.
    assert {c["formula"].strip("[]") for c in columns if "format" not in c} == {"string", "boolean", "time"}


def _relationship_model(
    dim_day_key: str = "date_key",
    reverse: bool = False,
    dim_day_dialect: OssieDialect = OssieDialect.ANSI_SQL,
) -> OssieDocument:
    """One fact joined to two dimensions: one on a differently named column, one on a
    column that has the same name on both sides."""

    def dataset(name: str, *fields: str, dialect: OssieDialect = OssieDialect.ANSI_SQL) -> OssieDataset:
        return OssieDataset(
            name=name,
            source=f"db.public.{name}",
            fields=[
                OssieField(
                    name=f,
                    expression=OssieExpression(
                        dialects=[OssieDialectExpression(dialect=dialect, expression=f)]
                    ),
                )
                for f in fields
            ],
        )

    datasets = [
        dataset("dim_day", "date_key", dialect=dim_day_dialect),
        dataset("dim_day_alt", "calendar_date"),
        dataset("fact_day", "calendar_date"),
    ]
    return OssieDocument(
        name="m",
        datasets=datasets[::-1] if reverse else datasets,
        relationships=[
            OssieRelationship(
                name="fact_day_to_dim_day",
                **{"from": "fact_day"},
                to="dim_day",
                from_columns=["calendar_date"],
                to_columns=[dim_day_key],
            ),
            OssieRelationship(
                name="fact_day_to_dim_day_alt",
                **{"from": "fact_day"},
                to="dim_day_alt",
                from_columns=["calendar_date"],
                to_columns=["calendar_date"],
            ),
        ],
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["targets_first", "targets_last"])
def test_relationship_keys_use_the_column_id_of_each_side(reverse):
    """The target key must be a column id of the target element. Looking it up in the
    source element wrote the bare name when the names differed, and the source column's
    id when they matched; Sigma rejects both. Relationships are built after every
    element, so a target that comes later in `datasets` resolves too."""
    result = OssieToSigmaConverter().convert(_relationship_model(reverse=reverse))

    elements = {e["name"]: e for p in result.output["pages"] for e in p["elements"]}
    fact = elements["fact_day"]
    fact_column_id = fact["columns"][0]["id"]
    for relationship in fact["relationships"]:
        target = next(e for e in elements.values() if e["id"] == relationship["targetElementId"])
        [key] = relationship["keys"]
        assert key["sourceColumnId"] == fact_column_id
        assert key["targetColumnId"] == target["columns"][0]["id"]
        assert key["targetColumnId"] in {c["id"] for c in target["columns"]}
    assert not result.issues


def test_relationship_column_missing_from_its_dataset_is_reported():
    result = OssieToSigmaConverter().convert(_relationship_model(dim_day_key="no_such_column"))

    unresolved = [i for i in result.issues if i.issue_type is ConverterIssueType.RELATIONSHIP_COLUMN_UNRESOLVED]
    assert len(unresolved) == 1
    assert "no_such_column" in unresolved[0].detail
    assert "dim_day" in unresolved[0].detail


def test_relationship_to_an_omitted_column_is_dropped_and_reported():
    """A field with no usable expression gets no Sigma column. A key pointing at it can
    never resolve, so the relationship is dropped and the converter says why."""
    result = OssieToSigmaConverter().convert(_relationship_model(dim_day_dialect=OssieDialect.MDX))

    elements = {e["name"]: e for p in result.output["pages"] for e in p["elements"]}
    dim_day_id = elements["dim_day"]["id"]
    fact = elements["fact_day"]
    assert [r["targetElementId"] for r in fact["relationships"]] != []
    assert all(r["targetElementId"] != dim_day_id for r in fact["relationships"])
    unresolved = [i for i in result.issues if i.issue_type is ConverterIssueType.RELATIONSHIP_COLUMN_UNRESOLVED]
    assert len(unresolved) == 1
    assert "date_key" in unresolved[0].detail
    assert "omitted" in unresolved[0].detail
