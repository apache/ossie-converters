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

import importlib.util
import json
import warnings
from pathlib import Path

import pytest
import yaml

from ossie_bigquery.lookml_to_ossie import convert_lookml_to_ossie
from ossie_bigquery.ossie_to_lookml import (
    SUPPORTED_VERSION,
    OssieConversionError,
    convert_ossie_to_lookml,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OSSIE = Path(__file__).resolve().parents[2] / "ossie"


def _load_validator():
    """The spec's own validator (`ossie/validation/validate.py`), or None
    when it can't load (no jsonschema, submodule not checked out). It exits
    at import time when jsonschema is missing, hence SystemExit."""
    try:
        spec = importlib.util.spec_from_file_location(
            "ossie_validate", OSSIE / "validation" / "validate.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        schema = json.loads((OSSIE / "core-spec" / "ossie-schema.json").read_text())
        return module, schema
    except (Exception, SystemExit):
        return None


_VALIDATOR = _load_validator()


def _assert_valid(ossie_yaml):
    if _VALIDATOR is None:
        pytest.skip("ossie/validation/validate.py unavailable")
    module, schema = _VALIDATOR
    assert module.validate_document(yaml.safe_load(ossie_yaml), schema) == []


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


def _dataset(body, view="orders"):
    model = _convert(f"view: {view} {{ sql_table_name: analytics.{view} ;; {body} }}")
    return model["datasets"][0]


def _metrics(body):
    return {m["name"]: m for m in _dataset(body).get("metrics", [])}


def _metric_sql(metric, dialect="BIGQUERY"):
    [d] = metric["expression"]["dialects"]
    assert d["dialect"] == dialect
    return d["expression"]


class TestMeasures:
    def test_count(self):
        metrics = _metrics("measure: count { type: count }")
        assert metrics["count"] == {
            "name": "count",
            "expression": {"dialects": [{"dialect": "BIGQUERY", "expression": "COUNT(*)"}]},
            "datatype": "Integer",
        }

    @pytest.mark.parametrize("lookml_type, sql", [
        ("sum", "SUM(amount)"),
        ("average", "AVG(amount)"),
        ("min", "MIN(amount)"),
        ("max", "MAX(amount)"),
        ("count_distinct", "COUNT(DISTINCT amount)"),
    ])
    def test_aggregates(self, lookml_type, sql):
        metrics = _metrics(f"dimension: amount {{ type: number }} "
                           f"measure: m {{ type: {lookml_type} sql: ${{TABLE}}.amount ;; }}")
        assert _metric_sql(metrics["m"]) == sql

    def test_count_distinct_is_integer(self):
        metrics = _metrics("dimension: user_id {} "
                           "measure: users { type: count_distinct sql: ${user_id} ;; }")
        assert metrics["users"]["datatype"] == "Integer"

    def test_sum_has_no_datatype(self):
        metrics = _metrics("dimension: amount { type: number } "
                           "measure: total { type: sum sql: ${amount} ;; }")
        assert "datatype" not in metrics["total"]

    def test_dimension_reference_is_the_field_name(self):
        metrics = _metrics(
            "dimension: line_total { type: number sql: ${TABLE}.price * ${TABLE}.qty ;; }"
            "measure: revenue { type: sum sql: ${line_total} ;; }"
        )
        assert _metric_sql(metrics["revenue"]) == "SUM(line_total)"

    def test_expression_over_fields(self):
        metrics = _metrics(
            "dimension: price { type: number } dimension: qty { type: number }"
            "measure: revenue { type: sum sql: ${price} * ${qty} ;; }"
        )
        assert _metric_sql(metrics["revenue"]) == "SUM(price * qty)"

    def test_time_group_raw_reference(self):
        metrics = _metrics(
            "dimension_group: created { type: time sql: ${TABLE}.created_at ;; }"
            "measure: first_order { type: min sql: ${created_raw} ;; }"
        )
        assert _metric_sql(metrics["first_order"]) == "MIN(created_at)"

    @pytest.mark.parametrize("lookml, key, name", [
        ("dimension: status {", "fields", "status"),
        ("dimension_group: created { type: time sql: ${TABLE}.c ;;", "fields", "c"),
        ("measure: count { type: count", "metrics", "count"),
    ])
    def test_label_and_description(self, lookml, key, name):
        dataset = _dataset(f'{lookml} label: "Shown" description: "Explained" }}')
        [entry] = [e for e in dataset[key] if e["name"] == name]
        assert entry["description"] == "Explained"
        # Ossie metrics have no label.
        assert entry.get("label") == ("Shown" if key == "fields" else None)


class TestMeasureColumns:
    def test_column_without_dimension_gets_a_field(self):
        dataset = _dataset("measure: total { type: sum sql: ${TABLE}.amount ;; }")
        assert _metric_sql(dataset["metrics"][0]) == "SUM(amount)"
        assert dataset["fields"] == [{
            "name": "amount",
            "expression": {"dialects": [{"dialect": "BIGQUERY", "expression": "amount"}]},
        }]

    def test_field_named_after_measure_gets_suffix(self):
        dataset = _dataset("measure: revenue { type: sum sql: ${TABLE}.revenue ;; }")
        assert _metric_sql(dataset["metrics"][0]) == "SUM(revenue_value)"
        assert [f["name"] for f in dataset["fields"]] == ["revenue_value"]

    def test_existing_dimension_is_reused(self):
        dataset = _dataset("dimension: amount { type: number sql: ${TABLE}.amount ;; }"
                           "measure: total { type: sum sql: ${TABLE}.amount ;; }")
        assert [f["name"] for f in dataset["fields"]] == ["amount"]

    def test_column_is_added_once(self):
        dataset = _dataset("measure: total { type: sum sql: ${TABLE}.amount ;; }"
                           "measure: biggest { type: max sql: ${TABLE}.amount ;; }")
        assert [f["name"] for f in dataset["fields"]] == ["amount"]

    def test_skipped_measure_adds_no_field(self):
        with pytest.warns(UserWarning, match="measure 'orders.share'"):
            dataset = _dataset("measure: share { type: percent_of_total sql: ${TABLE}.amount ;; }")
        assert "fields" not in dataset


class TestNumberMeasures:
    def test_measures_are_inlined(self):
        metrics = _metrics(
            "dimension: amount { type: number }"
            "measure: total { type: sum sql: ${amount} ;; }"
            "measure: count { type: count }"
            "measure: avg_order { type: number sql: ${total} / NULLIF(${count}, 0) ;; }"
        )
        assert _metric_sql(metrics["avg_order"]) == "SUM(amount) / NULLIF(COUNT(*), 0)"

    def test_nested_number_measure_is_parenthesized(self):
        metrics = _metrics(
            "measure: count { type: count }"
            "measure: doubled { type: number sql: ${count} + ${count} ;; }"
            "measure: tripled { type: number sql: ${doubled} * 3 ;; }"
        )
        assert _metric_sql(metrics["tripled"]) == "(COUNT(*) + COUNT(*)) * 3"

    def test_cycle_is_skipped(self):
        with pytest.warns(UserWarning, match="circular reference"):
            metrics = _metrics("measure: a { type: number sql: ${b} ;; }"
                               "measure: b { type: number sql: ${a} ;; }")
        assert metrics == {}

    def test_measure_inside_aggregate_is_skipped(self):
        with pytest.warns(UserWarning, match="measure inside an aggregate"):
            metrics = _metrics("measure: count { type: count }"
                               "measure: bad { type: sum sql: ${count} ;; }")
        assert set(metrics) == {"count"}


class TestUnsupportedMeasures:
    @pytest.mark.parametrize("lookml_type", [
        "percent_of_total", "running_total", "median", "sum_distinct", "list", "string",
    ])
    def test_type_is_skipped(self, lookml_type):
        with pytest.warns(UserWarning, match=f"type '{lookml_type}' has no Ossie form"):
            metrics = _metrics(f"measure: m {{ type: {lookml_type} sql: ${{TABLE}}.x ;; }}")
        assert metrics == {}

    def test_other_view_is_skipped(self):
        with pytest.warns(UserWarning, match="reads view 'users'.*without an explore"):
            metrics = _metrics("measure: m { type: sum sql: ${users.age} ;; }")
        assert metrics == {}

    def test_skipped_dimension_reference(self):
        with pytest.warns(UserWarning) as record:
            metrics = _metrics("dimension: t { type: tier sql: ${TABLE}.x ;; }"
                               "measure: m { type: sum sql: ${t} ;; }")
        assert any("'${t}', which was skipped" in str(w.message) for w in record)
        assert metrics == {}


def _filtered(filters, dims="dimension: status {} dimension: total { type: number } "
                            "dimension: is_big { type: yesno sql: ${total} > 100 ;; }"):
    metrics = _metrics(f"{dims} measure: m {{ type: count filters: [{filters}] }}")
    return _metric_sql(metrics["m"], "OSSIE_SQL_2026")


class TestFilters:
    def test_string_value(self):
        assert _filtered('status: "complete"') == "COUNT(*) FILTER(WHERE status = 'complete')"

    def test_string_list(self):
        assert _filtered('status: "complete,shipped"') == (
            "COUNT(*) FILTER(WHERE status IN ('complete', 'shipped'))"
        )

    def test_wildcard_is_like(self):
        assert _filtered('status: "in_%"') == "COUNT(*) FILTER(WHERE status LIKE 'in_%')"

    def test_quote_is_escaped(self):
        assert _filtered('status: "it\'s"') == "COUNT(*) FILTER(WHERE status = 'it''s')"

    @pytest.mark.parametrize("value, condition", [
        ("100", "total = 100"),
        (">100", "total > 100"),
        (">=1.5", "total >= 1.5"),
        ("<0", "total < 0"),
        ("1,2,3", "total IN (1, 2, 3)"),
    ])
    def test_number(self, value, condition):
        assert _filtered(f'total: "{value}"') == f"COUNT(*) FILTER(WHERE {condition})"

    def test_yes(self):
        assert _filtered('is_big: "Yes"') == "COUNT(*) FILTER(WHERE is_big)"

    def test_no_includes_null(self):
        assert _filtered('is_big: "No"') == (
            "COUNT(*) FILTER(WHERE NOT COALESCE(is_big, FALSE))"
        )

    def test_null(self):
        assert _filtered('status: "NULL"') == "COUNT(*) FILTER(WHERE status IS NULL)"
        assert _filtered('status: "-NULL"') == "COUNT(*) FILTER(WHERE NOT status IS NULL)"

    def test_several_filters_are_anded(self):
        assert _filtered('status: "complete", total: ">100"') == (
            "COUNT(*) FILTER(WHERE status = 'complete' AND total > 100)"
        )

    def test_aggregate_over_field(self):
        metrics = _metrics("dimension: status {} dimension: amount { type: number }"
                           'measure: m { type: sum sql: ${amount} ;; filters: [status: "complete"] }')
        assert _metric_sql(metrics["m"], "OSSIE_SQL_2026") == (
            "SUM(amount) FILTER(WHERE status = 'complete')"
        )

    def test_old_filter_syntax(self):
        metrics = _metrics('dimension: status {} '
                           'measure: m { type: count filters: { field: status value: "complete" } }')
        assert _metric_sql(metrics["m"], "OSSIE_SQL_2026") == (
            "COUNT(*) FILTER(WHERE status = 'complete')"
        )

    def test_number_measure_over_filtered_measure(self):
        metrics = _metrics(
            'dimension: status {} measure: done { type: count filters: [status: "complete"] }'
            "measure: count { type: count }"
            "measure: done_share { type: number sql: ${done} / ${count} ;; }"
        )
        assert _metric_sql(metrics["done_share"], "OSSIE_SQL_2026") == (
            "(COUNT(*) FILTER(WHERE status = 'complete')) / COUNT(*)"
        )

    @pytest.mark.parametrize("filters", [
        'status: "-cancelled"',       # negation: NULL handling undocumented
        'status: "EMPTY"',
        'status: "a^_b"',             # escape
        'status: "a%,b"',             # wildcard in a list
        'total: "[1, 10]"',           # range
        'total: "NOT 5"',
        'is_big: "maybe"',
        'created_date: "7 days"',     # date filter
        'missing: "x"',
    ])
    def test_untranslatable_filter_skips_the_measure(self, filters):
        with pytest.warns(UserWarning, match="measure 'orders.m'"):
            metrics = _metrics(
                "dimension: status {} dimension: total { type: number } "
                "dimension: is_big { type: yesno sql: ${total} > 100 ;; } "
                "dimension_group: created { type: time sql: ${TABLE}.created_at ;; } "
                f"measure: m {{ type: count filters: [{filters}] }}"
            )
        assert "m" not in metrics

    def test_filters_on_number_measure_are_skipped(self):
        with pytest.warns(UserWarning, match="filters on a type 'number' measure"):
            metrics = _metrics('dimension: status {} measure: count { type: count }'
                               'measure: m { type: number sql: ${count} ;; filters: [status: "a"] }')
        assert "m" not in metrics


_VIEWS = {
    "orders.view.lkml": """
        view: orders {
          sql_table_name: shop.orders ;;
          dimension: id { primary_key: yes type: number }
          dimension: user_id { type: number }
          dimension: product_id { type: number }
          dimension: seller_id { type: number }
          dimension_group: created { type: time sql: ${TABLE}.created_at ;; }
        }""",
    "users.view.lkml": """
        view: users {
          sql_table_name: shop.users ;;
          dimension: id { primary_key: yes type: number }
          dimension: country {}
        }""",
    "products.view.lkml": """
        view: products {
          sql_table_name: shop.products ;;
          dimension: id { primary_key: yes type: number }
        }""",
    "events.view.lkml": """
        view: events {
          sql_table_name: shop.events ;;
          dimension: order_id { type: number }
          dimension_group: at { type: time sql: ${TABLE}.event_at ;; }
        }""",
}


def _explore(explore_lookml, **kwargs):
    files = dict(_VIEWS, **{"shop.model.lkml": 'include: "*.view"\n' + explore_lookml})
    return yaml.safe_load(convert_lookml_to_ossie(files, **kwargs))


def _rels(model):
    return {r["name"]: {k: v for k, v in r.items() if k != "name"}
            for r in model.get("relationships", [])}


class TestExplores:
    def test_explore_is_the_model(self):
        model = _explore("""explore: orders {
            description: "Orders with buyers"
            join: users { sql_on: ${orders.user_id} = ${users.id} ;; relationship: many_to_one }
        }""")
        assert model["name"] == "orders"
        assert model["description"] == "Orders with buyers"
        assert [d["name"] for d in model["datasets"]] == ["orders", "users"]

    def test_several_explores_need_a_choice(self):
        with pytest.raises(OssieConversionError,
                           match="choose one to convert \\(orders, users\\), or merge"):
            _explore("explore: orders {} explore: users {}")

    def test_chosen_explore(self):
        model = _explore("explore: orders {} explore: users {}", explore="users")
        assert [d["name"] for d in model["datasets"]] == ["users"]

    def test_unknown_explore(self):
        with pytest.raises(OssieConversionError, match="Explore 'nope' not found"):
            _explore("explore: orders {}", explore="nope")

    def test_missing_view(self):
        with pytest.raises(OssieConversionError, match="view 'ghosts' is not defined in or included by"):
            _explore("explore: orders { join: ghosts { sql_on: ${orders.id} = ${ghosts.id} ;; } }")

    def test_explore_from(self):
        model = _explore("explore: buyers { from: users }")
        assert model["datasets"][0]["name"] == "buyers"
        assert model["datasets"][0]["source"] == "shop.users"

    def test_explore_view_name(self):
        model = _explore("explore: sales { view_name: orders }")
        assert model["name"] == "sales"
        assert [d["name"] for d in model["datasets"]] == ["orders"]

    def test_explore_filter_warns(self):
        with pytest.warns(UserWarning, match="sql_always_where has no Ossie form"):
            _explore("explore: orders { sql_always_where: ${orders.id} > 0 ;; }")


class TestJoins:
    def test_many_to_one(self):
        model = _explore("explore: orders { join: users { "
                         "sql_on: ${orders.user_id} = ${users.id} ;; relationship: many_to_one } }")
        assert _rels(model) == {"orders_to_users": {
            "from": "orders", "to": "users", "from_columns": ["user_id"], "to_columns": ["id"],
        }}

    def test_default_relationship_is_many_to_one(self):
        model = _explore("explore: orders { join: users { sql_on: ${users.id} = ${orders.user_id} ;; } }")
        assert _rels(model)["orders_to_users"]["from"] == "orders"

    def test_one_to_many_points_from_the_joined_view(self):
        model = _explore("explore: orders { join: events { "
                         "sql_on: ${orders.id} = ${events.order_id} ;; relationship: one_to_many } }")
        assert _rels(model) == {"events_to_orders": {
            "from": "events", "to": "orders", "from_columns": ["order_id"], "to_columns": ["id"],
        }}

    def test_one_to_one(self):
        model = _explore("explore: orders { join: users { "
                         "sql_on: ${orders.user_id} = ${users.id} ;; relationship: one_to_one } }")
        assert _rels(model)["orders_to_users"]["from"] == "orders"

    def test_composite_key(self):
        model = _explore("explore: orders { join: users { sql_on: "
                         "${orders.user_id} = ${users.id} AND ${orders.seller_id} = ${users.id} ;; } }")
        assert _rels(model)["orders_to_users"]["from_columns"] == ["user_id", "seller_id"]

    def test_alias(self):
        model = _explore("explore: orders {"
                         " join: buyer { from: users sql_on: ${orders.user_id} = ${buyer.id} ;; }"
                         " join: seller { from: users sql_on: ${orders.seller_id} = ${seller.id} ;; } }")
        assert [d["name"] for d in model["datasets"]] == ["orders", "buyer", "seller"]
        assert set(_rels(model)) == {"orders_to_buyer", "orders_to_seller"}

    def test_foreign_key(self):
        model = _explore("explore: orders { join: products { foreign_key: product_id } }")
        assert _rels(model) == {"orders_to_products": {
            "from": "orders", "to": "products", "from_columns": ["product_id"], "to_columns": ["id"],
        }}

    def test_chained_join(self):
        model = _explore("explore: events {"
                         " join: orders { sql_on: ${events.order_id} = ${orders.id} ;; }"
                         " join: users { sql_on: ${orders.user_id} = ${users.id} ;; } }")
        assert set(_rels(model)) == {"events_to_orders", "orders_to_users"}

    def test_time_group_key_uses_its_field(self):
        model = _explore("explore: orders { join: events { relationship: one_to_many "
                         "sql_on: ${orders.created_raw} = ${events.at_raw} ;; } }")
        rel = _rels(model)["events_to_orders"]
        assert rel["from_columns"] == ["event_at"] and rel["to_columns"] == ["created_at"]

    def test_inner_join_warns_and_keeps_keys(self):
        with pytest.warns(UserWarning, match="join 'orders.users'.*type 'inner' has no Ossie form"):
            model = _explore("explore: orders { join: users { type: inner "
                             "sql_on: ${orders.user_id} = ${users.id} ;; } }")
        assert "orders_to_users" in _rels(model)

    @pytest.mark.parametrize("join, reason", [
        ("relationship: many_to_many sql_on: ${orders.user_id} = ${users.id} ;;",
         "many_to_many"),
        ("sql_on: ${orders.user_id} > ${users.id} ;;", "not equalities"),
        ("sql_on: ${orders.user_id} = ${users.id} AND ${users.country} = 'NL' ;;",
         "not equalities"),
        ("sql_on: ${orders.created_date} = ${users.id} ;;", "not a converted dimension"),
        ("type: cross", "no sql_on or foreign_key"),
    ])
    def test_unsupported_join_keeps_the_dataset(self, join, reason):
        with pytest.warns(UserWarning, match=f"join 'orders.users'.*{reason}"):
            model = _explore(f"explore: orders {{ join: users {{ {join} }} }}")
        assert [d["name"] for d in model["datasets"]] == ["orders", "users"]
        assert _rels(model) == {}


def _shop():
    return {p.name: p.read_text() for p in (FIXTURES / "lookml_shop").glob("*.lkml")}


def _metric_names(ossie_yaml):
    doc = yaml.safe_load(ossie_yaml)
    return ({m["name"] for m in doc.get("metrics", [])}
            | {m["name"] for d in doc["datasets"] for m in d.get("metrics", [])})


def _shape(ossie_yaml):
    """What a round trip must keep: datasets with their field and metric
    names, and the relationships' keys."""
    doc = yaml.safe_load(ossie_yaml)
    datasets = {
        d["name"]: ({f["name"] for f in d.get("fields", [])},
                    {m["name"] for m in d.get("metrics", [])})
        for d in doc["datasets"]
    }
    relationships = {
        (r["from"], r["to"], tuple(r["from_columns"]), tuple(r["to_columns"]))
        for r in doc.get("relationships", [])
    }
    return datasets, relationships


class TestProject:
    def test_shop_converts_without_warnings_and_validates(self, recwarn):
        ossie_yaml = convert_lookml_to_ossie(_shop(), explore="order_items")
        assert [str(w.message) for w in recwarn] == []
        _assert_valid(ossie_yaml)
        datasets, relationships = _shape(ossie_yaml)
        assert set(datasets) == {"order_items", "orders", "users", "products"}
        assert relationships == {
            ("order_items", "orders", ("order_id",), ("id",)),
            ("order_items", "products", ("product_id",), ("id",)),
            ("orders", "users", ("user_id",), ("id",)),
        }

    def test_round_trip_through_ossie_to_lookml(self):
        first = convert_lookml_to_ossie(_shop(), explore="order_items")
        with pytest.warns(UserWarning):  # the exporter notes what LookML can't hold
            lookml = convert_ossie_to_lookml(first)
        second = convert_lookml_to_ossie({"shop.lkml": lookml}, explore="order_items")
        before, after = _shape(first), _shape(second)
        assert after[1] == before[1]
        for name, (fields, metrics) in before[0].items():
            assert after[0][name][0] == fields
            # The exporter splits type: number arithmetic into hidden helper
            # measures, which come back as metrics of their own.
            assert metrics <= after[0][name][1]

    @pytest.mark.skipif(not (OSSIE / "examples" / "tpcds_semantic_model.yaml").exists(),
                        reason="TPC-DS example not found")
    def test_tpcds_round_trip(self):
        """The spec's TPC-DS example, exported with ossie_to_lookml and imported
        back without warnings, keeps every dataset, field, relationship and
        metric."""
        original = (OSSIE / "examples" / "tpcds_semantic_model.yaml").read_text()
        with pytest.warns(UserWarning):
            lookml = convert_ossie_to_lookml(original)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            imported = convert_lookml_to_ossie({"tpcds.lkml": lookml})
        _assert_valid(imported)
        before, after = _shape(original), _shape(imported)
        assert _metric_names(original) <= _metric_names(imported)
        assert after[1] == before[1]
        assert set(after[0]) == set(before[0])
        for name, (fields, _) in before[0].items():
            # The exporter adds a hidden `primary_key` dimension for
            # composite keys, which comes back as a field.
            assert after[0][name][0] - {"primary_key"} == fields


def _project(files, **kwargs):
    return yaml.safe_load(convert_lookml_to_ossie(files, **kwargs))


def _sources(model):
    return {d["name"]: d["source"] for d in model["datasets"]}


class TestIncludes:
    def test_same_view_in_two_files_is_fine_when_one_is_included(self):
        model = _project({
            "shop.model.lkml": 'include: "/views/*.view.lkml" explore: customers {}',
            "views/customers.view.lkml": "view: customers { sql_table_name: shop.customers ;; }",
            "legacy/customers.view.lkml": "view: customers { sql_table_name: old.customers ;; }",
        })
        assert _sources(model) == {"customers": "shop.customers"}

    def test_both_included_is_an_error(self):
        with pytest.raises(OssieConversionError, match="View 'customers' is defined in both"):
            _project({
                "shop.model.lkml": 'include: "/**/*.view" explore: customers {}',
                "views/customers.view.lkml": "view: customers {}",
                "legacy/customers.view.lkml": "view: customers {}",
            })

    def test_relative_include_starts_at_the_including_file(self):
        model = _project({
            "models/shop.model.lkml": 'include: "../views/a.view" explore: a {}',
            "views/a.view.lkml": "view: a { sql_table_name: s.a ;; }",
        })
        assert _sources(model) == {"a": "s.a"}

    def test_double_star_crosses_folders(self):
        model = _project({
            "shop.model.lkml": 'include: "/views/**/*.view" explore: a { join: b { foreign_key: id } }',
            "views/a.view.lkml": "view: a { dimension: id { primary_key: yes } }",
            "views/deep/er/b.view.lkml": "view: b { dimension: id { primary_key: yes } }",
        })
        assert set(_sources(model)) == {"a", "b"}

    def test_explore_file_and_nested_includes(self):
        model = _project({
            "shop.model.lkml": 'include: "/explores/*.explore.lkml"',
            "explores/orders.explore.lkml": 'include: "/views/orders.view" explore: orders {}',
            "views/orders.view.lkml": "view: orders { sql_table_name: s.orders ;; }",
        })
        assert model["name"] == "orders"
        assert _sources(model) == {"orders": "s.orders"}

    def test_views_outside_the_model_are_not_converted(self):
        model = _project({
            "shop.model.lkml": 'include: "a.view" explore: a {}',
            "a.view.lkml": "view: a {}",
            "unused.view.lkml": "view: unused {}",
        })
        assert set(_sources(model)) == {"a"}

    def test_explore_in_two_models_needs_a_model(self):
        files = {
            "one.model.lkml": 'include: "/views/*.view" include: "orders.explore.lkml"',
            "two.model.lkml": 'include: "/views/*.view" include: "orders.explore.lkml"',
            "orders.explore.lkml": "explore: orders {}",
            "views/orders.view.lkml": "view: orders {}",
        }
        with pytest.raises(OssieConversionError, match="in models one, two; choose one with model="):
            _project(files, explore="orders")
        assert _project(files, explore="orders", model="two")["name"] == "orders"

    def test_unknown_model(self):
        with pytest.raises(OssieConversionError, match="Model 'nope' not found. Models: shop"):
            _project({"shop.model.lkml": "explore: a {}"}, model="nope")

    def test_include_matching_nothing_warns(self):
        with pytest.warns(UserWarning, match="include '/missing/\\*.view' matches no files"):
            _project({"shop.model.lkml": 'include: "/missing/*.view" include: "a.view" explore: a {}',
                      "a.view.lkml": "view: a {}"})

    def test_dashboard_include_is_ignored(self, recwarn):
        _project({"shop.model.lkml": 'include: "/dashboards/*.dashboard.lookml" '
                                     'include: "a.view" explore: a {}',
                  "a.view.lkml": "view: a {}"})
        assert [str(w.message) for w in recwarn] == []

    def test_other_project_include_warns(self):
        with pytest.warns(UserWarning, match="another project"):
            _project({"shop.model.lkml": 'include: "//lib/x.view" include: "a.view" explore: a {}',
                      "a.view.lkml": "view: a {}"})

    def test_unincluded_file_is_not_parsed(self):
        model = _project({
            "shop.model.lkml": 'include: "a.view" explore: a {}',
            "a.view.lkml": "view: a {}",
            "broken.view.lkml": "view: broken {",
        })
        assert set(_sources(model)) == {"a"}


class TestRefinements:
    def test_adds_a_field(self):
        fields = _fields_of(_convert(
            "view: orders { dimension: id {} }",
            "view: +orders { dimension: status {} }",
        ))
        assert set(fields) == {"id", "status"}

    def test_overrides_field_parameters_and_keeps_the_rest(self):
        fields = _fields_of(_convert(
            'view: orders { dimension: total { type: number sql: ${TABLE}.amount ;; } }',
            'view: +orders { dimension: total { label: "Total" } }',
        ))
        assert _sql(fields["total"]) == "amount"
        assert fields["total"]["label"] == "Total"

    def test_overrides_view_parameters(self):
        model = _convert("view: orders { sql_table_name: old.orders ;; }",
                         "view: +orders { sql_table_name: new.orders ;; }")
        assert model["datasets"][0]["source"] == "new.orders"

    def test_later_refinement_wins(self):
        model = _convert("view: orders {}",
                         "view: +orders { sql_table_name: a.orders ;; }",
                         "view: +orders { sql_table_name: b.orders ;; }")
        assert model["datasets"][0]["source"] == "b.orders"

    def test_refinement_before_its_view(self):
        model = _convert("view: +orders { sql_table_name: new.orders ;; }",
                         "view: orders { sql_table_name: old.orders ;; }")
        assert model["datasets"][0]["source"] == "new.orders"

    def test_explore_refinement_adds_a_join(self):
        model = _project({
            "shop.model.lkml": 'include: "*.view" explore: orders {}',
            "orders.view.lkml": "view: orders { dimension: user_id {} }",
            "users.view.lkml": "view: users { dimension: id { primary_key: yes } }",
            "more.view.lkml": "explore: +orders { join: users { foreign_key: user_id } }",
        })
        assert [r["to"] for r in model["relationships"]] == ["users"]

    def test_refined_field_can_be_a_join_key(self):
        model = _project({
            "shop.model.lkml": 'include: "*.view" '
                               'explore: orders { join: users { sql_on: ${orders.user_id} = ${users.id} ;; } }',
            "orders.view.lkml": "view: orders {}",
            "orders_rfn.view.lkml": "view: +orders { dimension: user_id {} }",
            "users.view.lkml": "view: users { dimension: id {} }",
        })
        assert model["relationships"][0]["from_columns"] == ["user_id"]

    def test_refinement_of_unknown_view_warns(self):
        with pytest.warns(UserWarning, match="view '\\+ghosts'.*does not define or include"):
            _convert("view: orders {}", "view: +ghosts {}")

    def test_refinement_outside_the_model_is_not_applied(self):
        model = _project({
            "shop.model.lkml": 'include: "orders.view" explore: orders {}',
            "orders.view.lkml": "view: orders { sql_table_name: s.orders ;; }",
            "other.view.lkml": "view: +orders { sql_table_name: x.orders ;; }",
        })
        assert _sources(model) == {"orders": "s.orders"}


def _fields_of(model):
    return {f["name"]: f for f in model["datasets"][0].get("fields", [])}


class TestExtends:
    def test_inherits_fields_and_table(self):
        model = _convert(
            "view: base { extension: required sql_table_name: s.orders ;; dimension: id {} }",
            "view: orders { extends: [base] dimension: status {} }",
        )
        [dataset] = model["datasets"]
        assert dataset["name"] == "orders"
        assert dataset["source"] == "s.orders"
        assert {f["name"] for f in dataset["fields"]} == {"id", "status"}

    def test_own_definition_overrides(self):
        fields = _fields_of(_convert(
            "view: base { extension: required "
            "dimension: total { type: number sql: ${TABLE}.a ;; } }",
            'view: orders { extends: [base] dimension: total { sql: ${TABLE}.b ;; } }',
        ))
        assert _sql(fields["total"]) == "b"
        assert "datatype" not in fields["total"]  # type: number inherited

    def test_later_parent_wins(self):
        model = _convert(
            "view: a { extension: required sql_table_name: s.a ;; }",
            "view: b { extension: required sql_table_name: s.b ;; }",
            "view: c { extends: [a, b] }",
        )
        assert model["datasets"][0]["source"] == "s.b"

    def test_chain(self):
        model = _convert(
            "view: a { extension: required dimension: x {} }",
            "view: b { extends: [a] extension: required dimension: y {} }",
            "view: c { extends: [b] dimension: z {} }",
        )
        assert set(_fields_of(model)) == {"x", "y", "z"}

    def test_parent_used_directly_is_a_dataset_too(self):
        model = _convert("view: users { sql_table_name: s.users ;; }",
                         "view: buyers { extends: [users] }")
        assert {d["name"]: d["source"] for d in model["datasets"]} == {
            "users": "s.users", "buyers": "s.users",
        }

    def test_refinement_can_add_extends(self):
        model = _convert(
            "view: base { extension: required dimension: x {} }",
            "view: orders { dimension: y {} }",
            "view: +orders { extends: [base] }",
        )
        assert set(_fields_of(model)) == {"x", "y"}

    def test_refinement_of_parent_is_inherited(self):
        model = _convert(
            "view: base { extension: required }",
            "view: +base { dimension: x {} }",
            "view: orders { extends: [base] }",
        )
        assert set(_fields_of(model)) == {"x"}

    def test_explore_extends(self):
        model = _project({
            "shop.model.lkml": 'include: "*.view" '
                               "explore: base { extension: required "
                               "join: users { foreign_key: user_id } } "
                               "explore: orders { extends: [base] }",
            "orders.view.lkml": "view: orders { dimension: user_id {} }",
            "users.view.lkml": "view: users { dimension: id { primary_key: yes } }",
        })
        assert model["name"] == "orders"
        assert [r["to"] for r in model["relationships"]] == ["users"]

    def test_missing_parent_warns(self):
        with pytest.warns(UserWarning, match="view 'orders'.*extends 'ghost'.*fields are missing"):
            _convert("view: orders { extends: [ghost] }")

    def test_cycle_warns(self):
        with pytest.warns(UserWarning, match="in a cycle"):
            _convert("view: a { extends: [b] }", "view: b { extends: [a] }")


_CROSS = {
    "shop.model.lkml": 'include: "*.view" explore: orders { '
                       "join: users { sql_on: ${orders.user_id} = ${users.id} ;; } }",
    "users.view.lkml": "view: users { sql_table_name: s.users ;; "
                       "dimension: id { primary_key: yes type: number } "
                       "dimension: age { type: number } dimension: country {} "
                       "measure: count { type: count } "
                       "measure: total_age { type: sum sql: ${age} ;; } }",
}


def _cross(measures, user_dims=""):
    files = dict(_CROSS)
    files["orders.view.lkml"] = (
        "view: orders { sql_table_name: s.orders ;; dimension: id { primary_key: yes } "
        f"dimension: user_id {{}} dimension: status {{}} {measures} }}"
    )
    model = yaml.safe_load(convert_lookml_to_ossie(files))
    return model, {m["name"]: m for m in model.get("metrics", [])}


class TestModelScopedMetrics:
    def test_measure_reading_another_view(self):
        model, metrics = _cross("measure: buyer_age { type: average sql: ${users.age} ;; }")
        assert _metric_sql(metrics["buyer_age"]) == "AVG(users.age)"
        assert "metrics" not in model["datasets"][0]
        _assert_valid(yaml.safe_dump(model))

    def test_own_fields_are_qualified(self):
        _, metrics = _cross(
            "measure: m { type: count_distinct sql: ${user_id} || ${users.country} ;; }")
        assert _metric_sql(metrics["m"]) == "COUNT(DISTINCT orders.user_id || users.country)"

    def test_number_inlines_measures_of_both_views(self):
        _, metrics = _cross("measure: count { type: count } "
                            "measure: per_user { type: number sql: ${count} / ${users.count} ;; }")
        assert _metric_sql(metrics["per_user"]) == (
            "COUNT(DISTINCT orders.id) / COUNT(DISTINCT users.id)"
        )

    def test_dataset_scoped_measures_stay_in_their_dataset(self):
        model, metrics = _cross("measure: count { type: count } "
                                "measure: buyer_age { type: sum sql: ${users.age} ;; }")
        assert [m["name"] for m in model["datasets"][0]["metrics"]] == ["count"]
        assert set(metrics) == {"buyer_age"}

    def test_filter_on_the_aggregated_view(self):
        _, metrics = _cross('measure: dutch_age { type: sum sql: ${users.age} ;; '
                            'filters: [users.country: "NL"] }')
        assert _metric_sql(metrics["dutch_age"], "OSSIE_SQL_2026") == (
            "SUM(users.age) FILTER(WHERE users.country = 'NL')"
        )

    def test_count_filtered_on_another_view_is_skipped(self):
        # Ossie's FILTER reads only the aggregate's dataset.
        with pytest.warns(UserWarning, match="filters and aggregate read different views"):
            _, metrics = _cross('measure: dutch { type: count filters: [users.country: "NL"] }')
        assert metrics == {}

    def test_count_without_primary_key_is_skipped(self):
        files = dict(_CROSS)
        files["orders.view.lkml"] = (
            "view: orders { dimension: user_id {} "
            "measure: count { type: count } "
            "measure: per_user { type: number sql: ${count} / ${users.count} ;; } }"
        )
        with pytest.warns(UserWarning, match="count of view 'orders'.*needs its primary key"):
            model = yaml.safe_load(convert_lookml_to_ossie(files))
        assert "metrics" not in model

    def test_view_outside_the_explore_is_skipped(self):
        with pytest.warns(UserWarning, match="reads view 'products', which the explore does not join"):
            _, metrics = _cross("measure: m { type: sum sql: ${products.price} ;; }")
        assert metrics == {}

    def test_name_clash_gets_the_view_prefix(self):
        files = dict(_CROSS)
        files["users.view.lkml"] = _CROSS["users.view.lkml"].replace(
            "measure: count", "measure: mix { type: sum sql: ${orders.id} ;; } measure: count")
        files["orders.view.lkml"] = (
            "view: orders { dimension: id { primary_key: yes } dimension: user_id {} "
            "measure: mix { type: sum sql: ${users.age} ;; } }"
        )
        model = yaml.safe_load(convert_lookml_to_ossie(files))
        assert {m["name"] for m in model["metrics"]} == {"mix", "users_mix"}


class TestAllExplores:
    def test_merges_datasets_and_relationships(self):
        model = _explore("explore: orders { join: users { foreign_key: user_id } }"
                         "explore: events { join: orders { sql_on: ${events.order_id} = ${orders.id} ;; }"
                         " join: users { sql_on: ${orders.user_id} = ${users.id} ;; } }",
                         all_explores=True)
        assert model["name"] == "lookml_model"
        assert [d["name"] for d in model["datasets"]] == ["orders", "users", "events"]
        assert _rels(model) == {
            "orders_to_users": {"from": "orders", "to": "users",
                                "from_columns": ["user_id"], "to_columns": ["id"]},
            "events_to_orders": {"from": "events", "to": "orders",
                                 "from_columns": ["order_id"], "to_columns": ["id"]},
        }

    def test_alias_for_different_views_keeps_the_first(self):
        with pytest.warns(UserWarning, match="'buyer' is a different view"):
            model = _explore("explore: orders { join: buyer { from: users foreign_key: user_id } }"
                             "explore: events { join: buyer { from: products "
                             "sql_on: ${events.order_id} = ${buyer.id} ;; } }",
                             all_explores=True)
        assert {d["name"]: d["source"] for d in model["datasets"]}["buyer"] == "shop.users"

    def test_not_with_an_explore(self):
        with pytest.raises(OssieConversionError, match="not both"):
            _explore("explore: orders {}", explore="orders", all_explores=True)

    def test_round_trips_a_whole_exported_model(self):
        first = convert_lookml_to_ossie(_shop(), all_explores=True)
        with pytest.warns(UserWarning):
            lookml = convert_ossie_to_lookml(first)
        second = convert_lookml_to_ossie({"m.lkml": lookml}, all_explores=True)
        assert _shape(second)[1] == _shape(first)[1]
        assert set(_shape(second)[0]) == set(_shape(first)[0])

    def test_explores_in_several_models_need_a_model(self):
        with pytest.raises(OssieConversionError, match="Explores are in models one, two"):
            _project({"one.model.lkml": 'include: "a.view" explore: a {}',
                      "two.model.lkml": 'include: "a.view" explore: b { from: a }',
                      "a.view.lkml": "view: a {}"})
