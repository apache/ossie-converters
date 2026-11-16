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

"""Tests for the OSI YAML → Denodo VQL converter (osi_to_vql.py)."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from ossie_denodo.osi_to_vql import (
    OssieConversionError,
    _build_dimensions,
    _dataset_to_source,
    _extract_denodo_extension,
    _extract_expression,
    _relationship_to_assoc_ref,
    _relationship_to_association,
    convert_ossie_to_vql,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _minimal_ossie(**overrides) -> str:
    base = {
        "version": "0.2.0.dev0", #Used the most recent version in this case. Supported 0.1.1 and 0.2.0.dev0
        "name": "test_view",
        "datasets": [
            {
                "name": "orders",
                "source": "psql_orders",
                "fields": [
                    {
                        "name": "order_id",
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "orders.order_id"}]
                        },
                    }
                ],
            }
        ],
        "relationships": [],
        "metrics": [
            {
                "name": "total",
                "expression": {
                    "dialects": [{"dialect": "ANSI_SQL", "expression": "count(orders.order_id)"}]
                },
            }
        ],
    }
    base.update(overrides)
    return yaml.dump(base, default_flow_style=False, sort_keys=False)

# ---------------------------------------------------------------------------
# Tests for converter.py entry point
# ---------------------------------------------------------------------------

class TestConverterEntryPoint:
    def test_convert_ossie_to_denodo_delegates(self):
        from ossie_denodo.converter import convert_ossie_to_denodo
        result = convert_ossie_to_denodo(_minimal_ossie())
        assert "CREATE OR REPLACE METRIC VIEW test_view" in result

    def test_convert_ossie_to_denodo_raises_on_invalid(self):
        from ossie_denodo.converter import OssieConversionError, convert_ossie_to_denodo
        with pytest.raises(OssieConversionError):
            convert_ossie_to_denodo("- not a mapping")


# ---------------------------------------------------------------------------
# Unit tests: _extract_expression
# ---------------------------------------------------------------------------

class TestExtractExpression:
    def test_ansi_sql_dialect(self):
        expr = {"dialects": [{"dialect": "ANSI_SQL", "expression": "orders.amount"}]}
        assert _extract_expression(expr) == "orders.amount"

    def test_denodo_dialect_preferred(self):
        expr = {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": "ansi_expr"},
                {"dialect": "DENODO", "expression": "denodo_expr"},
            ]
        }
        assert _extract_expression(expr) == "denodo_expr"

    def test_fallback_to_first_when_no_preferred(self):
        expr = {"dialects": [{"dialect": "BIGQUERY", "expression": "bq_expr"}]}
        assert _extract_expression(expr) == "bq_expr"

    def test_none_expression_returns_none(self):
        assert _extract_expression(None) is None

    def test_empty_dialects_returns_none(self):
        assert _extract_expression({"dialects": []}) is None


# ---------------------------------------------------------------------------
# Unit tests: _dataset_to_source
# ---------------------------------------------------------------------------

class TestDatasetToSource:
    def test_source_with_database_prefix(self):
        ds = {"name": "nation", "source": "demo_db.psql_tpch_nation"}
        src = _dataset_to_source(ds)
        assert src.alias == "nation"
        assert src.database == "demo_db"
        assert src.view == "psql_tpch_nation"

    def test_source_without_database_prefix(self):
        ds = {"name": "orders", "source": "psql_tpch_orders"}
        src = _dataset_to_source(ds)
        assert src.alias == "orders"
        assert src.database is None
        assert src.view == "psql_tpch_orders"

    def test_origin_field_used_as_fallback(self):
        ds = {"name": "orders", "origin": "psql_tpch_orders"}
        src = _dataset_to_source(ds)
        assert src.view == "psql_tpch_orders"

    def test_quoted_alias(self):
        ds = {"name": "#nation", "source": "psql_tpch_nation"}
        src = _dataset_to_source(ds)
        assert src.alias == "#nation"


# ---------------------------------------------------------------------------
# Unit tests: _relationship_to_association
# ---------------------------------------------------------------------------

class TestRelationshipToAssociation:
    def test_basic_relationship(self):
        rel = {
            "name": "psql_tpch_nation_psql_tpch_customer",
            "from": "customer",
            "to": "nation",
            "from_columns": ["c_nationkey"],
            "to_columns": ["n_nationkey"],
        }
        assoc = _relationship_to_association(rel)
        assert assoc is not None
        assert assoc.name == "psql_tpch_nation_psql_tpch_customer"
        assert assoc.folder is None
        assert assoc.referential_constraint is True
        assert len(assoc.endpoints) == 2
        assert assoc.mappings == [("c_nationkey", "n_nationkey")]

    def test_principal_endpoint_is_to_side(self):
        rel = {
            "name": "assoc",
            "from": "orders",
            "to": "customer",
            "from_columns": ["o_custkey"],
            "to_columns": ["c_custkey"],
        }
        alias_to_source = {"orders": "psql_tpch_orders", "customer": "psql_tpch_customer"}
        assoc = _relationship_to_association(rel, alias_to_source)
        principal_ep = next(ep for ep in assoc.endpoints if ep.is_principal)
        # role_name = from source view (the "many" side), view_name = to source view (the "one" side)
        assert principal_ep.role_name == "psql_tpch_orders"
        assert principal_ep.view_name == "psql_tpch_customer"
        assert principal_ep.cardinality == "1"

    def test_many_side_cardinality(self):
        rel = {
            "name": "assoc",
            "from": "orders",
            "to": "customer",
            "from_columns": ["o_custkey"],
            "to_columns": ["c_custkey"],
        }
        alias_to_source = {"orders": "psql_tpch_orders", "customer": "psql_tpch_customer"}
        assoc = _relationship_to_association(rel, alias_to_source)
        many_ep = next(ep for ep in assoc.endpoints if not ep.is_principal)
        # role_name = to source view (the "one" side), view_name = from source view (the "many" side)
        assert many_ep.role_name == "psql_tpch_customer"
        assert many_ep.view_name == "psql_tpch_orders"
        assert many_ep.cardinality == "0,*"

    def test_missing_name_returns_none(self):
        rel = {"from": "a", "to": "b", "from_columns": [], "to_columns": []}
        assert _relationship_to_association(rel) is None

    def test_multiple_mappings(self):
        rel = {
            "name": "assoc",
            "from": "a",
            "to": "b",
            "from_columns": ["col1", "col2"],
            "to_columns": ["col3", "col4"],
        }
        assoc = _relationship_to_association(rel)
        assert assoc.mappings == [("col1", "col3"), ("col2", "col4")]


# ---------------------------------------------------------------------------
# Unit tests: _relationship_to_assoc_ref
# ---------------------------------------------------------------------------

class TestRelationshipToAssocRef:
    def test_basic(self):
        rel = {
            "name": "psql_tpch_region_psql_tpch_nation",
            "from": "nation",
            "to": "region",
        }
        ref = _relationship_to_assoc_ref(rel)
        assert ref.from_alias == "nation"
        assert ref.to_alias == "region"
        assert ref.association_name == "psql_tpch_region_psql_tpch_nation"
        assert ref.join_type is None


# ---------------------------------------------------------------------------
# Unit tests: _build_dimensions
# ---------------------------------------------------------------------------

class TestBuildDimensions:
    def test_single_field_is_ungrouped(self):
        datasets = [
            {
                "name": "nation",
                "source": "psql_tpch_nation",
                "fields": [
                    {
                        "name": "nation_name",
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "nation.n_name"}]
                        },
                    }
                ],
            }
        ]
        dims = _build_dimensions(datasets)
        assert len(dims) == 1
        assert dims[0].group_name is None
        assert dims[0].fields[0].name == "nation_name"

    def test_multiple_fields_are_grouped(self):
        datasets = [
            {
                "name": "customer",
                "source": "psql_tpch_customer",
                "fields": [
                    {
                        "name": "customer_name",
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "customer.c_name"}]
                        },
                    },
                    {
                        "name": "customer_address",
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "customer.c_address"}]
                        },
                    },
                ],
                "custom_extensions": [{
                    "vendor_name": "DENODO",
                    "data": '{"fields_dimension":{"customer_name": "customer_dim","customer_address": "customer_dim"}}'
                }]
            }
        ]
        dims = _build_dimensions(datasets)
        assert len(dims) == 1
        assert dims[0].group_name == "customer_dim"
        assert len(dims[0].fields) == 2

    def test_empty_fields_skipped(self):
        datasets = [{"name": "region", "source": "psql_tpch_region", "fields": []}]
        dims = _build_dimensions(datasets)
        assert dims == []


# ---------------------------------------------------------------------------
# Unit tests: _extract_denodo_extension
# ---------------------------------------------------------------------------

class TestExtractDenodoExtension:
    def test_extracts_folder_and_context(self):
        ossie = {
            "custom_extensions": [
                {
                    "vendor_name": "DENODO",
                    "data": '{"folder": "/3. views", "context": {"I18N": "us_pst"}}',
                }
            ]
        }
        ext = _extract_denodo_extension(ossie)
        assert ext["folder"] == "/3. views"
        assert ext["context"] == {"I18N": "us_pst"}

    def test_returns_none_when_no_denodo_extension(self):
        ossie = {"custom_extensions": [{"vendor_name": "SNOWFLAKE", "data": "{}"}]}
        assert _extract_denodo_extension(ossie) is None

    def test_returns_none_when_no_extensions(self):
        assert _extract_denodo_extension({}) is None


# ---------------------------------------------------------------------------
# Integration tests: convert_ossie_to_vql
# ---------------------------------------------------------------------------

class TestConvertOssieToVql:
    def test_invalid_yaml_root_raises(self):
        with pytest.raises(OssieConversionError, match="expected a mapping"):
            convert_ossie_to_vql("- a list")

    def test_wrong_version_raises(self):
        bad = yaml.dump({"version": "9.9.9", "name": "m"})
        with pytest.warns(UserWarning, match="Unsupported Ossie specification"):
            convert_ossie_to_vql(bad)

    def test_missing_name_raises(self):
        bad = yaml.dump({"version": "0.1.1", "datasets": []})
        with pytest.raises(OssieConversionError, match="Missing required 'name'"):
            convert_ossie_to_vql(bad)

    def test_legacy_semantic_model_format_supported(self):
        ossie_yaml = yaml.dump({
            "version": "0.1.1",
            "semantic_model": [
                {
                    "name": "test_view",
                    "datasets": [{"name": "orders", "source": "psql_orders", "fields": []}],
                    "metrics": [],
                }
            ],
        })
        result = convert_ossie_to_vql(ossie_yaml)
        assert "CREATE OR REPLACE METRIC VIEW test_view" in result

    def test_output_contains_metric_view(self):
        result = convert_ossie_to_vql(_minimal_ossie())
        assert "CREATE OR REPLACE METRIC VIEW test_view" in result

    def test_output_contains_sources(self):
        result = convert_ossie_to_vql(_minimal_ossie())
        assert "SOURCES" in result
        assert "orders AS psql_orders" in result

    def test_output_contains_metrics(self):
        result = convert_ossie_to_vql(_minimal_ossie())
        assert "METRICS" in result
        assert "total AS count(orders.order_id)" in result

    def test_output_contains_dimensions(self):
        result = convert_ossie_to_vql(_minimal_ossie())
        assert "DIMENSIONS" in result
        assert "order_id AS orders.order_id" in result

    def test_description_rendered(self):
        result = convert_ossie_to_vql(_minimal_ossie(description="My description"))
        assert "DESCRIPTION = 'My description'" in result

    def test_tags_rendered(self):
        result = convert_ossie_to_vql(
            _minimal_ossie(
                custom_extensions=[
                    {"vendor_name": "DENODO", "data": '{"tags": ["tag1", "tag2"]}'}
                ]
            )
        )
        assert "TAGS (tag1, tag2)" in result

    def test_folder_from_denodo_extension(self):
        result = convert_ossie_to_vql(
            _minimal_ossie(
                custom_extensions=[
                    {"vendor_name": "DENODO", "data": '{"folder": "/my/folder"}'}
                ]
            )
        )
        assert "FOLDER = '/my/folder'" in result

    def test_context_from_denodo_extension(self):
        result = convert_ossie_to_vql(
            _minimal_ossie(
                custom_extensions=[
                    {"vendor_name": "DENODO", "data": '{"context": {"I18N": "us_pst"}}'}
                ]
            )
        )
        assert "CONTEXT ('I18N' = 'us_pst')" in result

    def test_associations_generated(self):
        ossie_yaml = _minimal_ossie(
            datasets=[
                {"name": "orders", "source": "psql_orders", "fields": []},
                {"name": "customer", "source": "psql_customer", "fields": []},
            ],
            relationships=[
                {
                    "name": "orders_customer",
                    "from": "orders",
                    "to": "customer",
                    "from_columns": ["o_custkey"],
                    "to_columns": ["c_custkey"],
                }
            ],
        )
        result = convert_ossie_to_vql(ossie_yaml)
        assert "CREATE OR REPLACE ASSOCIATION orders_customer REFERENTIAL CONSTRAINT" in result
        assert "ADD MAPPING o_custkey=c_custkey" in result
        assert "ASSOCIATIONS" in result
        # Endpoints must use real source view names, not dataset aliases
        assert "ENDPOINT psql_orders psql_customer PRINCIPAL (1)" in result
        assert "ENDPOINT psql_customer psql_orders (0,*)" in result

    def test_no_relationships_no_associations(self):
        result = convert_ossie_to_vql(_minimal_ossie(relationships=[]))
        assert "CREATE OR REPLACE ASSOCIATION" not in result

    def test_quoted_identifier_in_output(self):
        ossie_yaml = _minimal_ossie(
            datasets=[
                {
                    "name": "#nation",
                    "source": "psql_tpch_nation",
                    "fields": [
                        {
                            "name": "nation_name",
                            "expression": {
                                "dialects": [
                                    {"dialect": "ANSI_SQL", "expression": '"#nation".n_name'}
                                ]
                            },
                        }
                    ],
                }
            ]
        )
        result = convert_ossie_to_vql(ossie_yaml)
        assert '"#nation"' in result

    def test_denodo_dialect_preferred_over_ansi(self):
        ossie_yaml = _minimal_ossie(
            metrics=[
                {
                    "name": "total",
                    "expression": {
                        "dialects": [
                            {"dialect": "ANSI_SQL", "expression": "count(orders.id)"},
                            {"dialect": "DENODO", "expression": "count(orders.denodo_id)"},
                        ]
                    },
                }
            ]
        )
        result = convert_ossie_to_vql(ossie_yaml)
        assert "count(orders.denodo_id)" in result
        assert "count(orders.id)" not in result


# ---------------------------------------------------------------------------
# Fixture-based round-trip tests: YAML → VQL → parse → assert
# ---------------------------------------------------------------------------

class TestFixtureRoundTrip:
    """Load each example YAML, convert to VQL, parse the VQL, assert key fields."""

    def _convert_vql(self, yaml_path: pathlib.Path):
        ossie_yaml = yaml_path.read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        return vql

    def test_example1_metric_view_name(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        assert "CREATE OR REPLACE METRIC VIEW metric_complex_fields" in vql

    def test_example1_expected_sources(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_sources = """    SOURCES (
        nation AS psql_tpch_nation,
        orders AS psql_tpch_orders,
        customer AS psql_tpch_customer
    )"""
        assert expected_sources in vql

    def test_example1_vql_associations_expected_a(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_association_a = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_psql_tpch_customer REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_customer psql_tpch_nation PRINCIPAL (1)
    ENDPOINT psql_tpch_nation psql_tpch_customer (0,*)
    ADD MAPPING c_nationkey=n_nationkey;"""
        assert expected_association_a in vql

    def test_example1_vql_associations_expected_b(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_association_c = """CREATE OR REPLACE ASSOCIATION psql_tpch_customer_psql_tpch_orders REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_orders psql_tpch_customer PRINCIPAL (1)
    ENDPOINT psql_tpch_customer psql_tpch_orders (0,*)
    ADD MAPPING o_custkey=c_custkey;"""
        assert expected_association_c in vql

    def test_example1_associations_in_view(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_association_block = """    ASSOCIATIONS (
        customer nation psql_tpch_nation_psql_tpch_customer,
        orders customer psql_tpch_customer_psql_tpch_orders
    )"""
        assert expected_association_block in vql

    def test_example1_dimensions(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_dimensions = """    DIMENSIONS (
        nation_name AS nation.n_name,
        order_date AS orders.o_orderdate,
        order_dim_extra AS (orders.o_orderkey+5),
        customer_address_full AS concat(customer.c_address, '-', nation.n_name),
        customer_name AS customer.c_name,
        customer_address AS customer.c_address,
        customer_comment AS customer.c_comment,
        generic_attribute AS (orders.o_orderkey-1)
    )"""
        assert expected_dimensions in vql

    def test_example1_metrics(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_metrics = """    METRICS (
        count_order_keys AS count(orders.o_orderkey),
        order_average_value AS avg(orders.o_totalprice)
    )"""
        assert expected_metrics in vql

    def test_example1_custom_extension_folder(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_folder = """CREATE OR REPLACE METRIC VIEW metric_complex_fields
    FOLDER = '/3. views'"""
        assert expected_folder in vql

    def test_example1_custom_extension_context(self):
        vql = self._convert_vql(FIXTURES / "example1.yaml")
        expected_context = """    METRICS (
        count_order_keys AS count(orders.o_orderkey),
        order_average_value AS avg(orders.o_totalprice)
    )
CONTEXT ('I18N' = 'us_pst');"""
        assert expected_context in vql

    def test_example2_database_prefix_in_sources(self):
        vql = self._convert_vql(FIXTURES / "example2.yaml")
        expected_source_prefix = """    SOURCES (
        nation AS demo_metricview_metadata_db.psql_tpch_nation,
        orders AS demo_metricview_metadata_db.psql_tpch_orders,
        customer AS demo_metricview_metadata_db.psql_tpch_customer
    )"""
        assert expected_source_prefix in vql

    def test_example2_association_names_include_db_prefix(self):
        vql = self._convert_vql(FIXTURES / "example2.yaml")
        expected_association_prefix = """    ASSOCIATIONS (
        customer nation demo_metricview_metadata_db.psql_tpch_nation_psql_tpch_customer,
        orders customer demo_metricview_metadata_db.psql_tpch_customer_psql_tpch_orders
    )"""
        assert expected_association_prefix in vql

    def test_example3_complex_metric_expression_preserved(self):
        vql = self._convert_vql(FIXTURES / "example3.yaml")
        expected_metric_complex_expression = """test_metric AS nullif(sum(case WHEN (orders.o_totalprice > 10000) THEN 1 ELSE 0 END), 0)"""
        assert expected_metric_complex_expression in vql

    def test_example3_metrics(self):
        vql = self._convert_vql(FIXTURES / "example3.yaml")
        expected_metrics = """    METRICS (
        count_order_keys AS count(orders.o_orderkey),
        order_average_value AS avg(orders.o_totalprice),
        test_metric AS nullif(sum(case WHEN (orders.o_totalprice > 10000) THEN 1 ELSE 0 END), 0)
    )"""
        assert expected_metrics in vql

    def test_example4_quoted_identifiers_in_sources(self):
        vql = self._convert_vql(FIXTURES / "example4.yaml")
        expected_sources_quoted_identifiers = """    SOURCES (
        "#nation" AS psql_tpch_nation,
        "#orders" AS psql_tpch_orders,
        customer AS psql_tpch_customer
    )"""
        assert expected_sources_quoted_identifiers in vql

    def test_example4_quoted_associations(self):
        vql = self._convert_vql(FIXTURES / "example4.yaml")
        expected_association_quoted = """    ASSOCIATIONS (
        customer "#nation" psql_tpch_nation_psql_tpch_customer,
        "#orders" customer psql_tpch_customer_psql_tpch_orders
    )"""
        assert expected_association_quoted in vql

    def test_example4_quoted_dimensions(self):
        vql = self._convert_vql(FIXTURES / "example4.yaml")
        expected_quoted_dimensions = """    DIMENSIONS (
        nation_name AS "#nation".n_name,
        order_date AS "#orders".o_orderdate,
        customer_address_full AS concat(customer.c_address, '-', "#nation".n_name),
        customer_name AS customer.c_name,
        "#customer_address" AS customer.c_address,
        customer_comment AS customer.c_comment
    )"""
        assert expected_quoted_dimensions in vql

    def test_example4_quoted_metrics(self):
        vql = self._convert_vql(FIXTURES / "example4.yaml")
        expected_quoted_metrics = """    METRICS (
        "#count_order_keys" AS count("#orders".o_orderkey),
        order_average_value AS avg("#orders".o_totalprice)
    )"""
        assert expected_quoted_metrics in vql

    def test_example4_folder_description_tags_from_denodo_extension(self):
        vql = self._convert_vql(FIXTURES / "example4.yaml")
        expected_quoted_metrics = """    FOLDER = '/3. views'
    DESCRIPTION = 'Example of metric view'
    TAGS (sales_data, sensitive)"""
        assert expected_quoted_metrics in vql

class TestExample5Filter:
    """Tests for the example_osi_yaml.yaml fixture: YAML → VQL → parse → assert."""

    FIXTURE = FIXTURES / "example5.yaml"

    def _convert_vql(self):
        import pathlib
        from ossie_denodo.osi_to_vql import convert_ossie_to_vql
        text = pathlib.Path(self.FIXTURE).read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(text)
        return vql

    def test_check_filter(self):
        vql = self._convert_vql()
        expected = """    FILTER (
        nation.n_name <> "NON_EXISTENT_NATION"
    )"""
        assert expected in vql

    def test_check_filter_between_sources_association(self):
        vql = self._convert_vql()
        expected = """    SOURCES (
        nation AS psql_tpch_nation,
        orders AS psql_tpch_orders,
        region AS psql_tpch_region,
        customer AS psql_tpch_customer
    )
    FILTER (
        nation.n_name <> "NON_EXISTENT_NATION"
    )
    ASSOCIATIONS (
        customer nation psql_tpch_nation_psql_tpch_customer,
        nation region psql_tpch_region_psql_tpch_nation,
        orders customer psql_tpch_customer_psql_tpch_orders
    )"""
        assert expected in vql

# ---------------------------------------------------------------------------
# Tests for example_osi_yaml.yaml fixture
# ---------------------------------------------------------------------------

class TestExampleOsiYamlFixture:
    """Tests for the example_osi_yaml.yaml fixture: YAML → VQL → parse → assert."""

    FIXTURE = FIXTURES / "example_osi_yaml.yaml"

    def _convert_vql(self):
        ossie_yaml = self.FIXTURE.read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        return vql

    def test_metric_view_name(self):
        vql = self._convert_vql()
        expected_name_and_description = """CREATE OR REPLACE METRIC VIEW ecommerce_analytics
    DESCRIPTION = 'E-commerce sales and customer analytics'"""
        assert expected_name_and_description in vql

    def test_sources_count(self):
        """Sources with 3-part names (db.schema.view) must be reduced to db.view."""
        vql = self._convert_vql()
        expected_sources = """    SOURCES (
        orders AS sales.orders,
        customers AS sales.customers
    )"""
        assert expected_sources in vql

    def test_schema_warning_emitted(self):
        """A UserWarning must be emitted for each source with a schema segment."""
        import warnings
        ossie_yaml = self.FIXTURE.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            convert_ossie_to_vql(ossie_yaml)
        schema_warnings = [w for w in caught if issubclass(w.category, UserWarning) and "schema" in str(w.message).lower()]
        # Both orders and customers sources have a schema segment
        assert len(schema_warnings) >= 2

    def test_association_name(self):
        vql = self._convert_vql()
        expected_association = """    ASSOCIATIONS (
        orders customers orders_to_customers
    )"""
        assert expected_association in vql

    def test_associations_count(self):
        vql = self._convert_vql()
        expected_association = """CREATE OR REPLACE ASSOCIATION """
        assert vql.count(expected_association) == 1

    def test_metrics_count(self):
        vql = self._convert_vql()
        expected_metrics = """   METRICS (
        total_revenue AS SUM(orders.amount),
        customer_count AS COUNT(DISTINCT customers.id)
    );"""
        assert expected_metrics in vql

# ---------------------------------------------------------------------------
# Tests for example_osi_yaml_complex_fields.yaml fixture
# ---------------------------------------------------------------------------

class TestExampleOsiYamlComplexFieldsFixture:
    """Tests for complex field expressions (functions) in DIMENSIONS.

    Verifies that:
    - Simple field expressions are prefixed with the dataset alias.
    - Complex expressions (functions, concatenations) are preserved as-is.
    - The ANSI_SQL dialect is preferred over DENODO when both are present.
    """

    FIXTURE = FIXTURES / "example_osi_yaml_complex_fields.yaml"

    def _convert_vql(self):
        ossie_yaml = self.FIXTURE.read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        return vql

    def test_metric_view_name(self):
        vql = self._convert_vql()
        expected_name = """CREATE OR REPLACE METRIC VIEW ecommerce_analytics
    DESCRIPTION = 'E-commerce sales and customer analytics'"""
        assert expected_name in vql

    def test_sources_count(self):
        vql = self._convert_vql()
        expected_sources = """    SOURCES (
        orders AS sales.orders,
        customers AS sales.customers
    )"""
        assert expected_sources in vql


    def test_simple_dimensions_name(self):
        vql = self._convert_vql()
        expected_dimensions = """    DIMENSIONS (
        orders_dim(
            order_id AS orders.order_id,
            customer_id AS orders.customer_id,
            order_date AS orders.order_date,
            order_date_year AS year(orders.order_date),
            amount AS orders.amount
        ),
        customers_dim(
            id AS customers.id,
            customer_full_name AS concat(customers.customer_lastname,",",customers.customer_name),
            email AS customers.email
        )
    )"""
        assert expected_dimensions in vql

    # --- Metrics ---

    def test_metrics(self):
        vql = self._convert_vql()
        expected_metrics = """    METRICS (
        total_revenue AS SUM(orders.amount),
        customer_count AS COUNT(DISTINCT customers.id)
    );"""
        assert expected_metrics in vql

class TestExampleOsiYamlMultipleDialectsFixture:
    """Tests for example_osi_yaml_multiple_dialects.yaml — dialect precedence rules:
    1. ANSI_SQL is preferred when available.
    2. If ANSI_SQL is not defined, the first available dialect is used.
    """

    FIXTURE = FIXTURES / "example_osi_yaml_multiple_dialects.yaml"

    def _convert_vql(self):
        import pathlib
        from ossie_denodo.osi_to_vql import convert_ossie_to_vql
        text = pathlib.Path(self.FIXTURE).read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(text)
        return vql

    # --- ANSI_SQL preferred ---

    def test_order_id_uses_ansi_sql(self):
        """order_id has only ANSI_SQL → simple field, prefixed with alias."""
        vql = self._convert_vql()
        expected = """orders_dim(
            order_id AS orders.order_id,"""
        assert expected in vql

    def test_customer_id_uses_ansi_sql(self):
        """customer_id has only ANSI_SQL → simple field, prefixed with alias."""
        vql = self._convert_vql()
        expected = """ customer_id AS orders.customer_id,"""
        assert expected in vql

    def test_order_date_uses_ansi_sql(self):
        """order_date has only ANSI_SQL → simple field, prefixed with alias."""
        vql = self._convert_vql()
        expected = """order_date AS orders.order_date,"""
        assert expected in vql

    def test_amount_uses_ansi_sql(self):
        """amount has only ANSI_SQL → simple field, prefixed with alias."""
        vql = self._convert_vql()
        expected = """amount AS orders.amount"""
        assert expected in vql

    def test_customer_full_name_uses_ansi_sql_over_databricks(self):
        """customer_full_name has DATABRICKS first, then ANSI_SQL → ANSI_SQL wins."""
        vql = self._convert_vql()
        expected = """customer_full_name AS customers.customer_lastname || ","|| customers.customer_name,"""
        assert expected in vql

    # --- First available dialect when ANSI_SQL is absent ---

    def test_order_date_year_uses_first_available_dialect(self):
        """order_date_year has SNOWFLAKE and DATABRICKS but no ANSI_SQL → first (SNOWFLAKE) is used."""
        vql = self._convert_vql()
        expected = """ order_date_year AS year(orders.order_date),"""
        assert expected in vql

    # --- Metrics (all have ANSI_SQL) ---

    def test_total_revenue_uses_ansi_sql(self):
        vql = self._convert_vql()
        expected = """total_revenue AS SUM(orders.amount),"""
        assert expected in vql

    def test_customer_count_uses_ansi_sql(self):
        vql = self._convert_vql()
        expected = """customer_count AS COUNT(DISTINCT customers.id)"""
        assert expected in vql


# ---------------------------------------------------------------------------
# Unit tests: _is_simple_unprefixed
# ---------------------------------------------------------------------------

class TestIsSimpleUnprefixed:
    """Tests for the _is_simple_unprefixed helper."""

    def test_bare_column_name_is_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("n_name") is True

    def test_prefixed_column_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("nation.n_name") is False

    def test_function_call_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("year(order_date)") is False

    def test_arithmetic_with_plus_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("(o_orderkey+5)") is False

    def test_arithmetic_with_minus_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("(o_orderkey-1)") is False

    def test_concat_function_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("concat(customer.c_address, '-', nation.n_name)") is False

    def test_pipe_concat_is_not_simple(self):
        from ossie_denodo.osi_to_vql import _is_simple_unprefixed
        assert _is_simple_unprefixed("first_name || ' ' || last_name") is False


# ---------------------------------------------------------------------------
# Integration tests: example6.yaml — unprefixed expressions use source as prefix
# ---------------------------------------------------------------------------

class TestExample6UnprefixedExpressions:
    """Tests for example6.yaml: bare column expressions must be prefixed with the
    dataset alias."""

    FIXTURE = FIXTURES / "example6.yaml"

    def _convert_vql(self):
        import pathlib
        from ossie_denodo.osi_to_vql import convert_ossie_to_vql
        text = pathlib.Path(self.FIXTURE).read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(text)
        return vql

    def test_associations_alias(self):
        vql = self._convert_vql()
        expected = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_psql_tpch_customer REFERENTIAL CONSTRAINT
FOLDER = '/associations'
    ENDPOINT psql_tpch_customer psql_tpch_nation PRINCIPAL (1)
    ENDPOINT psql_tpch_nation psql_tpch_customer (0,*)
    ADD MAPPING c_nationkey=n_nationkey;

CREATE OR REPLACE ASSOCIATION psql_tpch_customer_psql_tpch_orders REFERENTIAL CONSTRAINT
FOLDER = '/associations'
    ENDPOINT psql_tpch_orders psql_tpch_customer PRINCIPAL (1)
    ENDPOINT psql_tpch_customer psql_tpch_orders (0,*)
    ADD MAPPING o_custkey=c_custkey;
"""
        assert expected in vql


    def test_nation_name_prefixed_with_source(self):
        """nation_name expression 'n_name' → 'psql_tpch_nation.n_name'."""
        vql = self._convert_vql()
        expected = """ nation_name AS nation.n_name"""
        assert expected in vql

    def test_order_dim_prefixed_with_source(self):
        vql = self._convert_vql()
        expected = """        orders_dim(
            order_date AS orders.o_orderdate,
            order_dim_extra AS (orders.o_orderkey+5)
        ),"""
        assert expected in vql

    def test_customer_dim(self):
        vql = self._convert_vql()
        expected = """        customer_dim(
            customer_address_full AS concat(customer.c_address, '-', nation.n_name),
            customer_name AS customer.c_name,
            customer_address AS customer.c_address,
            customer_comment AS customer.c_comment,
            generic_attribute AS (customer.o_orderkey-1)
        ),"""
        assert expected in vql

    def test_metrics(self):
        vql = self._convert_vql()
        expected = """    METRICS (
        count_order_keys AS count(orders.o_orderkey),
        order_average_value AS avg(orders.o_totalprice)
    )"""
        assert expected in vql

# ---------------------------------------------------------------------------
# Tests for DENODO custom_extension with function mappings (OSI → VQL)
# ---------------------------------------------------------------------------

class TestMappingFunctionCustomExtensionOsiToVql:
    """Tests for OSI → VQL conversion when a relationship has a DENODO
    custom_extension with function mappings."""

    def _ossie_with_function_mapping(self, from_funcs=None, to_funcs=None):
        import json
        data = {}
        if from_funcs:
            data["from_columns"] = from_funcs
        if to_funcs:
            data["to_columns"] = to_funcs
        return yaml.dump({
            "version": "0.2.0.dev0",
            "name": "test_view",
            "datasets": [
                {"name": "sales", "source": "dw_fact_sales", "fields": []},
                {"name": "customers", "source": "dw_dim_customers", "fields": []},
            ],
            "relationships": [
                {
                    "name": "dw_dim_customers_dw_fact_sales",
                    "from": "sales",
                    "to": "customers",
                    "from_columns": ["customer_key"],
                    "to_columns": ["customer_key"],
                    "custom_extensions": [
                        {
                            "vendor_name": "DENODO",
                            "data": json.dumps(data),
                        }
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "total",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "count(sales.id)"}]
                    },
                }
            ],
        }, default_flow_style=False, sort_keys=False)

    def test_from_function_generates_add_mapping_with_function(self):
        """When from_columns has a function mapping, ADD MAPPING must use the function."""
        ossie_yaml = self._ossie_with_function_mapping(
            from_funcs={"customer_key": "round(customer_key)"}
        )
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "ADD MAPPING round(customer_key)=customer_key" in vql

    def test_no_function_generates_plain_add_mapping(self):
        """When no custom_extension is present, ADD MAPPING uses plain column names."""
        ossie_yaml = yaml.dump({
            "version": "0.2.0.dev0",
            "name": "test_view",
            "datasets": [
                {"name": "sales", "source": "dw_fact_sales", "fields": []},
                {"name": "customers", "source": "dw_dim_customers", "fields": []},
            ],
            "relationships": [
                {
                    "name": "dw_dim_customers_dw_fact_sales",
                    "from": "sales",
                    "to": "customers",
                    "from_columns": ["customer_key"],
                    "to_columns": ["customer_key"],
                }
            ],
            "metrics": [
                {
                    "name": "total",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "count(sales.id)"}]
                    },
                }
            ],
        }, default_flow_style=False, sort_keys=False)
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "ADD MAPPING customer_key=customer_key" in vql

    def test_to_function_generates_add_mapping_with_function_on_right(self):
        """When to_columns has a function mapping, ADD MAPPING must use the function on the right."""
        ossie_yaml = self._ossie_with_function_mapping(
            to_funcs={"customer_key": "round(customer_key)"}
        )
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "ADD MAPPING customer_key=round(customer_key)" in vql

    def test_example7_yaml_fixture_generates_function_mapping(self):
        """The example7.yaml fixture (with custom_extension) must generate round(customer_key)=customer_key."""
        ossie_yaml = (FIXTURES / "example7.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "ADD MAPPING round(customer_key)=customer_key" in vql

    def test_example7_yaml_fixture_plain_mappings_unchanged(self):
        """Other mappings without functions must remain plain."""
        ossie_yaml = (FIXTURES / "example7.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "ADD MAPPING product_key=product_key" in vql
        assert "ADD MAPPING date_key=date_key" in vql

    def test_example7_yaml_fixture_referential_constraint_generated(self):
        """All associations generated from YAML must include REFERENTIAL CONSTRAINT."""
        ossie_yaml = (FIXTURES / "example7.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "REFERENTIAL CONSTRAINT" in vql

    def test_example7_yaml_fixture_principal_cardinality(self):
        """The PRINCIPAL endpoint must use real source view names."""
        ossie_yaml = (FIXTURES / "example7.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        # example7.vql has (1) on both endpoints → round-trip preserves that
        assert "ENDPOINT dw_fact_sales dw_dim_customers PRINCIPAL (1)" in vql

class TestExample9CustomExtensionRelationCardinality:

    FIXTURE = FIXTURES / "example9.yaml"

    def _convert_vql(self):
        import pathlib
        from ossie_denodo.osi_to_vql import convert_ossie_to_vql
        text = pathlib.Path(self.FIXTURE).read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(text)
        return vql

    def test_cardinality_with_plus(self):
        vql = self._convert_vql()
        expected_a = """CREATE OR REPLACE ASSOCIATION psql_tpch_region_nation REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_nation psql_tpch_region PRINCIPAL (1)
    ENDPOINT psql_tpch_region psql_tpch_nation (+)
    ADD MAPPING r_regionkey=n_regionkey;"""
        assert expected_a in vql

        expected_b = """CREATE OR REPLACE ASSOCIATION psql_tpch_orders_lineitem REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_lineitem psql_tpch_orders PRINCIPAL (1)
    ENDPOINT psql_tpch_orders psql_tpch_lineitem (+)
    ADD MAPPING o_orderkey=l_orderkey;
"""
        assert expected_b in vql

        expected_c = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_supplier REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_nation psql_tpch_supplier PRINCIPAL (1)
    ENDPOINT psql_tpch_supplier psql_tpch_nation (+)
    ADD MAPPING s_nationkey=n_nationkey;
"""
        assert expected_c in vql

    def test_cardinality_with_one(self):
        vql = self._convert_vql()
        expected = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_customer REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_customer psql_tpch_nation PRINCIPAL (1)
    ENDPOINT psql_tpch_nation psql_tpch_customer (1)
    ADD MAPPING n_nationkey=c_nationkey;"""
        assert expected in vql

    def test_cardinality_with_one_zero(self):
        vql = self._convert_vql()
        expected = """CREATE OR REPLACE ASSOCIATION psql_tpch_customer_orders REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_orders psql_tpch_customer PRINCIPAL (1)
    ENDPOINT psql_tpch_customer psql_tpch_orders (0,1)
    ADD MAPPING c_custkey=o_custkey;"""
        assert expected in vql

class TestExample9CustomExtensionReferentialConstraint:

    FIXTURE = FIXTURES / "example10.yaml"

    def _convert_vql(self):
        import pathlib
        from ossie_denodo.osi_to_vql import convert_ossie_to_vql
        text = pathlib.Path(self.FIXTURE).read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(text)
        return vql

    def test_cardinality_with_referential_constraint(self):
        vql = self._convert_vql()
        expected_a = """CREATE OR REPLACE ASSOCIATION psql_tpch_region_nation REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_nation psql_tpch_region PRINCIPAL (1)
    ENDPOINT psql_tpch_region psql_tpch_nation (+)
    ADD MAPPING r_regionkey=n_regionkey;"""
        assert expected_a in vql

        expected_b = """CREATE OR REPLACE ASSOCIATION psql_tpch_customer_orders REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_orders psql_tpch_customer PRINCIPAL (1)
    ENDPOINT psql_tpch_customer psql_tpch_orders (+)
    ADD MAPPING c_custkey=o_custkey;"""
        assert expected_b in vql

        expected_c = """CREATE OR REPLACE ASSOCIATION psql_tpch_orders_lineitem REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_lineitem psql_tpch_orders PRINCIPAL (1)
    ENDPOINT psql_tpch_orders psql_tpch_lineitem (+)
    ADD MAPPING o_orderkey=l_orderkey;
"""
        assert expected_c in vql

        expected_d = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_supplier REFERENTIAL CONSTRAINT
    ENDPOINT psql_tpch_nation psql_tpch_supplier PRINCIPAL (1)
    ENDPOINT psql_tpch_supplier psql_tpch_nation (+)
    ADD MAPPING s_nationkey=n_nationkey;
"""
        assert expected_d in vql

    def test_cardinality_without_referential_constraint(self):
        vql = self._convert_vql()
        expected = """CREATE OR REPLACE ASSOCIATION psql_tpch_nation_customer
    ENDPOINT psql_tpch_nation psql_tpch_customer PRINCIPAL (1)
    ENDPOINT psql_tpch_customer psql_tpch_nation (1)
    ADD MAPPING c_nationkey=n_nationkey;"""
        assert expected in vql

class TestExample11AssociationDiffVDB:

    def test_relationship_exist_command(self):
        ossie_yaml = (FIXTURES / "example11.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "CONNECT DATABASE vdb_tpch" in vql

    def test_relationship_n_command(self):
        ossie_yaml = (FIXTURES / "example11.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert vql.count("CONNECT DATABASE vdb_tpch") == 3

    def test_relationship_n_closes(self):
        ossie_yaml = (FIXTURES / "example11.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert vql.count("CLOSE") == 3

class TestExample12GroupDimension:

    def test_group_dimension(self):
        ossie_yaml = (FIXTURES / "example12.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "dim_customer(" in vql
        assert "dim_nation(" in vql

    def test_group_dimension_b(self):
        ossie_yaml = (FIXTURES / "example12b.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        expected_dim = """dim_customer_data(
            nation_name AS nation.n_name,
            customer_name AS customer.c_name
        ),"""
        assert expected_dim in vql
        expected_dim_b = """dim_customer(
            customer_address_full AS concat(customer.c_address, '-', nation.n_name)
        ),"""
        assert expected_dim_b in vql

class TestExample13JoinType:

    def test_right_join(self):
        ossie_yaml = (FIXTURES / "example13.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "customer nation vdb_tpch.psql_tpch_nation_psql_tpch_customer RIGHT," in vql

    def test_left_join(self):
        ossie_yaml = (FIXTURES / "example13.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "orders customer vdb_tpch.psql_tpch_customer_psql_tpch_orders LEFT" in vql

    def test_default_join(self):
        ossie_yaml = (FIXTURES / "example13.yaml").read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)
        assert "nation region vdb_tpch.psql_tpch_region_psql_tpch_nation," in vql


class TestExampleTpcDsDatasetMetrics:
    """Test for the example-tpc-ds.yaml fixture to verify dataset-level metrics."""

    FIXTURE = pathlib.Path(__file__).parent / "tpc-ds-examples" / "example-tpc-ds.yaml"

    #To check the warning related to ai_context entries
    FIXTURE_A = pathlib.Path(__file__).parent / "tpc-ds-examples" /"ai-context-tests"/ "example-tpc-ds_A.yaml"
    FIXTURE_B = pathlib.Path(__file__).parent / "tpc-ds-examples" /"ai-context-tests"/ "example-tpc-ds_B.yaml"
    FIXTURE_C = pathlib.Path(__file__).parent / "tpc-ds-examples" /"ai-context-tests"/ "example-tpc-ds_C.yaml"

    def test_dataset_metrics_rendered(self):
        ossie_yaml = self.FIXTURE.read_text(encoding="utf-8")
        vql = convert_ossie_to_vql(ossie_yaml)

        # Verify dataset-level metric from 'store_sales' dataset
        # In example-tpc-ds.yaml:
        # metrics:
        #   - name: total_quantity
        #     expression:
        #       dialects:
        #         - dialect: ANSI_SQL
        #           expression: SUM(ss_quantity)
        assert "total_quantity AS SUM(store_sales.ss_quantity)" in vql or "total_quantity AS SUM(ss_quantity)" in vql

        # Verify model-level metrics are also present
        assert "total_sales AS SUM(store_sales.ss_ext_sales_price)" in vql
        assert "total_profit AS SUM(store_sales.ss_net_profit)" in vql

    def test_warning_ai_context(self):
        ossie_yaml = self.FIXTURE.read_text(encoding="utf-8")
        with pytest.warns(UserWarning, match="Denodo currently does not support 'ai_context' and it will be ignored"):
            convert_ossie_to_vql(ossie_yaml)

    def test_warning_ai_context_fields_dataset(self):
        ossie_yaml = self.FIXTURE_A.read_text(encoding="utf-8")
        with pytest.warns(UserWarning, match="Denodo currently does not support 'ai_context' and it will be ignored"):
            convert_ossie_to_vql(ossie_yaml)

    def test_warning_ai_context_fields_relationships(self):
        ossie_yaml = self.FIXTURE_B.read_text(encoding="utf-8")
        with pytest.warns(UserWarning, match="Denodo currently does not support 'ai_context' and it will be ignored"):
            convert_ossie_to_vql(ossie_yaml)

    def test_warning_ai_context_fields_metrics(self):
        ossie_yaml = self.FIXTURE_C.read_text(encoding="utf-8")
        with pytest.warns(UserWarning, match="Denodo currently does not support 'ai_context' and it will be ignored"):
            convert_ossie_to_vql(ossie_yaml)
