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

"""How much of a model comes back usable after Ossie -> Lightdash -> Ossie.

These are the portability measurements apache/ossie#576 proposes, as the
M0-M3 fixtures of apache/ossie#596: one dataset and one SUM (M0), plus a
scalar calculated column (M1), the core aggregates (M2), and a conditional
aggregate (M3). Each metric must come back (nothing dropped, silently or
otherwise), with its expression intact, and tagged with a dialect the next
tool can read: ``ANSI_SQL`` by default, or ``OSSIE_SQL_2026`` when the
import is told that is what the Lightdash SQL is written in.
"""

import json

import pytest

from ossie import OssieDialect, OssieDocument

from ossie_lightdash import LightdashToOssieConverter, OssieToLightdashConverter
from ossie_lightdash.datatype_utils import datatype_to_lightdash_type
from ossie_lightdash.dbt_project import model_file_to_dbt_model


def _expression(sql: str, dialect: str = "OSSIE_SQL_2026"):
    return {"dialects": [{"dialect": dialect, "expression": sql}]}


def _field(name: str, sql: str, datatype: str = "Decimal", dialect: str = "OSSIE_SQL_2026"):
    return {"name": name, "datatype": datatype, "expression": _expression(sql, dialect), "dimension": {}}


def _metric(name: str, sql: str, dialect: str = "OSSIE_SQL_2026"):
    return {"name": name, "expression": _expression(sql, dialect)}


def _model(fields, metrics):
    return OssieDocument.model_validate(
        {
            "version": "0.2.0.dev0",
            "name": "portability",
            "datasets": [{"name": "orders", "source": "public.orders", "fields": fields}],
            "metrics": metrics,
        }
    )


M0_QUALIFIED = _model([_field("amount", "amount")], [_metric("total_amount", "SUM(orders.amount)")])
M0_ANSI = _model(
    [_field("amount", "amount", dialect="ANSI_SQL")],
    [_metric("total_amount", "SUM(orders.amount)", dialect="ANSI_SQL")],
)
M1 = _model(
    [_field("amount", "amount"), _field("amount_with_tax", "orders.amount * 1.1")],
    [_metric("total_amount", "SUM(orders.amount)")],
)
M2 = _model(
    [_field("amount", "amount"), _field("customer_id", "customer_id", "Integer")],
    [
        _metric("total_amount", "SUM(orders.amount)"),
        _metric("order_count", "COUNT(orders.customer_id)"),
        _metric("customer_count", "COUNT(DISTINCT orders.customer_id)"),
        _metric("average_amount", "AVG(orders.amount)"),
        _metric("smallest_amount", "MIN(orders.amount)"),
        _metric("largest_amount", "MAX(orders.amount)"),
    ],
)
M3 = _model(
    [_field("amount", "amount"), _field("status", "status", "String")],
    [
        _metric("total_amount", "SUM(orders.amount)"),
        _metric("completed_amount", "SUM(CASE WHEN orders.status = 'completed' THEN orders.amount ELSE 0 END)"),
    ],
)

FIXTURES = {"M0": M0_QUALIFIED, "M0-ansi": M0_ANSI, "M1": M1, "M2": M2, "M3": M3}


def _round_trip(document: OssieDocument, import_dialect=OssieDialect.ANSI_SQL):
    exported = OssieToLightdashConverter().convert_models(document)
    assert exported.issues == [], exported.issues
    reimported = LightdashToOssieConverter(import_dialect).convert(
        {"version": 2, "models": [model_file_to_dbt_model(model) for model in exported.output]},
        semantic_model_name=document.name,
    )
    assert reimported.issues == [], reimported.issues
    return reimported.output


def _lightdash_name(metric) -> str:
    for extension in metric.custom_extensions or []:
        if extension.vendor_name.upper() == "LIGHTDASH":
            return json.loads(extension.data)["name"]
    return metric.name


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_every_metric_survives_with_its_expression(fixture, assert_valid_ossie):
    original = FIXTURES[fixture]
    returned = _round_trip(original)
    assert_valid_ossie(returned)

    # Lightdash scopes metric names per model, so the Ossie name is
    # `<model>_<metric>` and the bare name travels in the extension.
    by_name = {_lightdash_name(metric): metric for metric in returned.metrics}
    assert set(by_name) == {metric.name for metric in original.metrics}
    for metric in original.metrics:
        assert by_name[metric.name].name == f"orders_{metric.name}"
        assert by_name[metric.name].expression.dialects[0].expression == metric.expression.dialects[0].expression

    fields = {field.name: field for field in returned.datasets[0].fields}
    for field in original.datasets[0].fields:
        assert field.name in fields
        assert fields[field.name].expression.dialects[0].expression == field.expression.dialects[0].expression
        # Lightdash's `number` covers Integer, Decimal and Float, so a datatype
        # round-trips by category (see "Types" in the README), not by member.
        assert datatype_to_lightdash_type(fields[field.name].datatype) == datatype_to_lightdash_type(field.datatype)


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_expressions_come_back_portable(fixture):
    """Tagged `ANSI_SQL` unless told otherwise: Lightdash records which
    warehouse a project runs on, not which SQL subset an expression uses."""
    returned = _round_trip(FIXTURES[fixture])
    tags = {
        dialect.dialect
        for element in [*returned.metrics, *returned.datasets[0].fields]
        for dialect in element.expression.dialects
    }
    assert tags == {OssieDialect.ANSI_SQL}


@pytest.mark.parametrize("fixture", ["M0", "M1", "M2", "M3"])
def test_the_portable_tag_is_kept_when_the_import_is_told_the_sql_is_portable(fixture):
    returned = _round_trip(FIXTURES[fixture], import_dialect=OssieDialect.OSSIE_SQL_2026)
    tags = {
        dialect.dialect
        for element in [*returned.metrics, *returned.datasets[0].fields]
        for dialect in element.expression.dialects
    }
    assert tags == {OssieDialect.OSSIE_SQL_2026}


def test_a_bare_column_reference_is_qualified_on_the_way_back():
    """The third M0 variant: `SUM(amount)` with no dataset qualifier. Export
    writes it as `${TABLE}.amount`, so it returns qualified."""
    bare = _model([_field("amount", "amount")], [_metric("total_amount", "SUM(amount)")])
    returned = _round_trip(bare)
    (metric,) = returned.metrics
    assert metric.expression.dialects[0].expression == "SUM(orders.amount)"
