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

"""Tests for the OSI YAML → Denodo VQL converter using the TPC-DS example model.

The fixture under test is:
    tests/tpc-ds-examples/example-tpc-ds.yaml

This suite mirrors the structure and coverage of test_osi_to_vql.py but is
adapted to the TPC-DS retail semantic model (tpcds_retail_model) instead of
the TPC-H model used in the fixtures/ directory.
"""

from __future__ import annotations

import pathlib
import warnings

import pytest

from ossie_denodo.osi_to_vql import convert_ossie_to_vql

FIXTURE = pathlib.Path(__file__).parent / "tpc-ds-examples" / "example-tpc-ds.yaml"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _convert_vql() -> str:
    ossie_yaml = FIXTURE.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return convert_ossie_to_vql(ossie_yaml)


# ---------------------------------------------------------------------------
# Metric view header
# ---------------------------------------------------------------------------

class TestTpcDsMetricViewHeader:
    """The generated VQL must open with the correct metric view name and description."""

    def test_metric_view_name(self):
        vql = _convert_vql()
        assert "CREATE OR REPLACE METRIC VIEW tpcds_retail_model" in vql

    def test_metric_view_description(self):
        vql = _convert_vql()
        assert "DESCRIPTION = 'TPC-DS retail semantic model for sales and customer analytics'" in vql

    def test_metric_view_name_and_description_together(self):
        vql = _convert_vql()
        expected = (
            "CREATE OR REPLACE METRIC VIEW tpcds_retail_model\n"
            "    DESCRIPTION = 'TPC-DS retail semantic model for sales and customer analytics'"
        )
        assert expected in vql


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class TestTpcDsSources:
    """All five TPC-DS datasets must appear as SOURCES with db-prefixed view names.

    The source strings in the YAML use the 3-part form tpcds.public.X; the
    converter strips the schema segment and emits tpcds.X.
    """

    def test_sources_block_present(self):
        vql = _convert_vql()
        assert "SOURCES (" in vql

    def test_all_five_sources_present(self):
        vql = _convert_vql()
        expected = """    SOURCES (
        store_sales AS tpcds.store_sales,
        date_dim AS tpcds.date_dim,
        customer AS tpcds.customer,
        item AS tpcds.item,
        store AS tpcds.store
    )"""
        assert expected in vql

    def test_schema_warning_emitted_for_each_source(self):
        """A UserWarning must be emitted for every source that has a schema segment."""
        ossie_yaml = FIXTURE.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            convert_ossie_to_vql(ossie_yaml)
        schema_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning) and "schema" in str(w.message).lower()
        ]
        # Five datasets, each with tpcds.public.X → five schema warnings
        assert len(schema_warnings) >= 5

    def test_store_sales_source(self):
        vql = _convert_vql()
        assert "store_sales AS tpcds.store_sales" in vql

    def test_date_dim_source(self):
        vql = _convert_vql()
        assert "date_dim AS tpcds.date_dim" in vql

    def test_customer_source(self):
        vql = _convert_vql()
        assert "customer AS tpcds.customer" in vql

    def test_item_source(self):
        vql = _convert_vql()
        assert "item AS tpcds.item" in vql

    def test_store_source(self):
        vql = _convert_vql()
        assert "store AS tpcds.store" in vql


# ---------------------------------------------------------------------------
# Associations (CREATE OR REPLACE ASSOCIATION …)
# ---------------------------------------------------------------------------

class TestTpcDsAssociationStatements:
    """Each relationship in the YAML must produce a standalone ASSOCIATION statement."""

    def test_four_associations_generated(self):
        vql = _convert_vql()
        assert vql.count("CREATE OR REPLACE ASSOCIATION") == 4

    def test_store_sales_to_date_association(self):
        vql = _convert_vql()
        expected = (
            "CREATE OR REPLACE ASSOCIATION store_sales_to_date REFERENTIAL CONSTRAINT\n"
            "    ENDPOINT store_sales date_dim PRINCIPAL (1)\n"
            "    ENDPOINT date_dim store_sales (0,*)\n"
            "    ADD MAPPING ss_sold_date_sk=d_date_sk;"
        )
        assert expected in vql

    def test_store_sales_to_customer_association(self):
        vql = _convert_vql()
        expected = (
            "CREATE OR REPLACE ASSOCIATION store_sales_to_customer REFERENTIAL CONSTRAINT\n"
            "    ENDPOINT store_sales customer PRINCIPAL (1)\n"
            "    ENDPOINT customer store_sales (0,*)\n"
            "    ADD MAPPING ss_customer_sk=c_customer_sk;"
        )
        assert expected in vql

    def test_store_sales_to_item_association(self):
        vql = _convert_vql()
        expected = (
            "CREATE OR REPLACE ASSOCIATION store_sales_to_item REFERENTIAL CONSTRAINT\n"
            "    ENDPOINT store_sales item PRINCIPAL (1)\n"
            "    ENDPOINT item store_sales (0,*)\n"
            "    ADD MAPPING ss_item_sk=i_item_sk;"
        )
        assert expected in vql

    def test_store_sales_to_store_association(self):
        vql = _convert_vql()
        expected = (
            "CREATE OR REPLACE ASSOCIATION store_sales_to_store REFERENTIAL CONSTRAINT\n"
            "    ENDPOINT store_sales store PRINCIPAL (1)\n"
            "    ENDPOINT store store_sales (0,*)\n"
            "    ADD MAPPING ss_store_sk=s_store_sk;"
        )
        assert expected in vql

    def test_all_associations_have_referential_constraint(self):
        vql = _convert_vql()
        assert vql.count("REFERENTIAL CONSTRAINT") == 4

    def test_principal_endpoint_cardinality_is_one(self):
        """Every PRINCIPAL endpoint must carry cardinality (1)."""
        vql = _convert_vql()
        assert vql.count("PRINCIPAL (1)") == 4

    def test_many_side_cardinality(self):
        """The many-side endpoint must carry cardinality (0,*)."""
        vql = _convert_vql()
        assert vql.count("(0,*)") == 4


# ---------------------------------------------------------------------------
# ASSOCIATIONS block inside the metric view
# ---------------------------------------------------------------------------

class TestTpcDsAssociationsBlock:
    """The ASSOCIATIONS block inside the metric view must reference all four relationships."""

    def test_associations_block_present(self):
        vql = _convert_vql()
        assert "ASSOCIATIONS (" in vql

    def test_associations_block_content(self):
        vql = _convert_vql()
        expected = """    ASSOCIATIONS (
        store_sales date_dim store_sales_to_date,
        store_sales customer store_sales_to_customer,
        store_sales item store_sales_to_item,
        store_sales store store_sales_to_store
    )"""
        assert expected in vql


# ---------------------------------------------------------------------------
# DIMENSIONS
# ---------------------------------------------------------------------------

class TestTpcDsDimensions:
    """All dataset fields must appear as DIMENSIONS prefixed with the dataset alias."""

    def test_dimensions_block_present(self):
        vql = _convert_vql()
        assert "DIMENSIONS (" in vql

    def test_store_sales_dimensions(self):
        vql = _convert_vql()
        assert "ss_sold_date_sk AS store_sales.ss_sold_date_sk" in vql
        assert "ss_item_sk AS store_sales.ss_item_sk" in vql
        assert "ss_ticket_number AS store_sales.ss_ticket_number" in vql
        assert "ss_customer_sk AS store_sales.ss_customer_sk" in vql
        assert "ss_store_sk AS store_sales.ss_store_sk" in vql
        assert "ss_quantity AS store_sales.ss_quantity" in vql
        assert "ss_sales_price AS store_sales.ss_sales_price" in vql
        assert "ss_ext_sales_price AS store_sales.ss_ext_sales_price" in vql
        assert "ss_net_profit AS store_sales.ss_net_profit" in vql

    def test_date_dim_dimensions(self):
        vql = _convert_vql()
        assert "d_date_sk AS date_dim.d_date_sk" in vql
        assert "d_date AS date_dim.d_date" in vql
        assert "d_year AS date_dim.d_year" in vql
        assert "d_quarter_name AS date_dim.d_quarter_name" in vql
        assert "d_moy AS date_dim.d_moy" in vql

    def test_customer_dimensions(self):
        vql = _convert_vql()
        assert "c_customer_sk AS customer.c_customer_sk" in vql
        assert "c_customer_id AS customer.c_customer_id" in vql
        assert "c_first_name AS customer.c_first_name" in vql
        assert "c_last_name AS customer.c_last_name" in vql
        assert "c_email_address AS customer.c_email_address" in vql

    def test_customer_full_name_computed_dimension(self):
        """customer_full_name is a computed expression (concatenation) and must be preserved as-is."""
        vql = _convert_vql()
        assert "customer_full_name AS customer.c_first_name || ' ' || customer.c_last_name" in vql

    def test_item_dimensions(self):
        vql = _convert_vql()
        assert "i_item_sk AS item.i_item_sk" in vql
        assert "i_item_id AS item.i_item_id" in vql
        assert "i_item_desc AS item.i_item_desc" in vql
        assert "i_brand AS item.i_brand" in vql
        assert "i_category AS item.i_category" in vql
        assert "i_current_price AS item.i_current_price" in vql

    def test_store_dimensions(self):
        vql = _convert_vql()
        assert "s_store_sk AS store.s_store_sk" in vql
        assert "s_store_id AS store.s_store_id" in vql
        assert "s_store_name AS store.s_store_name" in vql
        assert "s_city AS store.s_city" in vql
        assert "s_state AS store.s_state" in vql
        assert "s_number_employees AS store.s_number_employees" in vql

    def test_full_dimensions_block(self):
        vql = _convert_vql()
        expected = """    DIMENSIONS (
        ss_sold_date_sk AS store_sales.ss_sold_date_sk,
        ss_item_sk AS store_sales.ss_item_sk,
        ss_ticket_number AS store_sales.ss_ticket_number,
        ss_customer_sk AS store_sales.ss_customer_sk,
        ss_store_sk AS store_sales.ss_store_sk,
        ss_quantity AS store_sales.ss_quantity,
        ss_sales_price AS store_sales.ss_sales_price,
        ss_ext_sales_price AS store_sales.ss_ext_sales_price,
        ss_net_profit AS store_sales.ss_net_profit,
        d_date_sk AS date_dim.d_date_sk,
        d_date AS date_dim.d_date,
        d_year AS date_dim.d_year,
        d_quarter_name AS date_dim.d_quarter_name,
        d_moy AS date_dim.d_moy,
        c_customer_sk AS customer.c_customer_sk,
        c_customer_id AS customer.c_customer_id,
        c_first_name AS customer.c_first_name,
        c_last_name AS customer.c_last_name,
        customer_full_name AS customer.c_first_name || ' ' || customer.c_last_name,
        c_email_address AS customer.c_email_address,
        i_item_sk AS item.i_item_sk,
        i_item_id AS item.i_item_id,
        i_item_desc AS item.i_item_desc,
        i_brand AS item.i_brand,
        i_category AS item.i_category,
        i_current_price AS item.i_current_price,
        s_store_sk AS store.s_store_sk,
        s_store_id AS store.s_store_id,
        s_store_name AS store.s_store_name,
        s_city AS store.s_city,
        s_state AS store.s_state,
        s_number_employees AS store.s_number_employees
    )"""
        assert expected in vql


# ---------------------------------------------------------------------------
# METRICS
# ---------------------------------------------------------------------------

class TestTpcDsMetrics:
    """All model-level and dataset-level metrics must appear in the METRICS block."""

    def test_metrics_block_present(self):
        vql = _convert_vql()
        assert "METRICS (" in vql

    def test_total_sales_metric(self):
        vql = _convert_vql()
        assert "total_sales AS SUM(store_sales.ss_ext_sales_price)" in vql

    def test_total_profit_metric(self):
        vql = _convert_vql()
        assert "total_profit AS SUM(store_sales.ss_net_profit)" in vql

    def test_customer_lifetime_value_metric(self):
        vql = _convert_vql()
        assert (
            "customer_lifetime_value AS SUM(store_sales.ss_ext_sales_price)"
            " / COUNT(DISTINCT customer.c_customer_sk)"
        ) in vql

    def test_sales_by_brand_metric(self):
        vql = _convert_vql()
        assert "sales_by_brand AS SUM(store_sales.ss_ext_sales_price)" in vql

    def test_store_productivity_metric(self):
        vql = _convert_vql()
        assert (
            "store_productivity AS SUM(store_sales.ss_ext_sales_price)"
            " / NULLIF(SUM(store.s_number_employees), 0)"
        ) in vql

    def test_cumulative_sales_window_metric(self):
        vql = _convert_vql()
        assert (
            "cumulative_sales AS SUM(SUM(store_sales.ss_ext_sales_price))"
            " OVER (ORDER BY date_dim.d_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
        ) in vql

    def test_brand_rank_in_store_window_metric(self):
        vql = _convert_vql()
        assert (
            "brand_rank_in_store AS RANK() OVER"
            " (PARTITION BY store.s_store_sk ORDER BY SUM(store_sales.ss_ext_sales_price) DESC)"
        ) in vql

    def test_monthly_sales_change_window_metric(self):
        vql = _convert_vql()
        assert (
            "monthly_sales_change AS SUM(store_sales.ss_ext_sales_price)"
            " - LAG(SUM(store_sales.ss_ext_sales_price), 1)"
            " OVER (ORDER BY date_dim.d_year, date_dim.d_moy)"
        ) in vql

    def test_dataset_level_metric_total_quantity(self):
        """total_quantity is declared at dataset level inside store_sales and must be promoted
        to the model-level METRICS block, qualified with the dataset alias."""
        vql = _convert_vql()
        assert "total_quantity AS SUM(store_sales.ss_quantity)" in vql

    def test_full_metrics_block(self):
        vql = _convert_vql()
        expected = """    METRICS (
        total_sales AS SUM(store_sales.ss_ext_sales_price),
        total_profit AS SUM(store_sales.ss_net_profit),
        customer_lifetime_value AS SUM(store_sales.ss_ext_sales_price) / COUNT(DISTINCT customer.c_customer_sk),
        sales_by_brand AS SUM(store_sales.ss_ext_sales_price),
        store_productivity AS SUM(store_sales.ss_ext_sales_price) / NULLIF(SUM(store.s_number_employees), 0),
        cumulative_sales AS SUM(SUM(store_sales.ss_ext_sales_price)) OVER (ORDER BY date_dim.d_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        brand_rank_in_store AS RANK() OVER (PARTITION BY store.s_store_sk ORDER BY SUM(store_sales.ss_ext_sales_price) DESC),
        monthly_sales_change AS SUM(store_sales.ss_ext_sales_price) - LAG(SUM(store_sales.ss_ext_sales_price), 1) OVER (ORDER BY date_dim.d_year, date_dim.d_moy),
        total_quantity AS SUM(store_sales.ss_quantity)
    );"""
        assert expected in vql


# ---------------------------------------------------------------------------
# ai_context warnings
# ---------------------------------------------------------------------------

class TestTpcDsAiContextWarnings:
    """The converter must emit a UserWarning for every ai_context entry it encounters."""

    def test_warning_emitted_for_model_level_ai_context(self):
        ossie_yaml = FIXTURE.read_text(encoding="utf-8")
        with pytest.warns(UserWarning, match="Denodo currently does not support 'ai_context' and it will be ignored"):
            convert_ossie_to_vql(ossie_yaml)

    def test_multiple_ai_context_warnings_emitted(self):
        """The YAML has ai_context at model, dataset, field, relationship and metric level;
        at least one warning must be raised per conversion."""
        ossie_yaml = FIXTURE.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            convert_ossie_to_vql(ossie_yaml)
        ai_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning) and "ai_context" in str(w.message).lower()
        ]
        assert len(ai_warnings) >= 1
