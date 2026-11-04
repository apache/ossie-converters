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

"""Tests for the LookML to Ossie converter."""

import pytest
import yaml

from ossie_bigquery.lookml_to_ossie import convert_lookml_to_ossie
from ossie_bigquery.ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


def _convert(*views, **kwargs):
    """Converts views given as LookML strings, one file each."""
    files = {f"view_{i}.view.lkml": text for i, text in enumerate(views)}
    return yaml.safe_load(convert_lookml_to_ossie(files, **kwargs))


class TestModel:
    def test_document_header(self):
        model = _convert("view: orders { sql_table_name: analytics.orders ;; }")
        assert model["version"] == SUPPORTED_VERSION
        assert model["name"] == "lookml_model"

    def test_model_name(self):
        model = _convert("view: orders {}", model_name="shop")
        assert model["name"] == "shop"

    def test_empty_input(self):
        with pytest.raises(OssieConversionError, match="non-empty mapping"):
            convert_lookml_to_ossie({})

    def test_invalid_lookml_names_the_file(self):
        with pytest.raises(OssieConversionError, match="'bad.view.lkml'"):
            convert_lookml_to_ossie({"bad.view.lkml": "view: orders {"})

    def test_duplicate_view(self):
        with pytest.raises(OssieConversionError, match="View 'orders' is defined in both"):
            _convert("view: orders {}", "view: orders {}")


class TestViewSource:
    def test_table_name(self):
        model = _convert("view: orders { sql_table_name: analytics.orders ;; }")
        assert model["datasets"] == [{"name": "orders", "source": "analytics.orders"}]

    def test_backticks_are_removed(self):
        model = _convert("view: orders { sql_table_name: `proj.analytics.orders` ;; }")
        assert model["datasets"][0]["source"] == "proj.analytics.orders"

    def test_no_table_uses_view_name(self):
        model = _convert("view: orders {}")
        assert model["datasets"][0]["source"] == "orders"

    def test_derived_table_sql(self):
        model = _convert(
            "view: big_orders { derived_table: { sql: SELECT * FROM orders WHERE total > 100 ;; } }"
        )
        assert model["datasets"][0]["source"] == "SELECT * FROM orders WHERE total > 100"

    def test_native_derived_table_is_skipped(self):
        lookml = "view: summary { derived_table: { explore_source: orders { column: id {} } } }"
        with pytest.warns(UserWarning, match="view 'summary'.*explore_source"):
            model = _convert(lookml)
        assert model["datasets"] == []

    def test_refinement_is_skipped(self):
        with pytest.warns(UserWarning, match="view '\\+orders'.*refinements"):
            model = _convert("view: orders {}", "view: +orders {}")
        assert [d["name"] for d in model["datasets"]] == ["orders"]


def _fields(dimensions, view="orders"):
    """Converts one view with the given dimension LookML; returns its fields."""
    model = _convert(f"view: {view} {{ sql_table_name: analytics.{view} ;; {dimensions} }}")
    return {f["name"]: f for f in model["datasets"][0].get("fields", [])}


def _sql(field):
    [dialect] = field["expression"]["dialects"]
    assert dialect["dialect"] == "BIGQUERY"
    return dialect["expression"]


class TestDimensions:
    def test_table_column(self):
        fields = _fields("dimension: status { type: string sql: ${TABLE}.status ;; }")
        assert fields["status"] == {
            "name": "status",
            "expression": {"dialects": [{"dialect": "BIGQUERY", "expression": "status"}]},
            "datatype": "String",
        }

    def test_no_sql_reads_column_of_same_name(self):
        fields = _fields("dimension: status {}")
        assert _sql(fields["status"]) == "status"

    def test_quoted_column(self):
        fields = _fields("dimension: order_id { sql: ${TABLE}.`order id` ;; }")
        assert _sql(fields["order_id"]) == "`order id`"

    def test_expression(self):
        fields = _fields("dimension: is_big { type: yesno sql: ${TABLE}.total > 100 ;; }")
        assert _sql(fields["is_big"]) == "total > 100"
        assert fields["is_big"]["datatype"] == "Boolean"

    def test_default_type_is_string(self):
        assert _fields("dimension: status {}")["status"]["datatype"] == "String"

    def test_number_has_no_datatype(self):
        fields = _fields("dimension: total { type: number sql: ${TABLE}.total ;; }")
        assert "datatype" not in fields["total"]

    def test_label_and_description(self):
        fields = _fields('dimension: status { label: "Order status" description: "Where it is" }')
        assert fields["status"]["label"] == "Order status"
        assert fields["status"]["description"] == "Where it is"

    def test_primary_key(self):
        model = _convert("view: orders { dimension: id { primary_key: yes } dimension: status {} }")
        assert model["datasets"][0]["primary_key"] == ["id"]


class TestDimensionReferences:
    def test_reference_is_inlined(self):
        fields = _fields(
            "dimension: total { type: number sql: ${TABLE}.price * ${TABLE}.qty ;; }"
            "dimension: total_with_tax { type: number sql: ${total} * 1.2 ;; }"
        )
        assert _sql(fields["total_with_tax"]) == "(price * qty) * 1.2"

    def test_reference_to_plain_column_is_not_parenthesized(self):
        fields = _fields(
            "dimension: first { sql: ${TABLE}.first_name ;; }"
            "dimension: greeting { sql: 'Hi ' || ${first} ;; }"
        )
        assert _sql(fields["greeting"]) == "'Hi ' || first_name"

    def test_chained_references(self):
        fields = _fields(
            "dimension: a { type: number sql: ${TABLE}.x + 1 ;; }"
            "dimension: b { type: number sql: ${a} * 2 ;; }"
            "dimension: c { type: number sql: ${b} - 3 ;; }"
        )
        assert _sql(fields["c"]) == "((x + 1) * 2) - 3"

    def test_own_view_qualified_reference(self):
        fields = _fields(
            "dimension: total { type: number sql: ${TABLE}.total ;; }"
            "dimension: doubled { type: number sql: ${orders.total} * 2 ;; }"
        )
        assert _sql(fields["doubled"]) == "total * 2"

    def test_other_view_reference_is_skipped(self):
        with pytest.warns(UserWarning, match="dimension 'orders.user_name'.*another view"):
            fields = _fields("dimension: user_name { sql: ${users.name} ;; }")
        assert "user_name" not in fields

    def test_unknown_reference_is_skipped(self):
        with pytest.warns(UserWarning, match="'\\$\\{missing\\}'.*not a dimension"):
            fields = _fields("dimension: x { sql: ${missing} ;; }")
        assert fields == {}

    def test_cycle_is_skipped(self):
        with pytest.warns(UserWarning, match="circular reference"):
            fields = _fields(
                "dimension: a { sql: ${b} ;; } dimension: b { sql: ${a} ;; }"
            )
        assert fields == {}


class TestUnsupportedDimensions:
    @pytest.mark.parametrize("lookml_type", ["tier", "location", "distance", "duration"])
    def test_computed_types_are_skipped(self, lookml_type):
        with pytest.warns(UserWarning, match=f"type '{lookml_type}' has no Ossie form"):
            fields = _fields(f"dimension: x {{ type: {lookml_type} sql: ${{TABLE}}.x ;; }}")
        assert fields == {}

    def test_liquid_is_skipped(self):
        with pytest.warns(UserWarning, match="Liquid"):
            fields = _fields("dimension: x { sql: {% if a._in_query %} 1 {% endif %} ;; }")
        assert fields == {}

    def test_bare_table_is_skipped(self):
        with pytest.warns(UserWarning, match="without a column"):
            fields = _fields("dimension: x { sql: ${TABLE} ;; }")
        assert fields == {}

    def test_unparseable_sql_is_skipped(self):
        with pytest.warns(UserWarning, match="could not be parsed"):
            fields = _fields("dimension: x { sql: ${TABLE}.a + ;; }")
        assert fields == {}


class TestDimensionGroups:
    def test_time_group_is_one_field(self):
        fields = _fields(
            "dimension_group: created { type: time timeframes: [raw, date, month] "
            "sql: ${TABLE}.created_at ;; }"
        )
        assert fields["created_at"] == {
            "name": "created_at",
            "expression": {"dialects": [{"dialect": "BIGQUERY", "expression": "created_at"}]},
            "datatype": "DateTimeTz",
        }

    @pytest.mark.parametrize("lookml, ossie", [
        ("date", "Date"),
        ("datetime", "DateTime"),
        ("timestamp", "DateTimeTz"),
    ])
    def test_datatype(self, lookml, ossie):
        fields = _fields(f"dimension_group: created {{ type: time datatype: {lookml} "
                         "sql: ${TABLE}.created ;; }")
        assert fields["created"]["datatype"] == ossie  # column `created`

    @pytest.mark.parametrize("lookml_datatype", ["epoch", "yyyymmdd"])
    def test_numeric_datatype_is_skipped(self, lookml_datatype):
        with pytest.warns(UserWarning, match=f"datatype '{lookml_datatype}' stores a number"):
            fields = _fields(f"dimension_group: created {{ type: time "
                             f"datatype: {lookml_datatype} sql: ${{TABLE}}.c ;; }}")
        assert fields == {}

    def test_duration_is_skipped(self):
        with pytest.warns(UserWarning, match="dimension_group 'orders.open'.*type 'duration'"):
            fields = _fields(
                "dimension_group: open { type: duration intervals: [day] "
                "sql_start: ${TABLE}.opened ;; sql_end: ${TABLE}.closed ;; }"
            )
        assert fields == {}

    def test_label_and_description(self):
        fields = _fields('dimension_group: created { type: time label: "Created" '
                         'description: "When placed" sql: ${TABLE}.c ;; }')
        assert fields["c"]["label"] == "Created"
        assert fields["c"]["description"] == "When placed"

    def test_computed_sql_is_named_raw(self):
        fields = _fields("dimension_group: created { type: time sql: TIMESTAMP(${TABLE}.d) ;; }")
        assert _sql(fields["created_raw"]) == "TIMESTAMP(d)"

    def test_taken_column_name_is_named_raw(self):
        fields = _fields(
            "dimension: created_at { sql: CAST(${TABLE}.created_at AS STRING) ;; }"
            "dimension_group: created { type: time sql: ${TABLE}.created_at ;; }"
        )
        assert set(fields) == {"created_at", "created_raw"}

    def test_two_groups_on_one_column(self):
        fields = _fields(
            "dimension_group: created { type: time sql: ${TABLE}.created_at ;; }"
            "dimension_group: placed { type: time sql: ${TABLE}.created_at ;; }"
        )
        assert set(fields) == {"created_at", "placed_raw"}

    def test_quoted_column_is_named_raw(self):
        fields = _fields("dimension_group: created { type: time sql: ${TABLE}.`created at` ;; }")
        assert set(fields) == {"created_raw"}

    def test_date_time_dimension(self):
        fields = _fields("dimension: shipped { type: date_time sql: ${TABLE}.shipped ;; }")
        assert fields["shipped"]["datatype"] == "DateTimeTz"

    def test_date_dimension_is_skipped(self):
        with pytest.warns(UserWarning, match="type 'date' truncates a timestamp"):
            fields = _fields("dimension: shipped { type: date sql: ${TABLE}.shipped ;; }")
        assert fields == {}


class TestTimeframeReferences:
    def test_raw_timeframe_is_inlined(self):
        fields = _fields(
            "dimension_group: created { type: time sql: ${TABLE}.created_at ;; }"
            "dimension: is_recent { type: yesno sql: ${created_raw} > '2026-01-01' ;; }"
        )
        assert _sql(fields["is_recent"]) == "created_at > '2026-01-01'"

    def test_other_timeframe_is_skipped(self):
        with pytest.warns(UserWarning, match="'month' timeframe of dimension_group 'created'"):
            fields = _fields(
                "dimension_group: created { type: time sql: ${TABLE}.created_at ;; }"
                "dimension: m { sql: ${created_month} ;; }"
            )
        assert "m" not in fields

    def test_longest_group_name_wins(self):
        fields = _fields(
            "dimension_group: created { type: time sql: ${TABLE}.a ;; }"
            "dimension_group: created_at { type: time sql: ${TABLE}.b ;; }"
            "dimension: x { sql: ${created_at_raw} ;; }"
        )
        assert _sql(fields["x"]) == "b"
