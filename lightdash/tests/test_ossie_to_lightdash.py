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

from ossie import (
    OssieAIContextObject,
    OssieCustomExtension,
    OssieDataType,
    OssieDataset,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
    OssieRelationship,
)

from ossie_lightdash import (
    ConverterIssueType,
    LightdashToOssieConverter,
    OssieToLightdashConverter,
)


def _ansi(expression: str) -> OssieExpression:
    return OssieExpression(
        dialects=[
            OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=expression)
        ]
    )


def _document() -> OssieDocument:
    orders = OssieDataset(
        name="orders",
        source="analytics_db.marts.orders",
        description="One row per order",
        fields=[
            OssieField(
                name="order_date",
                expression=_ansi("order_date"),
                dimension=OssieDimension(is_time=True),
                label="Order date",
            ),
            OssieField(
                name="status",
                expression=_ansi("status"),
                dimension=OssieDimension(is_time=False),
            ),
            OssieField(name="amount", expression=_ansi("amount")),
            OssieField(name="customer_id", expression=_ansi("customer_id")),
        ],
    )
    customers = OssieDataset(
        name="customers",
        source="analytics_db.marts.customers",
        fields=[OssieField(name="customer_id", expression=_ansi("customer_id"))],
    )
    metrics = [
        OssieMetric(
            name="total_amount",
            expression=_ansi("SUM(orders.amount)"),
            description="Sum of order amounts",
            custom_extensions=[
                OssieCustomExtension(
                    vendor_name="LIGHTDASH",
                    data=json.dumps({"label": "Total amount", "format": "usd"}),
                )
            ],
        ),
        OssieMetric(
            name="conversion_rate",
            expression=_ansi(
                "SUM(orders.completed_count) / NULLIF(SUM(orders.total_count), 0)"
            ),
            custom_extensions=[
                OssieCustomExtension(
                    vendor_name="LIGHTDASH",
                    data=json.dumps({"format": "percent", "round": 1}),
                )
            ],
        ),
        OssieMetric(
            name="p90_amount",
            expression=_ansi("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.amount)"),
        ),
        OssieMetric(
            name="distinct_amount",
            expression=_ansi("SUM(DISTINCT orders.amount)"),
        ),
        OssieMetric(
            name="completed_rate",
            expression=_ansi("AVG(CASE WHEN orders.status = 'completed' THEN 1 ELSE 0 END)"),
        ),
        OssieMetric(
            name="cross_dataset",
            expression=_ansi("SUM(orders.amount) / COUNT(customers.customer_id)"),
        ),
        OssieMetric(
            name="foreign_vendor_metric",
            expression=_ansi("SUM(orders.amount)"),
            custom_extensions=[
                OssieCustomExtension(vendor_name="somebi", data='{"x": 1}')
            ],
        ),
    ]
    relationships = [
        OssieRelationship.model_validate(
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            }
        )
    ]
    return OssieDocument(
        version="0.3.0.dev0",
        name="sales",
        datasets=[orders, customers],
        metrics=metrics,
        relationships=relationships,
    )


def _model(output, name):
    return next(m for m in output["models"] if m["name"] == name)


def _column(model, name):
    return next(c for c in model["columns"] if c["name"] == name)


@pytest.mark.parametrize("source,expected", [
    ('"analytics"."marts"."orders"', "orders"),
    ('`my-project.marts.orders`', "orders"),
    ('`analytics`.`marts`.`orders`', "orders"),
    ('[analytics].[marts].[orders]', "orders"),
    ('(SELECT * FROM raw.orders)', "sales_orders"),
    ('"marts"."order.items"', "sales_orders"),
])
def test_export_names_and_join_references_use_the_same_safe_mapping(source, expected):
    document = _document()
    document.datasets[0] = document.datasets[0].model_copy(
        update={"name": "sales_orders", "source": source}
    )
    document.relationships[0] = document.relationships[0].model_copy(
        update={"from_dataset": "sales_orders"}
    )
    result = OssieToLightdashConverter().convert_models(document)
    assert result.output[0]["name"] == expected
    assert result.output[0]["sql_from"] == source
    assert result.output[0]["joins"][0]["sql_on"] == (
        f"${{{expected}.customer_id}} = ${{customers.customer_id}}"
    )
    assert OssieToLightdashConverter().convert(document).output["models"][0]["name"] == expected


def test_export_rejects_an_unsafe_fallback_name():
    document = _document()
    document.datasets[0] = document.datasets[0].model_copy(
        update={"name": "../outside", "source": "(SELECT * FROM raw.orders)"}
    )
    with pytest.raises(ValueError, match="no safe Lightdash model name"):
        OssieToLightdashConverter().convert_models(document)


def test_model_extension_cannot_override_validated_identity():
    document = _document()
    document.datasets[0] = document.datasets[0].model_copy(update={
        "custom_extensions": [OssieCustomExtension(
            vendor_name="LIGHTDASH",
            data=json.dumps({"name": "../outside", "sql_from": "wrong", "type": "chart"}),
        )]
    })
    model = OssieToLightdashConverter().convert_models(document).output[0]
    assert (model["name"], model["sql_from"], model["type"]) == (
        "orders", "analytics_db.marts.orders", "model"
    )


def test_export_prefers_portable_sql_over_an_unrelated_vendor_dialect():
    document = _document()
    document.datasets[0].fields[0] = document.datasets[0].fields[0].model_copy(update={
        "expression": OssieExpression(dialects=[
            OssieDialectExpression(dialect=OssieDialect.MDX, expression="[wrong]"),
            OssieDialectExpression(dialect=OssieDialect.OSSIE_SQL_2026, expression="order_date"),
        ])
    })
    result = OssieToLightdashConverter(OssieDialect.BIGQUERY).convert_models(document)
    assert result.output[0]["dimensions"][0]["sql"] == "${TABLE}.order_date"
    assert not any(issue.issue_type is ConverterIssueType.DIALECT_UNAVAILABLE for issue in result.issues)


class TestOssieToLightdash:
    def test_time_dimension_exports_date_type(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "order_date")
        assert column["meta"]["dimension"] == {"label": "Order date", "type": "date"}

    def test_dimension_with_nothing_to_say_needs_no_meta(self):
        # Every Lightdash column is a dimension by default.
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "status")
        assert "meta" not in column

    def test_measure_only_field_becomes_a_hidden_dimension(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "amount")
        assert column["meta"]["dimension"] == {"hidden": True}

    def test_simple_aggregation_becomes_column_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "amount")
        metric = column["meta"]["metrics"]["total_amount"]
        assert metric["type"] == "sum"
        assert metric["label"] == "Total amount"
        assert metric["format"] == "usd"
        assert metric["description"] == "Sum of order amounts"
        assert "sql" not in metric

    def test_complex_expression_becomes_model_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        metric = _model(result.output, "orders")["meta"]["metrics"]["conversion_rate"]
        assert metric["type"] == "number"
        assert (
            metric["sql"]
            == "SUM(${TABLE}.completed_count) / NULLIF(SUM(${TABLE}.total_count), 0)"
        )
        assert metric["format"] == "percent"
        assert metric["round"] == 1

    def test_metric_over_a_joined_dataset_lives_on_the_joining_model(self):
        result = OssieToLightdashConverter().convert(_document())
        metric = _model(result.output, "orders")["meta"]["metrics"]["cross_dataset"]
        assert metric == {
            "type": "number",
            "sql": "SUM(${TABLE}.amount) / COUNT(${customers.customer_id})",
        }
        assert not any(
            issue.issue_type is ConverterIssueType.CROSS_DATASET_METRIC_DROPPED
            for issue in result.issues
        )

    def test_metric_over_an_unjoined_dataset_is_dropped_with_issue(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        # customers does not join orders, so no model can host the metric.
        tampered = tampered.model_copy(
            update={
                "relationships": [],
                "metrics": [
                    *tampered.metrics,
                    OssieMetric(
                        name="reversed",
                        expression=_ansi("COUNT(customers.customer_id) / SUM(orders.amount)"),
                    ),
                ],
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert "metrics" not in _model(result.output, "customers").get("meta", {})
        assert {
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.CROSS_DATASET_METRIC_DROPPED
        } == {"cross_dataset", "reversed"}

    def test_field_references_resolve_through_declared_joins(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        orders = tampered.datasets[0]
        orders.fields.append(
            OssieField(
                name="customer_key",
                expression=_ansi("UPPER(customers.customer_id)"),
                dimension=OssieDimension(),
            )
        )
        customers = tampered.datasets[1]
        customers.fields.append(
            OssieField(
                name="last_order_status",
                expression=_ansi("orders.status"),
                dimension=OssieDimension(),
            )
        )
        result = OssieToLightdashConverter().convert(tampered)
        joined = _column(_model(result.output, "orders"), "customer_key")
        assert joined["meta"]["dimension"]["sql"] == "UPPER(${customers.customer_id})"
        unjoined = _column(_model(result.output, "customers"), "last_order_status")
        assert unjoined["meta"]["dimension"]["sql"] == "orders.status"
        assert [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.FIELD_REFERENCE_UNJOINED
        ] == ["last_order_status"]

    def test_preferred_dialect_falls_back_to_ansi_then_reports(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        orders = tampered.datasets[0]
        orders.fields[1] = OssieField(
            name="status",
            expression=OssieExpression(
                dialects=[
                    OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression="status"),
                    OssieDialectExpression(
                        dialect=OssieDialect.BIGQUERY, expression="LOWER(status)"
                    ),
                ]
            ),
            dimension=OssieDimension(),
        )
        orders.fields.append(
            OssieField(
                name="snowflake_only",
                expression=OssieExpression(
                    dialects=[
                        OssieDialectExpression(
                            dialect=OssieDialect.SNOWFLAKE, expression="status::VARCHAR"
                        )
                    ]
                ),
                dimension=OssieDimension(),
            )
        )
        result = OssieToLightdashConverter(OssieDialect.BIGQUERY).convert(tampered)
        model = _model(result.output, "orders")
        assert _column(model, "status")["meta"]["dimension"]["sql"] == "LOWER(status)"
        assert (
            _column(model, "snowflake_only")["meta"]["dimension"]["sql"] == "status::VARCHAR"
        )
        assert [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.DIALECT_UNAVAILABLE
        ] == ["snowflake_only"]

    def test_foreign_extension_is_reported(self):
        result = OssieToLightdashConverter().convert(_document())
        assert any(
            issue.issue_type is ConverterIssueType.FOREIGN_EXTENSION_IGNORED
            and issue.element_name == "foreign_vendor_metric"
            for issue in result.issues
        )

    def test_extension_vendor_name_is_the_registered_token(self):
        result = LightdashToOssieConverter().convert(
            {"models": [{"name": "t", "columns": [{"name": "c", "meta": {"dimension": {"format": "usd"}}}]}]},
            schema="s",
        )
        field = result.output.datasets[0].fields[0]
        assert field.custom_extensions[0].vendor_name == "LIGHTDASH"
        # Documents written before the registration used the lowercase name.
        legacy_document = result.output.model_copy(deep=True)
        legacy_document.datasets[0].fields[0] = field.model_copy(
            update={"custom_extensions": [
                OssieCustomExtension(vendor_name="lightdash", data=field.custom_extensions[0].data)
            ]}
        )
        legacy = OssieToLightdashConverter().convert(legacy_document)
        assert _column(_model(legacy.output, "t"), "c")["meta"]["dimension"]["format"] == "usd"
        assert not any(
            issue.issue_type is ConverterIssueType.FOREIGN_EXTENSION_IGNORED for issue in legacy.issues
        )

    def test_extension_cannot_override_structural_keys(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        metric = tampered.metrics[0].model_copy(
            update={
                "custom_extensions": [
                    OssieCustomExtension(
                        vendor_name="LIGHTDASH",
                        data=json.dumps(
                            {"label": "Total amount", "sql": "1 + 1", "description": "stale"}
                        ),
                    )
                ]
            }
        )
        tampered.metrics[0] = metric
        result = OssieToLightdashConverter().convert(tampered)
        column = _column(_model(result.output, "orders"), "amount")
        exported = column["meta"]["metrics"]["total_amount"]
        assert exported["label"] == "Total amount"
        assert "sql" not in exported
        assert exported["description"] == "Sum of order amounts"

    def test_mismatched_relationship_columns_are_skipped(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        relationship = OssieRelationship.model_validate(
            {
                "name": "broken",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id", "order_id"],
                "to_columns": ["customer_id"],
            }
        )
        tampered.relationships[0] = relationship
        result = OssieToLightdashConverter().convert(tampered)
        assert "joins" not in _model(result.output, "orders").get("meta", {})
        assert any(
            issue.issue_type is ConverterIssueType.RELATIONSHIP_COLUMNS_MISMATCHED
            and issue.element_name == "broken"
            for issue in result.issues
        )

    def test_invalid_extension_json_is_reported(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        metric = tampered.metrics[0].model_copy(
            update={
                "custom_extensions": [
                    OssieCustomExtension(vendor_name="LIGHTDASH", data="{not json")
                ]
            }
        )
        tampered.metrics[0] = metric
        result = OssieToLightdashConverter().convert(tampered)
        assert any(
            issue.issue_type is ConverterIssueType.EXTENSION_DATA_INVALID
            and issue.element_name == "total_amount"
            for issue in result.issues
        )

    def test_relationship_becomes_join(self):
        result = OssieToLightdashConverter().convert(_document())
        joins = _model(result.output, "orders")["meta"]["joins"]
        assert joins == [
            {
                "join": "customers",
                "sql_on": "${orders.customer_id} = ${customers.customer_id}",
                "relationship": "many-to-one",
            }
        ]

    def test_percentile_cont_becomes_percentile_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        column = _column(_model(result.output, "orders"), "amount")
        assert column["meta"]["metrics"]["p90_amount"] == {
            "type": "percentile",
            "percentile": 90,
        }
        assert column["meta"]["metrics"]["distinct_amount"] == {"type": "sum_distinct"}

    def test_aggregation_over_expression_becomes_typed_model_metric(self):
        result = OssieToLightdashConverter().convert(_document())
        metric = _model(result.output, "orders")["meta"]["metrics"]["completed_rate"]
        assert metric == {
            "type": "average",
            "sql": "CASE WHEN ${TABLE}.status = 'completed' THEN 1 ELSE 0 END",
        }

    def test_repeated_relationship_gets_an_alias(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        tampered.relationships.append(
            OssieRelationship.model_validate(
                {
                    "name": "orders_to_referrer",
                    "from": "orders",
                    "to": "customers",
                    "from_columns": ["amount"],
                    "to_columns": ["customer_id"],
                }
            )
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "orders")["meta"]["joins"] == [
            {
                "join": "customers",
                "sql_on": "${orders.customer_id} = ${customers.customer_id}",
                "relationship": "many-to-one",
            },
            {
                "join": "customers",
                "alias": "orders_to_referrer",
                "sql_on": "${orders.amount} = ${orders_to_referrer.customer_id}",
                "relationship": "many-to-one",
            },
        ]

    def test_self_join_gets_an_alias(self):
        tampered = _document().model_copy(deep=True)
        tampered.relationships.append(
            OssieRelationship.model_validate(
                {
                    "name": "orders_to_parent",
                    "from": "orders",
                    "to": "orders",
                    "from_columns": ["amount"],
                    "to_columns": ["order_id"],
                }
            )
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "orders")["meta"]["joins"][-1] == {
            "join": "orders",
            "alias": "orders_to_parent",
            "sql_on": "${orders.amount} = ${orders_to_parent.order_id}",
            "relationship": "many-to-one",
        }

    def test_join_alias_and_attributes_restore_from_extension(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        tampered.relationships[0] = OssieRelationship.model_validate(
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
                "custom_extensions": [
                    {
                        "vendor_name": "LIGHTDASH",
                        "data": json.dumps(
                            {"alias": "buyer", "relationship": "many-to-one", "sql_on": "1 = 1"}
                        ),
                    }
                ],
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "orders")["meta"]["joins"] == [
            {
                "join": "customers",
                "alias": "buyer",
                "sql_on": "${orders.customer_id} = ${buyer.customer_id}",
                "relationship": "many-to-one",
            }
        ]

    def test_primary_key_and_ai_context_become_model_meta(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        datasets = tampered.datasets
        datasets[0] = datasets[0].model_copy(
            update={
                "primary_key": ["order_id"],
                "ai_context": "Orders placed in the web shop.\nOne row per order.",
            }
        )
        datasets[1] = datasets[1].model_copy(
            update={
                "primary_key": ["customer_id", "region"],
                "ai_context": OssieAIContextObject(
                    instructions="Customer master data.",
                    synonyms=("clients", "buyers"),
                ),
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        orders_meta = _model(result.output, "orders")["meta"]
        assert orders_meta["primary_key"] == "order_id"
        assert orders_meta["ai_hint"] == [
            "Orders placed in the web shop.",
            "One row per order.",
        ]
        customers_meta = _model(result.output, "customers")["meta"]
        assert customers_meta["primary_key"] == ["customer_id", "region"]
        assert customers_meta["ai_hint"] == [
            "Customer master data.",
            "Also known as: clients, buyers",
        ]

    def test_field_and_metric_ai_context_become_ai_hints(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        orders = tampered.datasets[0]
        orders.fields[1] = orders.fields[1].model_copy(update={"ai_context": "Order lifecycle stage."})
        orders.fields[2] = orders.fields[2].model_copy(
            update={"ai_context": "Gross amount.", "datatype": OssieDataType.DECIMAL}
        )
        metrics = tampered.metrics
        metrics[0] = metrics[0].model_copy(update={"ai_context": "Revenue before refunds."})
        result = OssieToLightdashConverter().convert(tampered)
        model = _model(result.output, "orders")
        assert _column(model, "status")["meta"]["dimension"]["ai_hint"] == "Order lifecycle stage."
        # A hidden dimension keeps its hint and its type.
        assert _column(model, "amount")["meta"]["dimension"] == {
            "hidden": True,
            "ai_hint": "Gross amount.",
            "type": "number",
        }
        assert _column(model, "amount")["meta"]["metrics"]["total_amount"]["ai_hint"] == (
            "Revenue before refunds."
        )

    def test_time_axis_withdrawn_becomes_time_intervals_off(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        orders = tampered.datasets[0]
        orders.fields[0] = orders.fields[0].model_copy(
            update={"datatype": OssieDataType.DATE, "dimension": OssieDimension(is_time=False)}
        )
        result = OssieToLightdashConverter().convert(tampered)
        column = _column(_model(result.output, "orders"), "order_date")
        assert column["meta"]["dimension"] == {
            "label": "Order date",
            "type": "date",
            "time_intervals": "OFF",
        }

    def test_meta_can_be_placed_under_config(self):
        result = OssieToLightdashConverter(meta_under_config=True).convert(_document())
        model = _model(result.output, "orders")
        assert "meta" not in model
        assert model["config"]["meta"]["joins"][0]["join"] == "customers"
        column = _column(model, "order_date")
        assert "meta" not in column
        assert column["config"]["meta"]["dimension"]["type"] == "date"

    def test_lightdash_metric_name_comes_from_the_stash_then_the_prefix(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        tampered = tampered.model_copy(
            update={
                "metrics": [
                    OssieMetric(
                        name="orders_total_amount",
                        expression=_ansi("SUM(orders.amount)"),
                        custom_extensions=[
                            OssieCustomExtension(
                                vendor_name="LIGHTDASH",
                                data=json.dumps({"name": "total_amount", "label": "Total"}),
                            )
                        ],
                    ),
                    OssieMetric(name="orders_max_amount", expression=_ansi("MAX(orders.amount)")),
                    OssieMetric(name="min_amount", expression=_ansi("MIN(orders.amount)")),
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        metrics = _column(_model(result.output, "orders"), "amount")["meta"]["metrics"]
        assert metrics == {
            "total_amount": {"type": "sum", "label": "Total"},
            "max_amount": {"type": "max"},
            "min_amount": {"type": "min"},
        }

    def test_unqualified_metric_is_placed_on_the_stashed_model(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        tampered = tampered.model_copy(
            update={
                "metrics": [
                    OssieMetric(
                        name="customers_row_count",
                        expression=_ansi("COUNT(*)"),
                        custom_extensions=[
                            OssieCustomExtension(
                                vendor_name="LIGHTDASH",
                                data=json.dumps({"name": "row_count", "model": "customers"}),
                            )
                        ],
                    ),
                    OssieMetric(name="unplaceable", expression=_ansi("COUNT(*)")),
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "customers")["meta"]["metrics"] == {
            "row_count": {"type": "number", "sql": "COUNT(*)"}
        }
        assert [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.CROSS_DATASET_METRIC_DROPPED
        ] == ["unplaceable"]

    def test_stashed_joins_and_meta_restore_the_explore(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        datasets = tampered.datasets
        datasets[0] = datasets[0].model_copy(
            update={
                "custom_extensions": [
                    OssieCustomExtension(
                        vendor_name="LIGHTDASH",
                        data=json.dumps(
                            {
                                "sql_filter": "${TABLE}.deleted = false",
                                "joins": [
                                    {
                                        "join": "customers",
                                        "sql_on": "${orders.customer_id} = ${customers.customer_id} AND ${customers.active}",
                                    },
                                    {"join": "regions", "sql_on": "${customers.region_id} = ${regions.id}"},
                                ],
                            }
                        ),
                    )
                ]
            }
        )
        datasets[1] = datasets[1].model_copy(
            update={
                "fields": [
                    datasets[1].fields[0].model_copy(
                        update={
                            "custom_extensions": [
                                OssieCustomExtension(
                                    vendor_name="LIGHTDASH",
                                    data=json.dumps({"column_meta": {"additional_dimensions": {"id_prefix": {"type": "string", "sql": "LEFT(${TABLE}.customer_id, 2)"}}}}),
                                )
                            ]
                        }
                    )
                ]
            }
        )
        regions = OssieDataset(
            name="regions",
            source="analytics_db.marts.regions",
            fields=[OssieField(name="id", expression=_ansi("id"))],
        )
        tampered = tampered.model_copy(
            update={
                "datasets": [*datasets, regions],
                "metrics": [
                    OssieMetric(
                        name="regional_spread",
                        expression=_ansi("COUNT(orders.customer_id) / COUNT(DISTINCT regions.id)"),
                    ),
                ],
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        orders = _model(result.output, "orders")
        # The stashed join replaces the generated one to the same target and
        # the chained join is appended; the model meta comes back as is.
        assert orders["meta"]["joins"] == [
            {
                "join": "customers",
                "sql_on": "${orders.customer_id} = ${customers.customer_id} AND ${customers.active}",
            },
            {"join": "regions", "sql_on": "${customers.region_id} = ${regions.id}"},
        ]
        assert orders["meta"]["sql_filter"] == "${TABLE}.deleted = false"
        # A metric over the chained target resolves through the stashed join.
        assert orders["meta"]["metrics"]["regional_spread"] == {
            "type": "number",
            "sql": "COUNT(${TABLE}.customer_id) / COUNT(DISTINCT ${regions.id})",
        }
        customer_id = _column(_model(result.output, "customers"), "customer_id")
        assert customer_id["meta"]["additional_dimensions"] == {
            "id_prefix": {"type": "string", "sql": "LEFT(${TABLE}.customer_id, 2)"}
        }

    def test_lightdash_model_files_are_deployable_as_they_are(self):
        document = _document()
        tampered = document.model_copy(deep=True)
        datasets = tampered.datasets
        datasets[0] = datasets[0].model_copy(
            update={
                "primary_key": ["order_id"],
                "custom_extensions": [
                    OssieCustomExtension(
                        vendor_name="LIGHTDASH",
                        data=json.dumps({"sql_filter": "${TABLE}.deleted = false", "label": "Orders"}),
                    )
                ],
            }
        )
        result = OssieToLightdashConverter().convert_models(tampered)
        orders = next(m for m in result.output if m["name"] == "orders")
        assert list(orders)[:4] == ["type", "name", "label", "description"]
        assert orders["type"] == "model"
        assert orders["sql_from"] == "analytics_db.marts.orders"
        assert orders["primary_key"] == "order_id"
        assert orders["sql_filter"] == "${TABLE}.deleted = false"
        assert orders["joins"][0]["join"] == "customers"
        assert orders["metrics"]["conversion_rate"]["type"] == "number"
        dimensions = {d["name"]: d for d in orders["dimensions"]}
        # Every dimension carries its own type and sql; a measure-only field
        # is a hidden one; column metrics sit under their dimension.
        assert dimensions["order_date"] == {
            "name": "order_date",
            "type": "date",
            "label": "Order date",
            "sql": "${TABLE}.order_date",
        }
        assert dimensions["amount"]["hidden"] is True
        assert dimensions["amount"]["sql"] == "${TABLE}.amount"
        assert dimensions["amount"]["metrics"]["total_amount"]["type"] == "sum"
        # No datatype on `status`: the type is assumed and reported.
        assert dimensions["status"]["type"] == "string"
        assert [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.DIMENSION_TYPE_DEFAULTED
        ] == ["orders.status", "orders.amount", "orders.customer_id", "customers.customer_id"]


class TestReviewFindings:
    """Regressions from the apache/ossie-converters#7 review."""

    def test_stashed_join_resolves_references_by_dataset_name(self):
        """A stashed join names the Lightdash model; expressions name the
        Ossie dataset. When the two differ, the join must still make the
        dataset referenceable, or a cross-dataset metric is dropped with a
        false FIELD_REFERENCE_UNJOINED / CROSS_DATASET_METRIC_DROPPED."""
        store_sales = OssieDataset(
            name="sales_ds",
            source="public.store_sales",
            fields=[
                OssieField(name="customer_sk", expression=_ansi("customer_sk")),
                OssieField(name="amount", expression=_ansi("amount")),
            ],
            custom_extensions=[
                OssieCustomExtension(
                    vendor_name="LIGHTDASH",
                    data=json.dumps(
                        {
                            "joins": [
                                {
                                    "join": "customer",
                                    "sql_on": "${store_sales.customer_sk} = ${customer.customer_sk} AND ${customer.active}",
                                }
                            ]
                        }
                    ),
                )
            ],
        )
        customer = OssieDataset(
            name="customer_ds",
            source="public.customer",
            fields=[
                OssieField(name="customer_sk", expression=_ansi("customer_sk")),
                OssieField(name="segment", expression=_ansi("segment"), dimension=OssieDimension()),
            ],
        )
        document = OssieDocument(
            version="0.3.0.dev0",
            name="sales",
            datasets=[store_sales, customer],
            metrics=[
                OssieMetric(
                    name="amount_per_customer",
                    expression=_ansi("SUM(sales_ds.amount) / COUNT(DISTINCT customer_ds.customer_sk)"),
                )
            ],
        )
        store_sales.fields.append(
            OssieField(
                name="customer_segment",
                expression=_ansi("customer_ds.segment"),
                dimension=OssieDimension(),
            )
        )
        result = OssieToLightdashConverter().convert(document)
        model = _model(result.output, "store_sales")
        assert model["meta"]["joins"][0]["join"] == "customer"
        assert _column(model, "customer_segment")["meta"]["dimension"]["sql"] == "${customer.segment}"
        assert model["meta"]["metrics"]["amount_per_customer"]["sql"] == (
            "SUM(${TABLE}.amount) / COUNT(DISTINCT ${customer.customer_sk})"
        )
        assert result.issues == []

    def test_self_join_keeps_the_models_own_columns_on_table(self):
        """A self-join registers the dataset as a join reference; the
        dataset's own mapping must still win for its own columns."""
        employees = OssieDataset(
            name="employees",
            source="hr.employees",
            fields=[
                OssieField(name="id", expression=_ansi("id")),
                OssieField(name="manager_id", expression=_ansi("manager_id")),
                OssieField(name="name", expression=_ansi("name"), dimension=OssieDimension()),
                OssieField(
                    name="name_upper",
                    expression=_ansi("UPPER(employees.name)"),
                    dimension=OssieDimension(),
                ),
            ],
        )
        document = OssieDocument(
            version="0.3.0.dev0",
            name="hr",
            datasets=[employees],
            relationships=[
                OssieRelationship.model_validate(
                    {
                        "name": "reports_to",
                        "from": "employees",
                        "to": "employees",
                        "from_columns": ["manager_id"],
                        "to_columns": ["id"],
                        "custom_extensions": [
                            {"vendor_name": "LIGHTDASH", "data": json.dumps({"alias": "manager"})}
                        ],
                    }
                )
            ],
            metrics=[OssieMetric(name="headcount", expression=_ansi("COUNT(DISTINCT employees.id)"))],
        )
        result = OssieToLightdashConverter().convert(document)
        model = _model(result.output, "employees")
        assert model["meta"]["joins"] == [
            {
                "join": "employees",
                "alias": "manager",
                "sql_on": "${employees.manager_id} = ${manager.id}",
                "relationship": "many-to-one",
            }
        ]
        assert _column(model, "name_upper")["meta"]["dimension"]["sql"] == "UPPER(${TABLE}.name)"
        assert _column(model, "id")["meta"]["metrics"]["headcount"] == {"type": "count_distinct"}
        assert result.issues == []


class TestSecondReviewFindings:
    """Regressions from the second round of the apache/ossie-converters#7 review."""

    def test_cross_model_metric_goes_back_on_the_model_it_came_from(self):
        """A metric on `orders` whose expression reads only `customers`
        columns must export on `orders` again: on `customers` the same SQL
        aggregates over a different set of rows."""
        document = _document()
        tampered = document.model_copy(deep=True).model_copy(
            update={
                "metrics": [
                    OssieMetric(
                        name="orders_customer_count",
                        expression=_ansi("COUNT(DISTINCT customers.customer_id)"),
                        custom_extensions=[
                            OssieCustomExtension(
                                vendor_name="LIGHTDASH",
                                data=json.dumps({"name": "customer_count", "model": "orders"}),
                            )
                        ],
                    )
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "orders")["meta"]["metrics"] == {
            "customer_count": {"type": "count_distinct", "sql": "${customers.customer_id}"}
        }
        assert "metrics" not in _model(result.output, "customers").get("meta", {})
        assert result.issues == []

    def test_stashed_model_that_cannot_reach_a_dataset_falls_back_to_the_heuristic(self):
        """`customers` does not join `orders`, so a metric stashed on
        `customers` that reads `orders` goes on `orders` as before."""
        document = _document()
        tampered = document.model_copy(deep=True).model_copy(
            update={
                "metrics": [
                    OssieMetric(
                        name="customers_order_total",
                        expression=_ansi("SUM(orders.amount)"),
                        custom_extensions=[
                            OssieCustomExtension(
                                vendor_name="LIGHTDASH",
                                data=json.dumps({"name": "order_total", "model": "customers"}),
                            )
                        ],
                    )
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _column(_model(result.output, "orders"), "amount")["meta"]["metrics"] == {
            "order_total": {"type": "sum"}
        }
        assert result.issues == []

    def test_cross_model_metric_round_trips_to_its_own_model(self):
        """Lightdash -> Ossie -> Lightdash keeps a metric on the model that
        declares it, even when its SQL only references the joined model."""
        project = {
            "version": 2,
            "models": [
                {
                    "name": "orders",
                    "meta": {
                        "joins": [
                            {
                                "join": "customers",
                                "sql_on": "${orders.customer_id} = ${customers.customer_id}",
                            }
                        ],
                        "metrics": {
                            "active_customers": {
                                "type": "count_distinct",
                                "sql": "${customers.customer_id}",
                            },
                            "customer_tenure": {
                                "type": "number",
                                "sql": "SUM(${customers.tenure_days}) / NULLIF(COUNT(${customers.customer_id}), 0)",
                            },
                        },
                    },
                    "columns": [{"name": "customer_id"}, {"name": "amount"}],
                },
                {
                    "name": "customers",
                    "columns": [{"name": "customer_id"}, {"name": "tenure_days"}],
                },
            ],
        }
        imported = LightdashToOssieConverter().convert(
            project, schema="marts", semantic_model_name="sales"
        )
        assert imported.issues == []
        exported = OssieToLightdashConverter().convert(imported.output)
        assert exported.issues == []
        assert _model(exported.output, "orders")["meta"]["metrics"] == {
            "active_customers": {"type": "count_distinct", "sql": "${customers.customer_id}"},
            "customer_tenure": {
                "type": "number",
                "sql": "SUM(${customers.tenure_days}) / NULLIF(COUNT(${customers.customer_id}), 0)",
            },
        }
        assert "metrics" not in _model(exported.output, "customers").get("meta", {})

    def test_compound_percentile_expression_is_a_sql_metric(self):
        """Two percentiles added together are not one `percentile` metric."""
        document = _document()
        expression = (
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY orders.amount)"
            " - PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY orders.amount)"
        )
        tampered = document.model_copy(deep=True).model_copy(
            update={"metrics": [OssieMetric(name="amount_spread", expression=_ansi(expression))]}
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert "metrics" not in _column(_model(result.output, "orders"), "amount").get("meta", {})
        assert _model(result.output, "orders")["meta"]["metrics"] == {
            "amount_spread": {
                "type": "number",
                "sql": (
                    "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ${TABLE}.amount)"
                    " - PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY ${TABLE}.amount)"
                ),
            }
        }
        assert result.issues == []

    def test_metrics_that_strip_to_the_same_name_are_both_kept(self):
        """`revenue` and `orders_revenue` on `orders` both want the Lightdash
        name `revenue`. The second keeps its full name and is reported, rather
        than overwriting the first."""
        document = _document()
        tampered = document.model_copy(deep=True).model_copy(
            update={
                "metrics": [
                    OssieMetric(name="revenue", expression=_ansi("SUM(orders.amount)")),
                    OssieMetric(
                        name="orders_revenue",
                        expression=_ansi("SUM(orders.amount) - SUM(orders.completed_count)"),
                    ),
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        orders = _model(result.output, "orders")
        assert _column(orders, "amount")["meta"]["metrics"] == {"revenue": {"type": "sum"}}
        assert orders["meta"]["metrics"] == {
            "orders_revenue": {
                "type": "number",
                "sql": "SUM(${TABLE}.amount) - SUM(${TABLE}.completed_count)",
            }
        }
        collisions = [
            issue.element_name
            for issue in result.issues
            if issue.issue_type is ConverterIssueType.METRIC_NAME_COLLISION
        ]
        assert len(collisions) == 1
        assert collisions[0].startswith("orders_revenue ")
        assert "'revenue'" in collisions[0] and "'orders_revenue'" in collisions[0]

    def test_metric_names_from_the_extension_that_collide_are_suffixed(self):
        """When the colliding name comes from the extension, the full Ossie
        name can collide too, so the metric gets a suffix."""
        document = _document()
        named = lambda name: [  # noqa: E731
            OssieCustomExtension(vendor_name="LIGHTDASH", data=json.dumps({"name": name}))
        ]
        tampered = document.model_copy(deep=True).model_copy(
            update={
                "metrics": [
                    OssieMetric(name="revenue", expression=_ansi("SUM(orders.amount)")),
                    OssieMetric(
                        name="revenue_2",
                        expression=_ansi("MAX(orders.amount)"),
                        custom_extensions=named("revenue"),
                    ),
                    OssieMetric(
                        name="revenue_3",
                        expression=_ansi("MIN(orders.amount)"),
                        custom_extensions=named("revenue_2"),
                    ),
                ]
            }
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _column(_model(result.output, "orders"), "amount")["meta"]["metrics"] == {
            "revenue": {"type": "sum"},
            "revenue_2": {"type": "max"},
            "revenue_3": {"type": "min"},
        }
        assert [
            issue.issue_type for issue in result.issues
        ] == [ConverterIssueType.METRIC_NAME_COLLISION] * 2

    def test_string_literals_and_longer_paths_are_not_rewritten(self):
        """`orders` inside a string literal is data, and `orders` inside
        `warehouse.orders.id` is a schema; neither is a dataset reference."""
        document = _document()
        expression = (
            "SUM(CASE WHEN orders.status = 'see orders.total' "
            "THEN warehouse.orders.id ELSE 0 END)"
        )
        tampered = document.model_copy(deep=True).model_copy(
            update={"metrics": [OssieMetric(name="literal_metric", expression=_ansi(expression))]}
        )
        result = OssieToLightdashConverter().convert(tampered)
        assert _model(result.output, "orders")["meta"]["metrics"] == {
            "literal_metric": {
                "type": "sum",
                "sql": (
                    "CASE WHEN ${TABLE}.status = 'see orders.total' "
                    "THEN warehouse.orders.id ELSE 0 END"
                ),
            }
        }
        assert result.issues == []

    def test_a_dataset_named_only_in_a_literal_is_not_a_cross_dataset_reference(self):
        """A dimension whose SQL mentions `customers` inside a string must not
        raise FIELD_REFERENCE_UNJOINED, and a metric that does so must not be
        dropped as cross-dataset."""
        document = _document()
        tampered = document.model_copy(deep=True)
        tampered = tampered.model_copy(
            update={
                "relationships": [],
                "metrics": [
                    OssieMetric(
                        name="orders_label_count",
                        expression=_ansi("COUNT(CASE WHEN orders.status = 'customers.vip' THEN 1 END)"),
                    )
                ],
            }
        )
        tampered.datasets[0].fields.append(
            OssieField(
                name="source_note",
                expression=_ansi("'from customers.table' || orders.status"),
                dimension=OssieDimension(),
            )
        )
        result = OssieToLightdashConverter().convert(tampered)
        orders = _model(result.output, "orders")
        assert _column(orders, "source_note")["meta"]["dimension"]["sql"] == (
            "'from customers.table' || ${TABLE}.status"
        )
        assert orders["meta"]["metrics"]["label_count"]["sql"] == (
            "CASE WHEN ${TABLE}.status = 'customers.vip' THEN 1 END"
        )
        assert result.issues == []


class TestDatasetScopedMetrics:
    """`datasets[].metrics` (core-spec 0.3.0.dev0) export like model-scoped
    metrics, always on their own dataset's model."""

    def _document(self, *metrics):
        document = _document()
        datasets = [
            document.datasets[0].model_copy(update={"metrics": list(metrics)}),
            *document.datasets[1:],
        ]
        return document.model_copy(update={"datasets": datasets, "metrics": []})

    def test_aggregation_over_a_field_becomes_a_column_metric(self):
        document = self._document(
            OssieMetric(name="total_amount", expression=_ansi("SUM(amount)"), description="Total")
        )
        result = OssieToLightdashConverter().convert(document)
        assert _column(_model(result.output, "orders"), "amount")["meta"]["metrics"] == {
            "total_amount": {"description": "Total", "type": "sum"}
        }
        assert result.issues == []

    def test_expression_fields_are_qualified_with_table(self):
        document = self._document(
            OssieMetric(
                name="amount_per_customer",
                expression=_ansi("SUM(amount) / NULLIF(COUNT(DISTINCT customer_id), 0)"),
            ),
            OssieMetric(
                name="orders_completed_amount",
                expression=_ansi("SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END)"),
            ),
        )
        result = OssieToLightdashConverter().convert(document)
        assert _model(result.output, "orders")["meta"]["metrics"] == {
            "amount_per_customer": {
                "type": "number",
                "sql": "SUM(${TABLE}.amount) / NULLIF(COUNT(DISTINCT ${TABLE}.customer_id), 0)",
            },
            "completed_amount": {
                "type": "sum",
                "sql": "CASE WHEN ${TABLE}.status = 'completed' THEN ${TABLE}.amount ELSE 0 END",
            },
        }
        assert result.issues == []

    def test_dataset_scoped_metric_stays_on_its_dataset(self):
        """Even with no dataset reference in the expression, and more than one
        dataset in the document, the metric is placed, not dropped."""
        document = self._document(OssieMetric(name="order_count", expression=_ansi("COUNT(*)")))
        result = OssieToLightdashConverter().convert(document)
        assert _model(result.output, "orders")["meta"]["metrics"] == {
            "order_count": {"type": "number", "sql": "COUNT(*)"}
        }
        assert "metrics" not in _model(result.output, "customers").get("meta", {})
        assert result.issues == []

    def test_dataset_scoped_metric_round_trips_as_a_model_scoped_metric(self):
        document = self._document(
            OssieMetric(name="total_amount", expression=_ansi("SUM(amount)"))
        )
        exported = OssieToLightdashConverter().convert(document)
        reimported = LightdashToOssieConverter().convert(
            exported.output, database="analytics_db", schema="marts", semantic_model_name="sales"
        )
        assert reimported.issues == []
        metrics = {
            metric.name: metric.expression.dialects[0].expression
            for metric in reimported.output.metrics
        }
        assert metrics == {"orders_total_amount": "SUM(orders.amount)"}
