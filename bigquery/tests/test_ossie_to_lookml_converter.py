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

"""Tests for the Ossie to LookML converter."""

import re
import warnings
from pathlib import Path

import lkml
import pytest
import yaml

from ossie_bigquery.ossie_to_lookml import (
    OssieConversionError,
    convert_ossie_to_lookml,
    _convert_source,
    _entity_description,
    _extract_expression,
    _strip_time_suffix,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TPCDS = Path(__file__).resolve().parents[2] / "ossie" / "examples" / "tpcds_semantic_model.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ansi(expr, dialect="ANSI_SQL"):
    return {"dialects": [{"dialect": dialect, "expression": expr}]}


def _field(name, expr=None, is_time=None, datatype=None, label=None, description=None, dialect="ANSI_SQL"):
    field = {"name": name, "expression": _ansi(expr or name, dialect)}
    if is_time is not None:
        field["dimension"] = {"is_time": is_time}
    if datatype is not None:
        field["datatype"] = datatype
    if label is not None:
        field["label"] = label
    if description is not None:
        field["description"] = description
    return field


def _dataset(name, fields, source=None, primary_key=None, metrics=None):
    dataset = {"name": name, "source": source or f"proj.sch.{name}", "fields": fields}
    if primary_key is not None:
        dataset["primary_key"] = primary_key
    if metrics is not None:
        dataset["metrics"] = metrics
    return dataset


def _rel(name, from_ds, to_ds, from_cols, to_cols):
    return {
        "name": name,
        "from": from_ds,
        "to": to_ds,
        "from_columns": from_cols,
        "to_columns": to_cols,
    }


def _metric(name, expr, dialect="ANSI_SQL", **extra):
    return {"name": name, "expression": _ansi(expr, dialect), **extra}


def _model(datasets, relationships=None, metrics=None, **extra):
    model = {"version": "0.2.0.dev0", "name": "test_model", "datasets": datasets}
    if relationships is not None:
        model["relationships"] = relationships
    if metrics is not None:
        model["metrics"] = metrics
    model.update(extra)
    return yaml.dump(model, default_flow_style=False)


def _minimal_model(**overrides):
    base = {
        "name": "test_model",
        "datasets": [_dataset("my_table", [_field("col1", is_time=False)])],
    }
    base.update(overrides)
    return base


def _wrap_ossie(model_dict):
    return yaml.dump({"version": "0.2.0.dev0", **model_dict}, default_flow_style=False)


def _convert(yaml_str):
    """Converts, swallowing warnings (they are asserted on separately)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return convert_ossie_to_lookml(yaml_str)


def _block(out, header):
    """Returns the text of the `<header> {` ... `}` block at its own indent."""
    match = re.search(rf"^( *){re.escape(header)} \{{\n(.*?)^\1\}}", out, re.S | re.M)
    assert match, f"no block '{header}' in:\n{out}"
    return match.group(2)


ORDERS = _dataset(
    "orders",
    [
        _field("order_id", datatype="Integer"),
        _field("customer_id", datatype="Integer"),
        _field("amount", datatype="Decimal"),
    ],
    primary_key=["order_id"],
)
CUSTOMERS = _dataset(
    "customers",
    [_field("id", datatype="Integer"), _field("region_id", datatype="Integer")],
    primary_key=["id"],
)
REGIONS = _dataset("regions", [_field("id", datatype="Integer")], primary_key=["id"])


# ---------------------------------------------------------------------------
# Version / envelope handling
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_rejects_wrong_version(self):
        bad = yaml.dump({"version": "0.0.0", **_minimal_model()})
        with pytest.raises(OssieConversionError, match="Unsupported Ossie"):
            convert_ossie_to_lookml(bad)

    def test_rejects_non_mapping_root(self):
        with pytest.raises(OssieConversionError, match="expected a mapping"):
            convert_ossie_to_lookml("- just\n- a\n- list\n")

    def test_rejects_legacy_semantic_model_wrapper(self):
        bad = yaml.dump({"version": "0.2.0.dev0", "semantic_model": [_minimal_model()]})
        with pytest.raises(OssieConversionError, match="Legacy 'semantic_model'"):
            convert_ossie_to_lookml(bad)

    def test_rejects_root_dialects(self):
        bad = yaml.dump({"version": "0.2.0.dev0", "dialects": ["ANSI_SQL"], **_minimal_model()})
        with pytest.raises(OssieConversionError, match="Root dialects"):
            convert_ossie_to_lookml(bad)

    def test_version_not_reported_as_dropped(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            convert_ossie_to_lookml(_wrap_ossie(_minimal_model()))


# ---------------------------------------------------------------------------
# _convert_source
# ---------------------------------------------------------------------------

class TestConvertSource:
    def test_three_part_is_backtick_quoted(self):
        assert _convert_source("proj.dataset.tbl", "ds") == (
            "sql_table_name: `proj.dataset.tbl` ;;"
        )

    def test_quoted_three_part_is_not_wrapped_again(self):
        assert _convert_source("`my-proj.ds.tbl`", "ds") == (
            "sql_table_name: `my-proj.ds.tbl` ;;"
        )

    def test_mixed_quoting_is_emitted_as_is(self):
        assert _convert_source("`my-proj`.ds.tbl", "ds") == (
            "sql_table_name: `my-proj`.ds.tbl ;;"
        )

    def test_subquery_becomes_derived_table(self):
        out = _convert_source("SELECT * FROM t", "ds")
        assert out.startswith("derived_table: {")
        assert "sql: SELECT * FROM t ;;" in out

    def test_two_part_emitted_as_is(self):
        assert _convert_source("dataset.tbl", "ds") == "sql_table_name: dataset.tbl ;;"

    @pytest.mark.parametrize("source", [None, "", "   "])
    def test_missing_source_is_a_conversion_error(self, source):
        # Looker would fall back to the view name as the table, so a dataset
        # without a source must not convert.
        with pytest.raises(OssieConversionError, match="Dataset 'ds'.*required 'source'"):
            _convert_source(source, "ds")

    def test_dataset_without_source_fails_before_rendering(self):
        ds = {"name": "t", "fields": [_field("a")]}
        with pytest.raises(OssieConversionError, match="Dataset 't'.*required 'source'"):
            convert_ossie_to_lookml(_model([ds]))


# ---------------------------------------------------------------------------
# _extract_expression — dialect preference
# ---------------------------------------------------------------------------

class TestExtractExpression:
    def test_prefers_bigquery_over_ansi(self):
        expression = {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": "LOWER(email)"},
                {"dialect": "BIGQUERY", "expression": "SAFE_CAST(LOWER(email) AS STRING)"},
            ]
        }
        assert _extract_expression(expression, "f") == ("SAFE_CAST(LOWER(email) AS STRING)", "BIGQUERY")

    def test_falls_back_to_ansi(self):
        expression = {"dialects": [{"dialect": "ANSI_SQL", "expression": "x"}]}
        assert _extract_expression(expression, "f") == ("x", "ANSI_SQL")

    def test_falls_back_to_ossie_sql_after_ansi(self):
        expression = {
            "dialects": [
                {"dialect": "OSSIE_SQL_2026", "expression": "ossie_expr"},
                {"dialect": "SNOWFLAKE", "expression": "snow_expr"},
            ]
        }
        assert _extract_expression(expression, "f") == ("ossie_expr", "OSSIE_SQL_2026")

    def test_unsupported_only_warns_and_skips(self):
        expression = {"dialects": [{"dialect": "SNOWFLAKE", "expression": "x"}]}
        with pytest.warns(UserWarning, match="Skipping"):
            assert _extract_expression(expression, "f") == (None, None)

    def test_missing_expression_raises(self):
        with pytest.raises(OssieConversionError, match="Missing or malformed"):
            _extract_expression(None, "f")


# ---------------------------------------------------------------------------
# Fields -> dimensions / dimension_groups
# ---------------------------------------------------------------------------

class TestFields:
    def _view(self, *fields, **dataset_kwargs):
        out = _convert(_model([_dataset("t", list(fields), **dataset_kwargs)]))
        return _block(out, "view: t")

    def test_plain_field_becomes_string_dimension_when_untyped(self):
        view = self._view(_field("customer_id", is_time=False))
        dim = _block(view, "dimension: customer_id")
        assert "type: string" in dim
        assert "sql: ${TABLE}.customer_id ;;" in dim

    def test_time_field_becomes_dimension_group(self):
        view = self._view(_field("order_date", datatype="DateTimeTz"))
        group = _block(view, "dimension_group: order")
        assert "type: time" in group
        assert "timeframes: [raw, time, date, week, month, quarter, year]" in group
        assert "sql: ${TABLE}.order_date ;;" in group

    @pytest.mark.parametrize("name", ["d_moy", "d_quarter_name"])
    def test_is_time_without_datatype_is_a_string_dimension_with_warning(self, name):
        # is_time is a role, not a type (TPC-DS sets it on an integer month
        # and a '2024Q1' string); a dimension_group would cast them as TIMESTAMPs.
        model = _model([_dataset("t", [_field(name, is_time=True)])])
        with pytest.warns(UserWarning, match=f"Field 't.{name}'.*is_time without a datatype"):
            out = convert_ossie_to_lookml(model)
        assert "dimension_group:" not in out
        assert "type: string" in _block(out, f"dimension: {name}")

    @pytest.mark.parametrize("datatype", ["Date", "DateTime", "DateTimeTz"])
    def test_is_time_false_keeps_a_temporal_field_off_the_time_axis(self, datatype):
        # Explicit is_time always wins (Ossie spec): an audit `created_at` with
        # is_time: false is a regular dimension.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(
                _model([_dataset("t", [_field("created_at", is_time=False, datatype=datatype)])])
            )
        assert "dimension_group:" not in out
        dim = _block(out, "dimension: created_at")
        assert "type: string" in dim
        assert "sql: ${TABLE}.created_at ;;" in dim

    def test_is_time_true_on_a_temporal_field_is_a_dimension_group(self):
        view = self._view(_field("created_at", is_time=True, datatype="Date"))
        assert "datatype: date" in _block(view, "dimension_group: created_at")

    def test_strip_time_suffix(self):
        assert _strip_time_suffix("order_date") == "order"
        assert _strip_time_suffix("created_timestamp") == "created"
        assert _strip_time_suffix("revenue") == "revenue"

    def test_dimension_group_keeps_name_on_collision(self):
        # Both would strip to `ship`; LookML names must be unique, so keep the
        # original names.
        view = self._view(_field("ship_date", datatype="DateTimeTz"), _field("ship_time", datatype="DateTimeTz"))
        assert "dimension_group: ship_date {" in view
        assert "dimension_group: ship_time {" in view

    def test_dimension_group_keeps_name_when_it_shadows_a_field(self):
        view = self._view(_field("created"), _field("created_date", datatype="DateTimeTz"))
        assert "dimension: created {" in view
        assert "dimension_group: created_date {" in view

    def test_label_and_description_are_emitted(self):
        view = self._view(_field("id", label="Order ID", description="The order"))
        dim = _block(view, "dimension: id")
        assert 'label: "Order ID"' in dim
        assert 'description: "The order"' in dim

    def test_computed_expression_columns_are_qualified(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(
                _model([_dataset("t", [_field("email_norm", expr="LOWER(email)")])])
            )
        assert "sql: LOWER(${TABLE}.email) ;;" in out

    def test_bigquery_functions_survive_qualification(self):
        out = self._view(
            _field("month", expr="DATE_TRUNC(order_date, MONTH)", dialect="BIGQUERY"),
            _field("email", expr="SAFE_CAST(LOWER(email) AS STRING)", dialect="BIGQUERY"),
        )
        assert "sql: DATE_TRUNC(${TABLE}.order_date, MONTH) ;;" in out
        assert "sql: SAFE_CAST(LOWER(${TABLE}.email) AS STRING) ;;" in out

    def test_quoted_column_keeps_its_quotes(self):
        out = self._view(_field("oid", expr="`Order ID`", dialect="BIGQUERY"))
        assert "sql: ${TABLE}.`Order ID` ;;" in out

    def test_quoted_column_in_an_expression_keeps_its_quotes(self):
        out = self._view(_field("oid", expr="CAST(`Order ID` AS STRING)", dialect="BIGQUERY"))
        assert "sql: CAST(${TABLE}.`Order ID` AS STRING) ;;" in out

    @pytest.mark.parametrize("literal", ["NULL", "TRUE", "CURRENT_DATE"])
    def test_sql_literals_are_not_columns(self, literal):
        out = self._view(_field("lit", expr=literal))
        assert f"sql: {literal} ;;" in out

    def test_unparseable_expression_is_verbatim_with_warning(self):
        model = _model([_dataset("t", [_field("weird", expr="SELECT FROM WHERE ((")])])
        with pytest.warns(UserWarning, match="Field 't.weird'.*verbatim"):
            out = convert_ossie_to_lookml(model)
        assert "sql: SELECT FROM WHERE (( ;;" in out


class TestDatatypes:
    def _dim(self, **field_kwargs):
        out = _convert(_model([_dataset("t", [_field("f", **field_kwargs)])]))
        return _block(out, "view: t")

    @pytest.mark.parametrize(
        "datatype, lookml_type",
        [
            ("String", "string"),
            ("Integer", "number"),
            ("Decimal", "number"),
            ("Float", "number"),
            ("Boolean", "yesno"),
            ("Time", "string"),
        ],
    )
    def test_scalar_datatypes(self, datatype, lookml_type):
        view = self._dim(datatype=datatype)
        assert f"type: {lookml_type}" in _block(view, "dimension: f")

    @pytest.mark.parametrize(
        "datatype, lookml_datatype, has_time_frame",
        [
            ("Date", "date", False),
            ("DateTime", "datetime", True),
            ("DateTimeTz", "timestamp", True),
        ],
    )
    def test_temporal_datatypes_become_dimension_groups(self, datatype, lookml_datatype, has_time_frame):
        view = self._dim(datatype=datatype)
        group = _block(view, "dimension_group: f")
        assert "type: time" in group
        assert f"datatype: {lookml_datatype}" in group
        assert ("time," in group) is has_time_frame

    def test_is_time_with_non_temporal_datatype_stays_a_dimension(self):
        # TPC-DS marks `d_year` (Integer) as is_time; a time dimension_group
        # over an integer column would not load in Looker.
        view = self._dim(is_time=True, datatype="Integer")
        assert "dimension_group:" not in view
        assert "type: number" in _block(view, "dimension: f")

    def test_opaque_datatype_warns_and_defaults_to_string(self):
        with pytest.warns(UserWarning, match="Opaque"):
            out = convert_ossie_to_lookml(_model([_dataset("t", [_field("f", datatype="Opaque")])]))
        assert "type: string" in _block(out, "dimension: f")

    def test_unknown_datatype_warns_and_defaults_to_string(self):
        with pytest.warns(UserWarning, match="unrecognized"):
            out = convert_ossie_to_lookml(_model([_dataset("t", [_field("f", datatype="Blob")])]))
        assert "type: string" in _block(out, "dimension: f")


class TestPrimaryKey:
    def test_single_column_key_marks_the_dimension(self):
        out = _convert(_model([ORDERS]))
        assert "primary_key: yes" in _block(out, "dimension: order_id")
        assert "dimension: primary_key" not in out

    def test_composite_key_becomes_hidden_concat_dimension(self):
        ds = _dataset("t", [_field("a"), _field("b")], primary_key=["a", "b"])
        out = _convert(_model([ds]))
        pk = _block(out, "dimension: primary_key")
        assert "primary_key: yes" in pk
        assert "hidden: yes" in pk
        assert "type: string" in pk
        assert "sql: CONCAT(CAST(${TABLE}.a AS STRING), '|', CAST(${TABLE}.b AS STRING)) ;;" in pk

    def test_time_column_key_becomes_hidden_dimension(self):
        # A dimension_group cannot carry primary_key, so fall back to a hidden
        # dimension over the raw column.
        ds = _dataset("t", [_field("created_at", datatype="DateTimeTz")], primary_key=["created_at"])
        out = _convert(_model([ds]))
        pk = _block(out, "dimension: primary_key")
        assert "sql: CAST(${TABLE}.created_at AS STRING) ;;" in pk

    def test_composite_key_uses_the_key_fields_expressions(self):
        # A key names fields; the physical column comes from the field's
        # expression, like every other reference to it.
        ds = _dataset("t", [_field("cust", expr="customer_id"), _field("b")], primary_key=["cust", "b"])
        out = _convert(_model([ds]))
        pk = _block(out, "dimension: primary_key")
        assert "sql: CONCAT(CAST(${TABLE}.customer_id AS STRING), '|', CAST(${TABLE}.b AS STRING)) ;;" in pk

    def test_time_key_uses_the_groups_expression(self):
        ds = _dataset("t", [_field("created", expr="created_at", datatype="DateTimeTz")], primary_key=["created"])
        out = _convert(_model([ds]))
        pk = _block(out, "dimension: primary_key")
        assert "sql: CAST(${TABLE}.created_at AS STRING) ;;" in pk

    def test_key_over_unexposed_column_falls_back_to_the_physical_column(self):
        ds = _dataset("t", [_field("a")], primary_key=["a", "tenant"])
        out = _convert(_model([ds]))
        pk = _block(out, "dimension: primary_key")
        assert "sql: CONCAT(CAST(${TABLE}.a AS STRING), '|', CAST(${TABLE}.tenant AS STRING)) ;;" in pk

    def test_join_target_without_key_warns(self):
        customers = _dataset("customers", [_field("id")])
        model = _model([ORDERS, customers], [_rel("o2c", "orders", "customers", ["customer_id"], ["id"])])
        with pytest.warns(UserWarning, match="customers.*no primary_key"):
            convert_ossie_to_lookml(model)


# ---------------------------------------------------------------------------
# Metrics -> measures
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_qualified_sum_becomes_typed_measure_in_its_dataset(self):
        model = _model(
            [CUSTOMERS, ORDERS],
            metrics=[_metric("total_revenue", "SUM(orders.amount)", description="Total revenue")],
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(model)
        measure = _block(_block(out, "view: orders"), "measure: total_revenue")
        assert "type: sum" in measure
        assert "sql: ${orders.amount} ;;" in measure
        assert 'description: "Total revenue"' in measure
        assert "measure:" not in _block(out, "view: customers")

    @pytest.mark.parametrize(
        "expr, lookml_type, sql",
        [
            ("AVG(orders.amount)", "average", "${orders.amount}"),
            ("MIN(orders.amount)", "min", "${orders.amount}"),
            ("MAX(orders.amount)", "max", "${orders.amount}"),
            ("COUNT(DISTINCT orders.customer_id)", "count_distinct", "${orders.customer_id}"),
        ],
    )
    def test_single_column_aggregates_map_to_native_types(self, expr, lookml_type, sql):
        out = _convert(_model([ORDERS], metrics=[_metric("m", expr)]))
        measure = _block(out, "measure: m")
        assert f"type: {lookml_type}" in measure
        assert f"sql: {sql} ;;" in measure

    def test_count_star_becomes_count_without_sql(self):
        out = _convert(_model([ORDERS], metrics=[_metric("n", "COUNT(*)")]))
        measure = _block(out, "measure: n")
        assert "type: count" in measure
        assert "sql:" not in measure

    def test_complex_expression_hoists_aggregates_into_hidden_measures(self):
        # Looker's symmetric aggregates only protect native typed measures, so
        # each aggregate becomes a hidden helper and the metric is arithmetic.
        expr = "SUM(orders.amount) / NULLIF(COUNT(DISTINCT orders.order_id), 0)"
        out = _convert(_model([ORDERS], metrics=[_metric("aov", expr)]))
        measure = _block(out, "measure: aov")
        assert "type: number" in measure
        assert "sql: ${aov_sum_1} / NULLIF(${aov_count_distinct_2}, 0) ;;" in measure
        helper = _block(out, "measure: aov_sum_1")
        assert "hidden: yes" in helper
        assert "type: sum" in helper
        assert "sql: ${orders.amount} ;;" in helper
        helper = _block(out, "measure: aov_count_distinct_2")
        assert "type: count_distinct" in helper
        assert "sql: ${orders.order_id} ;;" in helper

    def test_aggregate_over_row_expression_is_native(self):
        out = _convert(_model([ORDERS], metrics=[_metric("gross", "SUM(orders.amount * 1.1)")]))
        measure = _block(out, "measure: gross")
        assert "type: sum" in measure
        assert "sql: ${orders.amount} * 1.1 ;;" in measure

    def test_untypeable_aggregate_stays_inline_with_warning(self):
        model = _model([ORDERS], metrics=[_metric("sd", "STDDEV(orders.amount)")])
        with pytest.warns(UserWarning, match="Metric 'sd'.*cannot type natively"):
            out = convert_ossie_to_lookml(model)
        measure = _block(out, "measure: sd")
        assert "type: number" in measure
        assert "sql: STDDEV(${orders.amount}) ;;" in measure

    def test_count_of_a_column_is_a_native_sum_of_non_null_flags(self):
        # An inline COUNT(x) would count fanned-out rows; a native sum keeps
        # symmetric aggregates.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(_model([ORDERS], metrics=[_metric("n", "COUNT(orders.amount)")]))
        measure = _block(out, "measure: n")
        assert "type: sum" in measure
        assert "sql: IF(${orders.amount} IS NOT NULL, 1, 0) ;;" in measure

    def test_count_of_a_literal_is_a_count(self):
        ds = _dataset("orders", [_field("id")], primary_key=["id"], metrics=[_metric("n", "COUNT(1)")])
        measure = _block(_convert(_model([ds])), "measure: n")
        assert "type: count" in measure
        assert "sql:" not in measure

    def test_count_of_a_column_inside_arithmetic_is_hoisted(self):
        out = _convert(_model([ORDERS], metrics=[_metric("r", "SUM(orders.amount) / COUNT(orders.amount)")]))
        assert "sql: ${r_sum_1} / ${r_sum_2} ;;" in _block(out, "measure: r")
        assert "sql: IF(${orders.amount} IS NOT NULL, 1, 0) ;;" in _block(out, "measure: r_sum_2")

    def test_window_function_warns_and_stays_inline(self):
        expr = "SUM(SUM(orders.amount)) OVER (ORDER BY orders.order_id)"
        with pytest.warns(UserWarning, match="Metric 'running'.*window"):
            out = convert_ossie_to_lookml(_model([ORDERS], metrics=[_metric("running", expr)]))
        measure = _block(out, "measure: running")
        assert "type: number" in measure
        assert "SUM(SUM(${orders.amount})) OVER (ORDER BY ${orders.order_id}" in measure

    def test_unqualified_column_homes_in_its_only_owner(self):
        out = _convert(_model([CUSTOMERS, ORDERS], metrics=[_metric("rev", "SUM(amount)")]))
        measure = _block(_block(out, "view: orders"), "measure: rev")
        assert "sql: ${TABLE}.amount ;;" in measure

    def test_ambiguous_unqualified_column_warns_and_uses_first_dataset(self):
        model = _model([CUSTOMERS, REGIONS], metrics=[_metric("ids", "COUNT(DISTINCT id)")])
        with pytest.warns(UserWarning, match="Metric 'ids'.*several datasets.*'customers'"):
            out = convert_ossie_to_lookml(model)
        assert "measure: ids" in _block(out, "view: customers")

    def test_unknown_unqualified_column_warns(self):
        model = _model([CUSTOMERS, ORDERS], metrics=[_metric("t", "SUM(tax)")])
        with pytest.warns(UserWarning, match="Metric 't'.*no dataset exposes.*'tax'"):
            convert_ossie_to_lookml(model)

    def test_qualified_unknown_field_falls_back_to_physical_column_with_warning(self):
        model = _model([ORDERS], metrics=[_metric("t", "SUM(orders.tax)")])
        with pytest.warns(UserWarning, match="Metric 't'.*'orders.tax'"):
            out = convert_ossie_to_lookml(model)
        assert "sql: ${TABLE}.tax ;;" in _block(out, "measure: t")

    def test_cross_dataset_metric_warns_per_metric(self):
        expr = "SUM(orders.amount) / COUNT(DISTINCT customers.id)"
        model = _model([CUSTOMERS, ORDERS], metrics=[_metric("rpc", expr)])
        with pytest.warns(UserWarning, match="Metric 'rpc'.*customers"):
            out = convert_ossie_to_lookml(model)
        measure = _block(_block(out, "view: orders"), "measure: rpc")
        assert "sql: ${rpc_sum_1} / ${customers.rpc_count_distinct_2} ;;" in measure
        helper = _block(_block(out, "view: customers"), "measure: rpc_count_distinct_2")
        assert "sql: ${customers.id} ;;" in helper

    def test_helper_over_another_dataset_goes_in_that_datasets_view(self):
        # TPC-DS store_productivity: Looker counts each row of a measure's own
        # view once, so the employee sum must live in `store` to count each
        # store once rather than once per sales row.
        sales = _dataset("sales", [_field("id"), _field("store_id"), _field("price")], primary_key=["id"])
        store = _dataset("store", [_field("id"), _field("employees")], primary_key=["id"])
        expr = "SUM(sales.price) / NULLIF(SUM(store.employees), 0)"
        model = _model([sales, store], [_rel("s2s", "sales", "store", ["store_id"], ["id"])], metrics=[_metric("p", expr)])
        out = _convert(model)
        sales_view, store_view = _block(out, "view: sales"), _block(out, "view: store")
        assert "sql: ${p_sum_1} / NULLIF(${store.p_sum_2}, 0) ;;" in _block(sales_view, "measure: p")
        assert "sql: ${sales.price} ;;" in _block(sales_view, "measure: p_sum_1")
        helper = _block(store_view, "measure: p_sum_2")
        assert "hidden: yes" in helper
        assert "sql: ${store.employees} ;;" in helper
        assert "measure: p_sum_2" not in sales_view

    def test_helper_in_another_view_avoids_that_views_names(self):
        customers = _dataset("customers", [_field("id"), _field("rpc_count_distinct_2")], primary_key=["id"])
        expr = "SUM(orders.amount) / COUNT(DISTINCT customers.id)"
        out = _convert(_model([customers, ORDERS], metrics=[_metric("rpc", expr)]))
        assert "sql: ${rpc_sum_1} / ${customers.rpc_count_distinct_3} ;;" in _block(out, "measure: rpc")
        assert "sql: ${customers.id} ;;" in _block(out, "measure: rpc_count_distinct_3")

    def test_helper_over_several_datasets_stays_in_the_home_view(self):
        expr = "SUM(orders.amount * customers.id) / COUNT(*)"
        out = _convert(_model([ORDERS, CUSTOMERS], metrics=[_metric("x", expr)]))
        orders_view = _block(out, "view: orders")
        assert "sql: ${orders.amount} * ${customers.id} ;;" in _block(orders_view, "measure: x_sum_1")
        assert "sql: ${x_sum_1} / ${x_count_2} ;;" in _block(orders_view, "measure: x")

    def test_bare_columns_several_datasets_expose_go_to_the_first_that_does(self):
        # The first dataset in the model does not expose `id`.
        calendar = _dataset("calendar", [_field("day")], primary_key=["day"])
        customers2 = _dataset("customers2", [_field("id"), _field("region_id")], primary_key=["id"])
        model = _model([calendar, CUSTOMERS, customers2], metrics=[_metric("n", "COUNT(DISTINCT region_id)")])
        with pytest.warns(UserWarning, match="Metric 'n'.*several datasets expose.*emitted in view 'customers'"):
            out = convert_ossie_to_lookml(model)
        assert "measure: n {" in _block(out, "view: customers")
        assert "measure: n {" not in _block(out, "view: calendar")

    def test_unparseable_metric_is_verbatim_with_warning(self):
        model = _model([ORDERS], metrics=[_metric("bad", "SUM((")])
        with pytest.warns(UserWarning, match="Metric 'bad'.*verbatim"):
            out = convert_ossie_to_lookml(model)
        assert "sql: SUM(( ;;" in _block(out, "measure: bad")


# ---------------------------------------------------------------------------
# Relationships -> explore/join
# ---------------------------------------------------------------------------

class TestRelationships:
    def test_simple_join(self):
        out = _convert(_model([ORDERS, CUSTOMERS], [_rel("o2c", "orders", "customers", ["customer_id"], ["id"])]))
        join = _block(_block(out, "explore: orders"), "join: customers")
        assert "from:" not in join
        assert "type: left_outer" in join
        assert "relationship: many_to_one" in join
        assert "sql_on: ${orders.customer_id} = ${customers.id} ;;" in join

    def test_composite_join_uses_and(self):
        a = _dataset("a", [_field("x"), _field("y")], primary_key=["x", "y"])
        b = _dataset("b", [_field("x"), _field("y")], primary_key=["x", "y"])
        out = _convert(_model([a, b], [_rel("a2b", "a", "b", ["x", "y"], ["x", "y"])]))
        assert "sql_on: ${a.x} = ${b.x} AND ${a.y} = ${b.y} ;;" in out

    def test_mismatched_columns_raise(self):
        model = _model([ORDERS, CUSTOMERS], [_rel("o2c", "orders", "customers", ["customer_id", "x"], ["id"])])
        with pytest.raises(OssieConversionError, match="same length"):
            convert_ossie_to_lookml(model)

    @pytest.mark.parametrize("from_cols, to_cols", [(None, ["id"]), ([], ["id"]), (["customer_id"], None)])
    def test_missing_or_empty_columns_raise_conversion_error(self, from_cols, to_cols):
        model = _model([ORDERS, CUSTOMERS], [_rel("o2c", "orders", "customers", from_cols, to_cols)])
        with pytest.raises(OssieConversionError, match="Relationship 'o2c'"):
            convert_ossie_to_lookml(model)

    def test_two_relationships_to_same_dataset_are_aliased(self):
        orders = _dataset("orders", [_field("id"), _field("billing_addr"), _field("shipping_addr")], primary_key=["id"])
        addresses = _dataset("addresses", [_field("id")], primary_key=["id"])
        model = _model(
            [orders, addresses],
            [
                _rel("billing_address", "orders", "addresses", ["billing_addr"], ["id"]),
                _rel("shipping_address", "orders", "addresses", ["shipping_addr"], ["id"]),
            ],
        )
        out = _convert(model)
        explore = _block(out, "explore: orders")
        assert "join: addresses {" not in explore
        billing = _block(explore, "join: billing_address")
        assert "from: addresses" in billing
        assert "sql_on: ${orders.billing_addr} = ${billing_address.id} ;;" in billing
        shipping = _block(explore, "join: shipping_address")
        assert "from: addresses" in shipping
        assert "sql_on: ${orders.shipping_addr} = ${shipping_address.id} ;;" in shipping
        assert lkml.load(out)["explores"][0]["joins"][0]["from"] == "addresses"

    def test_alias_must_be_a_valid_lookml_name(self):
        orders = _dataset("orders", [_field("a"), _field("b")])
        addresses = _dataset("addresses", [_field("id")], primary_key=["id"])
        model = _model(
            [orders, addresses],
            [
                _rel("billing address", "orders", "addresses", ["a"], ["id"]),
                _rel("shipping-address", "orders", "addresses", ["b"], ["id"]),
            ],
        )
        with pytest.raises(OssieConversionError, match="'billing address'.*LookML"):
            convert_ossie_to_lookml(model)

    def test_chain_is_joined_transitively(self):
        model = _model(
            [ORDERS, CUSTOMERS, REGIONS],
            [
                _rel("o2c", "orders", "customers", ["customer_id"], ["id"]),
                _rel("c2r", "customers", "regions", ["region_id"], ["id"]),
            ],
        )
        out = _convert(model)
        orders = _block(out, "explore: orders")
        assert "sql_on: ${orders.customer_id} = ${customers.id} ;;" in _block(orders, "join: customers")
        assert "sql_on: ${customers.region_id} = ${regions.id} ;;" in _block(orders, "join: regions")
        customers = _block(out, "explore: customers")
        assert "join: regions {" in customers
        assert "explore: regions" not in out

    def test_transitive_join_through_alias_uses_the_alias(self):
        orders = _dataset("orders", [_field("id"), _field("bill_id"), _field("ship_id")], primary_key=["id"])
        addresses = _dataset("addresses", [_field("id"), _field("region_id")], primary_key=["id"])
        model = _model(
            [orders, addresses, REGIONS],
            [
                _rel("billing", "orders", "addresses", ["bill_id"], ["id"]),
                _rel("shipping", "orders", "addresses", ["ship_id"], ["id"]),
                _rel("a2r", "addresses", "regions", ["region_id"], ["id"]),
            ],
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(model)
        explore = _block(out, "explore: orders")
        # Each aliased path keeps its own copy of the downstream view.
        billing_regions = _block(explore, "join: billing_regions")
        assert "from: regions" in billing_regions
        assert "sql_on: ${billing.region_id} = ${billing_regions.id} ;;" in billing_regions
        assert "sql_on: ${shipping.region_id} = ${shipping_regions.id} ;;" in _block(explore, "join: shipping_regions")
        assert "join: regions {" not in explore

    def test_diamond_joins_the_shared_target_once_with_warning(self):
        stores = _dataset("stores", [_field("id"), _field("region_id")], primary_key=["id"])
        orders = _dataset("orders", [_field("id"), _field("customer_id"), _field("store_id")], primary_key=["id"])
        model = _model(
            [orders, CUSTOMERS, stores, REGIONS],
            [
                _rel("o2c", "orders", "customers", ["customer_id"], ["id"]),
                _rel("o2s", "orders", "stores", ["store_id"], ["id"]),
                _rel("c2r", "customers", "regions", ["region_id"], ["id"]),
                _rel("s2r", "stores", "regions", ["region_id"], ["id"]),
            ],
        )
        with pytest.warns(UserWarning, match="Relationship 's2r'.*'regions' is already joined"):
            out = convert_ossie_to_lookml(model)
        explore = _block(out, "explore: orders")
        assert explore.count("join: regions {") == 1
        assert "sql_on: ${customers.region_id} = ${regions.id} ;;" in explore

    def test_cycle_terminates_with_warning(self):
        a = _dataset("a", [_field("id"), _field("b_id")], primary_key=["id"])
        b = _dataset("b", [_field("id"), _field("a_id")], primary_key=["id"])
        model = _model([a, b], [_rel("a2b", "a", "b", ["b_id"], ["id"]), _rel("b2a", "b", "a", ["a_id"], ["id"])])
        with pytest.warns(UserWarning, match="Relationship 'b2a'.*cycle"):
            out = convert_ossie_to_lookml(model)
        assert _block(_block(out, "explore: a"), "join: b")
        assert "join: a {" not in _block(out, "explore: a")

    def test_sql_on_uses_raw_timeframe_for_dimension_groups(self):
        events = _dataset("events", [_field("id"), _field("created_timestamp", datatype="DateTimeTz")], primary_key=["id"])
        days = _dataset("days", [_field("day_ts", datatype="DateTimeTz")], primary_key=["day_ts"])
        model = _model([events, days], [_rel("e2d", "events", "days", ["created_timestamp"], ["day_ts"])])
        out = _convert(model)
        assert "sql_on: ${events.created_raw} = ${days.day_ts_raw} ;;" in out

    def test_sql_on_resolves_a_column_to_the_field_that_exposes_it(self):
        orders = _dataset("orders", [_field("id"), _field("cust", expr="customer_id")], primary_key=["id"])
        model = _model([orders, CUSTOMERS], [_rel("o2c", "orders", "customers", ["customer_id"], ["id"])])
        out = _convert(model)
        assert "sql_on: ${orders.cust} = ${customers.id} ;;" in out

    def test_sql_on_resolves_a_quoted_column_to_its_field(self):
        orders = _dataset("orders", [_field("id"), _field("cust", expr="`Customer ID`", dialect="BIGQUERY")], primary_key=["id"])
        model = _model([orders, CUSTOMERS], [_rel("o2c", "orders", "customers", ["Customer ID"], ["id"])])
        assert "sql_on: ${orders.cust} = ${customers.id} ;;" in _convert(model)

    def test_sql_on_unknown_column_warns(self):
        orders = _dataset("orders", [_field("id")], primary_key=["id"])
        model = _model([orders, CUSTOMERS], [_rel("o2c", "orders", "customers", ["customer_id"], ["id"])])
        with pytest.warns(UserWarning, match="Relationship 'o2c'.*'customer_id'.*no field"):
            out = convert_ossie_to_lookml(model)
        assert "sql_on: ${orders.customer_id} = ${customers.id} ;;" in out

    def test_unknown_dataset_warns(self):
        model = _model([ORDERS], [_rel("o2x", "orders", "ghosts", ["customer_id"], ["id"])])
        with pytest.warns(UserWarning, match="'ghosts' is not defined"):
            convert_ossie_to_lookml(model)

    def test_relationship_extensions_are_reported_as_dropped(self):
        rel = _rel("o2c", "orders", "customers", ["customer_id"], ["id"])
        rel["ai_context"] = {"instructions": "x"}
        rel["custom_extensions"] = [{"vendor": "X", "attributes": {}}]
        with pytest.warns(UserWarning, match="relationship 'o2c'") as record:
            convert_ossie_to_lookml(_model([ORDERS, CUSTOMERS], [rel]))
        messages = [str(w.message) for w in record if "relationship 'o2c'" in str(w.message)]
        assert any("ai_context" in m and "custom_extensions" in m for m in messages)


class TestExplores:
    def test_every_unjoined_dataset_gets_a_bare_explore(self):
        out = _convert(_model([ORDERS, CUSTOMERS]))
        assert "explore: orders {}" in out
        assert "explore: customers {}" in out

    def test_join_targets_do_not_get_their_own_explore(self):
        out = _convert(_model([ORDERS, CUSTOMERS], [_rel("o2c", "orders", "customers", ["customer_id"], ["id"])]))
        assert "explore: customers" not in out

    def test_output_has_view_and_model_sections(self):
        out = _convert(_model([ORDERS]))
        assert out.index("# Views") < out.index("view: orders") < out.index("# Explores") < out.index("explore: orders")


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------

class TestEntityDescription:
    def test_combines_description_and_synonyms(self):
        entity = {"description": "Orders", "ai_context": {"synonyms": ["purchases", "sales"]}}
        assert _entity_description(entity, "e") == "Orders (synonyms: purchases, sales)"

    def test_string_ai_context_appended(self):
        assert _entity_description({"description": "A", "ai_context": "B"}, "e") == "A B"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_bigquery_fixture(self):
        out = _convert((FIXTURES / "ossie_bigquery_example.yaml").read_text())
        orders = _block(out, "view: orders")
        assert "sql_table_name: `my_project.sales.orders` ;;" in orders
        # Looker has no view-level description: it is kept as a comment in the
        # view and emitted on the explore.
        assert "# Customer orders (synonyms: purchases, sales)" in orders
        assert "description:" not in orders.split("dimension:")[0]
        assert 'description: "Customer orders (synonyms: purchases, sales)"' in _block(out, "explore: orders")
        assert "primary_key: yes" in _block(orders, "dimension: order_id")
        assert "type: number" in _block(orders, "dimension: amount")
        assert "datatype: timestamp" in _block(orders, "dimension_group: order")
        assert "type: sum" in _block(orders, "measure: total_revenue")
        assert "sql: SAFE_CAST(LOWER(${TABLE}.email) AS STRING) ;;" in _block(out, "view: customers")
        join = _block(_block(out, "explore: orders"), "join: customers")
        assert "sql_on: ${orders.customer_id} = ${customers.id} ;;" in join

    def test_bigquery_fixture_is_valid_lookml(self):
        parsed = lkml.load(_convert((FIXTURES / "ossie_bigquery_example.yaml").read_text()))
        assert {v["name"] for v in parsed["views"]} == {"orders", "customers"}
        assert [e["name"] for e in parsed["explores"]] == ["orders"]

    @pytest.mark.skipif(not TPCDS.exists(), reason="TPC-DS example not found")
    def test_tpcds_example_converts_to_valid_lookml(self):
        out = _convert(TPCDS.read_text())
        parsed = lkml.load(out)
        views = {v["name"]: v for v in parsed["views"]}
        assert "store_sales" in views
        store_sales = _block(out, "view: store_sales")
        assert "primary_key: yes" in _block(store_sales, "dimension: primary_key")
        assert "type: sum" in _block(store_sales, "measure: total_sales")
        assert "sql: ${store_sales.ss_ext_sales_price} ;;" in _block(store_sales, "measure: total_sales")
        # d_year is an Integer marked is_time: a number dimension, not a group.
        date_dim = _block(out, "view: date_dim")
        assert "type: number" in _block(date_dim, "dimension: d_year")
        # d_moy and d_quarter_name set is_time with no datatype: plain dimensions.
        for name in ("d_moy", "d_quarter_name"):
            assert f"dimension_group: {name} {{" not in date_dim
            assert "type: string" in _block(date_dim, f"dimension: {name}")
        # The employee sum is counted per store, in the store view.
        assert "NULLIF(${store.store_productivity_sum_2}, 0)" in _block(store_sales, "measure: store_productivity")
        assert "sql: ${store.s_number_employees} ;;" in _block(
            _block(out, "view: store"), "measure: store_productivity_sum_2"
        )
        explore = _block(out, "explore: store_sales")
        for target in ("date_dim", "item", "customer", "store"):
            assert f"join: {target} {{" in explore


# ---------------------------------------------------------------------------
# The review's principles applied to the rest of the input: malformed input
# is a clean OssieConversionError, every emitted name is a unique valid LookML
# name, and nothing is dropped without a warning naming the entity.
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_invalid_yaml_is_a_conversion_error(self):
        with pytest.raises(OssieConversionError, match="Invalid Ossie YAML"):
            convert_ossie_to_lookml("name: [unclosed\nversion: x")

    @pytest.mark.parametrize(
        "model, message",
        [
            ({"datasets": ["orders"]}, "Dataset #1.*mapping"),
            ({"datasets": "orders"}, "'datasets' must be a list"),
            ({"datasets": [{"name": "t", "source": "s", "fields": [42]}]}, "Dataset 't'.*field #1.*mapping"),
            ({"datasets": [{"name": "t", "source": "s", "fields": {"a": 1}}]}, "Dataset 't'.*'fields' must be a list"),
            ({"datasets": [{"name": "t", "source": "s", "primary_key": "id"}]}, "Dataset 't'.*'primary_key' must be a list"),
            ({"datasets": [{"name": "t", "source": "s", "primary_key": [1]}]}, "Dataset 't'.*'primary_key'.*non-empty strings"),
            ({"datasets": [_dataset("t", [])], "metrics": [None]}, "Metric #1.*mapping"),
            ({"datasets": [_dataset("t", [])], "relationships": ["x"]}, "Relationship #1.*mapping"),
            ({"datasets": [_dataset("t", [{"name": "f", "expression": {"dialects": ["ANSI_SQL"]}}])]}, "Field 't.f'.*dialect #1.*mapping"),
            ({"datasets": [_dataset("t", [{"name": "f", "expression": {"dialects": "x"}}])]}, "Field 't.f'.*'dialects' must be a list"),
            # YAML reads `2024` as an int and `yes` / `on` as booleans.
            ({"datasets": [{"name": 2024, "source": "a.b.c"}]}, "Dataset '2024'.*not a valid LookML view name"),
            ({"datasets": [_dataset("t", [])], "metrics": [{"name": True, "expression": _ansi("COUNT(*)")}]}, "Metric 'True'.*not a valid LookML name"),
            ({"datasets": [_dataset("t", [], metrics=[{"name": 7, "expression": _ansi("COUNT(*)")}])]}, "Metric '7'.*not a valid LookML name"),
            ({"datasets": [_dataset("a", [_field("id")]), _dataset("b", [_field("id")], primary_key=["id"])],
              "relationships": [_rel(True, "a", "b", ["id"], ["id"])]}, "Relationship 'True'.*name must be a string"),
            ({"datasets": [_dataset("a", [_field("id")]), _dataset("b", [_field("id")], primary_key=["id"])],
              "relationships": [_rel("r", ["a"], "b", ["id"], ["id"])]}, "Relationship 'r'.*'from' and 'to' must be dataset names"),
            ({"datasets": [_dataset("t", [{"name": "f", "expression": _ansi(1)}])]}, "Field 't.f'.*expression must be a string"),
            ({"datasets": [_dataset("t", [{"name": "f", "expression": {"dialects": [{"dialect": 1, "expression": "x"}]}}])]}, "Field 't.f'.*'dialect' must be a string"),
            ({"datasets": [_dataset("t", [_field("f", datatype=["Integer"])])]}, "Field 't.f'.*'datatype' must be a string"),
            ({"datasets": [{"name": "t", "source": 42}]}, "Dataset 't'.*'source' must be a string"),
            ({"datasets": [_dataset("t", [])], "description": True}, "Model.*'description' must be a string"),
            ({"datasets": [{**_dataset("t", []), "description": 2024}]}, "Dataset 't'.*'description' must be a string"),
            ({"datasets": [_dataset("t", [_field("f", description=True)])]}, "Field 't.f'.*'description' must be a string"),
            ({"datasets": [_dataset("t", [])], "metrics": [_metric("m", "COUNT(*)", description=["x"])]},
             "Metric 'm'.*'description' must be a string"),
        ],
    )
    def test_wrongly_typed_containers_are_conversion_errors(self, model, message):
        doc = yaml.dump({"version": "0.2.0.dev0", "name": "m", **model})
        with pytest.raises(OssieConversionError, match=message):
            convert_ossie_to_lookml(doc)

    def test_relationship_columns_must_be_strings(self):
        model = _model([ORDERS, CUSTOMERS], [_rel("o2c", "orders", "customers", [1], ["id"])])
        with pytest.raises(OssieConversionError, match="Relationship 'o2c'.*from_columns.*non-empty strings"):
            convert_ossie_to_lookml(model)


class TestNameSafety:
    def test_field_name_must_be_a_valid_lookml_name(self):
        with pytest.raises(OssieConversionError, match="Field 't.order id'.*LookML"):
            convert_ossie_to_lookml(_model([_dataset("t", [_field("order id")])]))

    def test_duplicate_field_names_raise(self):
        with pytest.raises(OssieConversionError, match="Dataset 't'.*duplicate field name 'a'"):
            convert_ossie_to_lookml(_model([_dataset("t", [_field("a"), _field("a")])]))

    def test_duplicate_dataset_names_raise(self):
        with pytest.raises(OssieConversionError, match="Duplicate dataset name 't'"):
            convert_ossie_to_lookml(_model([_dataset("t", [_field("a")]), _dataset("t", [_field("b")])]))

    def test_duplicate_metric_names_raise(self):
        model = _model([ORDERS], metrics=[_metric("m", "SUM(orders.amount)"), _metric("m", "COUNT(*)")])
        with pytest.raises(OssieConversionError, match="Duplicate metric name 'm'"):
            convert_ossie_to_lookml(model)

    def test_duplicate_relationship_names_raise(self):
        rels = [_rel("r", "orders", "customers", ["customer_id"], ["id"])] * 2
        with pytest.raises(OssieConversionError, match="Duplicate relationship name 'r'"):
            convert_ossie_to_lookml(_model([ORDERS, CUSTOMERS], rels))

    def test_dimension_group_avoids_timeframe_collisions_with_fields(self):
        # `created_timestamp` would become group `created`, which exposes
        # `created_date`; that already exists as a plain field.
        fields = [_field("created_date"), _field("created_timestamp", datatype="DateTimeTz")]
        out = _convert(_model([_dataset("t", fields)]))
        assert "dimension: created_date {" in out
        assert "dimension_group: created_timestamp {" in out
        assert "dimension_group: created {" not in out

    def test_measure_colliding_with_a_field_is_renamed_with_warning(self):
        orders = _dataset("orders", [_field("id"), _field("amount")], primary_key=["id"])
        model = _model([orders], metrics=[_metric("amount", "SUM(orders.amount)")])
        with pytest.warns(UserWarning, match="Metric 'amount'.*clashes with a field.*'amount_measure'"):
            out = convert_ossie_to_lookml(model)
        assert "measure: amount_measure {" in out
        assert "measure: amount {" not in out

    def test_helper_measure_names_avoid_existing_fields(self):
        orders = _dataset("orders", [_field("id"), _field("amount"), _field("aov_sum_1")], primary_key=["id"])
        model = _model([orders], metrics=[_metric("aov", "SUM(orders.amount) / COUNT(DISTINCT orders.id)")])
        out = _convert(model)
        assert "measure: aov_sum_1 {" not in out
        assert "measure: aov_sum_2 {" in out
        assert "sql: ${aov_sum_2} / ${aov_count_distinct_2} ;;" in _block(out, "measure: aov")

    def test_synthetic_primary_key_avoids_a_field_named_primary_key(self):
        ds = _dataset("t", [_field("primary_key"), _field("b")], primary_key=["primary_key", "b"])
        out = _convert(_model([ds]))
        assert "dimension: pk {" in out
        assert "primary_key: yes" in _block(out, "dimension: pk")


class TestNotSilentlyDropped:
    def test_unique_keys_are_reported(self):
        ds = _dataset("t", [_field("a")], primary_key=["a"])
        ds["unique_keys"] = [["a"]]
        with pytest.warns(UserWarning, match="dataset 't'.*unique_keys"):
            convert_ossie_to_lookml(_model([ds]))

    def test_entity_instructions_join_the_description(self):
        entity = {"description": "Orders", "ai_context": {"instructions": "Use for sales.", "synonyms": ["purchases"]}}
        assert _entity_description(entity, "e") == "Orders Use for sales. (synonyms: purchases)"

    def test_other_ai_context_keys_are_reported(self):
        field = _field("a")
        field["ai_context"] = {"examples": ["What sold last week?"]}
        with pytest.warns(UserWarning, match="Field 't.a'.*ai_context.examples"):
            convert_ossie_to_lookml(_model([_dataset("t", [field])]))


class TestTimezones:
    def test_civil_datetime_does_not_convert_timezone(self):
        # Ossie DateTime (and BigQuery DATETIME) carry no timezone; Looker's
        # default convert_tz would shift them.
        view = _block(_convert(_model([_dataset("t", [_field("f", datatype="DateTime")])])), "view: t")
        assert "convert_tz: no" in _block(view, "dimension_group: f")

    @pytest.mark.parametrize("datatype", ["Date", "DateTimeTz"])
    def test_instants_and_dates_keep_looker_defaults(self, datatype):
        view = _block(_convert(_model([_dataset("t", [_field("f", datatype=datatype)])])), "view: t")
        assert "convert_tz" not in _block(view, "dimension_group: f")


class TestSourceDialects:
    """ANSI_SQL and OSSIE_SQL_2026 are read as ANSI and transpiled to BigQuery,
    since the two disagree on quoting and argument order."""

    def _view(self, *fields):
        return _block(_convert(_model([_dataset("t", list(fields))])), "view: t")

    def test_ansi_double_quotes_are_an_identifier(self):
        assert "sql: ${TABLE}.`Customer ID` ;;" in self._view(_field("cid", expr='"Customer ID"'))

    def test_ansi_date_trunc_is_transpiled(self):
        view = self._view(_field("m", expr="DATE_TRUNC('month', order_date)", dialect="OSSIE_SQL_2026"))
        assert "sql: DATE_TRUNC(${TABLE}.order_date, MONTH) ;;" in view

    def test_ansi_count_distinct_of_quoted_identifier(self):
        ds = _dataset("t", [_field("id"), _field("cid", expr='"customer id"')], primary_key=["id"])
        out = _convert(_model([ds], metrics=[_metric("n", 'COUNT(DISTINCT t.cid)')]))
        measure = _block(out, "measure: n")
        assert "type: count_distinct" in measure
        assert "sql: ${t.cid} ;;" in measure

    def test_bigquery_dialect_is_read_as_bigquery(self):
        # In BigQuery a double-quoted token is a string literal.
        assert "sql: 'x' ;;" in self._view(_field("lit", expr='"x"', dialect="BIGQUERY"))


class TestFilterClause:
    """`AGG(x) FILTER (WHERE c)` becomes `AGG(IF(c, x, NULL))`, so it is still
    typed natively and stays under symmetric aggregates."""

    DS = _dataset("t", [_field("id"), _field("amount"), _field("status"), _field("cid")], primary_key=["id"])

    def _measure(self, expr, name="m"):
        return _block(_convert(_model([self.DS], metrics=[_metric(name, expr)])), f"measure: {name}")

    def test_filtered_sum_is_a_native_sum(self):
        measure = self._measure("SUM(t.amount) FILTER (WHERE t.status = 'complete')")
        assert "type: sum" in measure
        assert "sql: IF(${t.status} = 'complete', ${t.amount}, NULL) ;;" in measure

    def test_filtered_count_distinct_is_native(self):
        measure = self._measure("COUNT(DISTINCT t.cid) FILTER (WHERE t.status = 'x')")
        assert "type: count_distinct" in measure
        assert "sql: IF(${t.status} = 'x', ${t.cid}, NULL) ;;" in measure

    def test_filtered_count_star_is_a_native_sum_of_matches(self):
        # A native measure keeps symmetric aggregates across fanning joins;
        # an inline COUNT(IF(...)) would count fanned-out rows.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(_model([self.DS], metrics=[_metric("m", "COUNT(*) FILTER (WHERE t.status = 'x')")]))
        measure = _block(out, "measure: m")
        assert "type: sum" in measure
        assert "sql: IF(${t.status} = 'x', 1, 0) ;;" in measure

    def test_filtered_aggregate_inside_arithmetic_hoists_the_filter(self):
        out = _convert(_model([self.DS], metrics=[_metric("r", "SUM(t.amount) FILTER (WHERE t.status = 'x') / SUM(t.amount)")]))
        helper = _block(out, "measure: r_sum_1")
        assert "sql: IF(${t.status} = 'x', ${t.amount}, NULL) ;;" in helper
        assert "sql: ${r_sum_1} / ${r_sum_2} ;;" in _block(out, "measure: r")


class TestUnparseable:
    def test_tokenizer_error_is_verbatim_with_warning(self):
        model = _model([_dataset("t", [_field("full", expr="CONCAT(first, ', last)")])])
        with pytest.warns(UserWarning, match="Field 't.full'.*verbatim"):
            out = convert_ossie_to_lookml(model)
        assert "sql: CONCAT(first, ', last) ;;" in out

    def test_tokenizer_error_in_a_metric_is_verbatim_with_warning(self):
        model = _model([ORDERS], metrics=[_metric("m", "SUM(CONCAT(a, ')")])
        with pytest.warns(UserWarning, match="Metric 'm'.*verbatim"):
            convert_ossie_to_lookml(model)


class TestMetricFieldReferences:
    """Metric expressions refer to fields, never source columns (Ossie spec,
    Name Resolution); field expressions refer to source columns."""

    DS = _dataset(
        "orders",
        [_field("id"), _field("price"), _field("quantity"), _field("revenue", expr="price * quantity")],
        primary_key=["id"],
    )

    def test_bare_name_of_a_computed_field_references_the_field(self):
        out = _convert(_model([self.DS], metrics=[_metric("total_revenue", "SUM(revenue)")]))
        measure = _block(out, "measure: total_revenue")
        assert "type: sum" in measure
        assert "sql: ${revenue} ;;" in measure

    def test_bare_name_of_a_pass_through_field_stays_the_column(self):
        out = _convert(_model([self.DS], metrics=[_metric("q", "SUM(quantity)")]))
        assert "sql: ${TABLE}.quantity ;;" in _block(out, "measure: q")

    def test_bare_name_of_a_computed_time_field_references_its_raw_timeframe(self):
        ds = _dataset("e", [_field("id"), _field("seen", expr="TIMESTAMP_SECONDS(ts)", datatype="DateTimeTz")], primary_key=["id"])
        out = _convert(_model([ds], metrics=[_metric("last_seen", "MAX(seen)")]))
        assert "sql: ${seen_raw} ;;" in _block(out, "measure: last_seen")

    def test_field_expressions_still_read_source_columns(self):
        # `revenue` in a field expression is the source column, not the field.
        ds = _dataset("o", [_field("revenue", expr="price * quantity"), _field("gross", expr="revenue * 1.2")])
        out = _convert(_model([ds]))
        assert "sql: ${TABLE}.revenue * 1.2 ;;" in _block(out, "dimension: gross")


class TestDatasetMetrics:
    """`datasets[].metrics` (dataset-scoped metrics) become measures in that
    dataset's view, resolving single-part names against its fields."""

    def test_dataset_metric_becomes_a_measure_in_its_view(self):
        ds = _dataset("orders", [_field("id"), _field("amount")], primary_key=["id"], metrics=[_metric("total", "SUM(amount)")])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(_model([ds]))
        measure = _block(_block(out, "view: orders"), "measure: total")
        assert "type: sum" in measure
        assert "sql: ${TABLE}.amount ;;" in measure

    def test_dataset_metric_is_placed_in_its_dataset_even_when_another_exposes_the_field(self):
        a = _dataset("a", [_field("id"), _field("amount")], primary_key=["id"])
        b = _dataset("b", [_field("id"), _field("amount")], primary_key=["id"], metrics=[_metric("total", "SUM(amount)")])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(_model([a, b]))
        assert "measure: total" in _block(out, "view: b")
        assert "measure: total" not in _block(out, "view: a")

    def test_dataset_metric_referencing_another_dataset_is_an_error(self):
        a = _dataset("a", [_field("id")], primary_key=["id"])
        b = _dataset("b", [_field("id")], primary_key=["id"], metrics=[_metric("n", "COUNT(DISTINCT a.id)")])
        with pytest.raises(OssieConversionError, match="Metric 'b.n'.*references dataset 'a'"):
            convert_ossie_to_lookml(_model([a, b]))

    def test_dataset_metric_name_must_not_repeat_a_field_name(self):
        ds = _dataset("t", [_field("id"), _field("total")], metrics=[_metric("total", "COUNT(*)")])
        with pytest.raises(OssieConversionError, match="Dataset 't'.*'total'.*both a field and a metric"):
            convert_ossie_to_lookml(_model([ds]))

    def test_duplicate_dataset_metric_names_raise(self):
        ds = _dataset("t", [_field("id")], metrics=[_metric("n", "COUNT(*)"), _metric("n", "COUNT(*)")])
        with pytest.raises(OssieConversionError, match="Dataset 't'.*duplicate metric name 'n'"):
            convert_ossie_to_lookml(_model([ds]))

    @pytest.mark.skipif(not TPCDS.exists(), reason="TPC-DS example not found")
    def test_tpcds_dataset_metric_is_converted(self):
        out = _convert(TPCDS.read_text())
        measure = _block(_block(out, "view: store_sales"), "measure: total_quantity")
        assert "type: sum" in measure


class TestTimeframeCollisions:
    def test_unshortened_group_drops_a_timeframe_a_field_already_uses(self):
        ds = _dataset("t", [_field("created", datatype="DateTimeTz"), _field("created_year", datatype="Integer")])
        with pytest.warns(UserWarning, match="Field 't.created'.*timeframe 'year'.*created_year"):
            out = convert_ossie_to_lookml(_model([ds]))
        group = _block(out, "dimension_group: created")
        assert "timeframes: [raw, time, date, week, month, quarter]" in group
        lkml.load(out)

    def test_raw_timeframe_collision_is_an_error(self):
        ds = _dataset("t", [_field("created", datatype="DateTimeTz"), _field("created_raw")])
        with pytest.raises(OssieConversionError, match="Field 't.created'.*created_raw"):
            convert_ossie_to_lookml(_model([ds]))

    def test_groups_never_expose_the_same_name(self):
        # `a` (Date) expands to a_date; `a_date_time` shortens to the group
        # `a_date`, which is that same name.
        ds = _dataset("t", [_field("a", datatype="Date"), _field("a_date_time", datatype="DateTimeTz")])
        out = _convert(_model([ds]))
        view = lkml.load(out)["views"][0]
        exposed = [d["name"] for d in view.get("dimensions", [])]
        for group in view.get("dimension_groups", []):
            exposed += [group["name"], *(f"{group['name']}_{tf}" for tf in group["timeframes"])]
        assert len(exposed) == len(set(exposed)), exposed


class TestNotSilentlyDroppedSpecKeys:
    def test_metric_datatype_is_reported(self):
        model = _model([ORDERS], metrics=[_metric("n", "COUNT(*)", datatype="Integer")])
        with pytest.warns(UserWarning, match="Dropped from Metric 'n'.*datatype"):
            convert_ossie_to_lookml(model)


class TestReviewFollowUps:
    def test_ansi_expression_with_backticks_falls_back_to_bigquery_with_warning(self):
        # Backticks are not ANSI; models in the wild still tag BigQuery SQL as ANSI_SQL.
        model = _model([_dataset("t", [_field("oid", expr="`Order ID`")])])
        with pytest.warns(UserWarning, match="Field 't.oid'.*not valid ANSI_SQL.*read as BIGQUERY"):
            out = convert_ossie_to_lookml(model)
        assert "sql: ${TABLE}.`Order ID` ;;" in out

    def test_skipped_fields_do_not_reserve_names(self):
        ds = _dataset("t", [
            _field("created", datatype="DateTimeTz"),
            {"name": "created_raw", "expression": _ansi("x", "SNOWFLAKE")},
            {"name": "created_year", "expression": _ansi("x", "SNOWFLAKE")},
        ])
        out = _convert(_model([ds]))
        assert "timeframes: [raw, time, date, week, month, quarter, year]" in _block(out, "dimension_group: created")

    def test_dataset_metric_with_unknown_name_warns(self):
        ds = _dataset("orders", [_field("id")], metrics=[_metric("m", "SUM(nonexistent)")])
        with pytest.warns(UserWarning, match="Metric 'orders.m'.*'nonexistent' matches no field"):
            convert_ossie_to_lookml(_model([ds]))

    def test_helper_measures_do_not_take_a_declared_metric_name(self):
        ds = _dataset("t", [_field("id"), _field("amount")], primary_key=["id"], metrics=[
            _metric("a", "SUM(amount) / COUNT(DISTINCT id)"),
            _metric("a_sum_1", "SUM(amount)"),
        ])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = convert_ossie_to_lookml(_model([ds]))
        assert "type: sum" in _block(out, "measure: a_sum_1")
        assert "hidden: yes" not in _block(out, "measure: a_sum_1")
        assert "a_sum_1_measure" not in out

    def test_helper_measures_do_not_take_a_model_metric_name(self):
        out = _convert(_model([ORDERS], metrics=[
            _metric("r", "SUM(orders.amount) / COUNT(DISTINCT orders.order_id)"),
            _metric("r_sum_1", "SUM(orders.amount)"),
        ]))
        assert "hidden: yes" not in _block(out, "measure: r_sum_1")

    def test_dataset_metric_keeps_its_name_over_a_generated_timeframe(self):
        ds = _dataset("t", [_field("created_time", datatype="DateTimeTz"), _field("n")],
                      metrics=[_metric("created_date", "COUNT(*)")])
        out = _convert(_model([ds]))
        assert "measure: created_date {" in out
        assert "created_date_measure" not in out
        # The group keeps its full name rather than expose `created_date`.
        assert "dimension_group: created_time {" in out
        lkml.load(out)

    def test_dataset_metric_named_like_a_timeframe_of_an_unshortened_group_drops_it(self):
        ds = _dataset("t", [_field("created", datatype="DateTimeTz")], metrics=[_metric("created_year", "COUNT(*)")])
        with pytest.warns(UserWarning, match="dropped timeframe 'year'"):
            out = convert_ossie_to_lookml(_model([ds]))
        assert "measure: created_year {" in out
        assert "created_year_measure" not in out
