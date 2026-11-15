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

"""Example-based tests for the Apache Ossie <-> Metric View converter, plus the fixture
comparisons that pin the expected output of both directions.

Ported from the Java ``OssieConverterSuite``.
"""

import textwrap
from pathlib import Path

import pytest

from ossie_databricks import joined_integer_sum
from ossie_databricks import ossie_converter_common
from ossie_databricks.metric_view_to_ossie import serialize_ossie
from ossie_databricks.ossie_converter import (
    OSSIE_VERSION,
    ConversionError,
    ConversionErrorKind,
    Notices,
    convert_metric_view_to_ossie,
    convert_ossie_to_metric_view,
    dump_yaml,
    parse_yaml,
)

RESOURCES = Path(__file__).parent / "resources"


def export(osi, source):
    return parse_yaml(convert_ossie_to_metric_view(osi, source).yaml)


class _ThrowingBean:
    """Python stand-in for the Java ThrowingBean: an object no YAML/JSON serializer can encode."""

    @property
    def value(self):
        raise RuntimeError("expected serialization failure")


def value_that_fails_serialization():
    return {"bad": _ThrowingBean()}


def ossie_model_with_body(body):
    # Flat model at the document root. Callers pass a body indented two spaces (it used to sit
    # under a `- name: m` list item); de-indent it so it sits at the root next to `name`.
    root = "version: '" + OSSIE_VERSION + "'\nname: m\n"
    for line in body.split("\n"):
        root += (line[2:] if line.startswith("  ") else line) + "\n"
    return root[:-1]


def cascade_notice(kind, name, reference):
    return ("[" + kind + " '" + name + "'] references dropped '" + reference
            + "'; dropping (downstream of a dropped field/metric)")


def assert_invalid_ossie_input(yaml, expected_reason):
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(yaml, None)
    e = excinfo.value
    assert e.kind == ConversionErrorKind.INVALID_INPUT
    assert e.reason == expected_reason


def test_fixture_a_star_schema_exports_to_expected_metric_view():
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: sales\n"
        + "description: Sales orders with customer attributes\n"
        + "datasets:\n"
        + "  - name: orders\n"
        + "    source: samples.tpch.orders\n"
        + "    primary_key: [o_orderkey]\n"
        + "    description: One row per order\n"
        + "    fields:\n"
        + "      - name: o_orderkey\n"
        + "        expression:\n"
        + "          dialects:\n"
        + "            - dialect: DATABRICKS\n"
        + "              expression: o_orderkey\n"
        + "        description: Order identifier\n"
        + "      - name: o_orderdate\n"
        + "        expression:\n"
        + "          dialects:\n"
        + "            - dialect: DATABRICKS\n"
        + "              expression: o_orderdate\n"
        + "        label: Order Date\n"
        + "        ai_context:\n"
        + "          synonyms: [order date, date]\n"
        + "  - name: customer\n"
        + "    source: samples.tpch.customer\n"
        + "    primary_key: [c_custkey]\n"
        + "    fields:\n"
        + "      - name: c_name\n"
        + "        expression:\n"
        + "          dialects:\n"
        + "            - dialect: DATABRICKS\n"
        + "              expression: c_name\n"
        + "        description: Customer name\n"
        + "relationships:\n"
        + "  - name: orders_to_customer\n"
        + "    from: orders\n"
        + "    to: customer\n"
        + "    from_columns: [o_custkey]\n"
        + "    to_columns: [c_custkey]\n"
        + "metrics:\n"
        + "  - name: total_revenue\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "        - dialect: DATABRICKS\n"
        + "          expression: SUM(o_totalprice)\n"
        + "    description: Total order revenue\n"
        + "    ai_context:\n"
        + "      synonyms: [revenue, total revenue, sales]\n"
        + "  - name: order_count\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "        - dialect: DATABRICKS\n"
        + "          expression: COUNT(*)\n"
        + "    description: Number of orders\n"
    )

    expected = (
        "version: '1.1'\n"
        + "source: samples.tpch.orders\n"
        + "comment: Sales orders with customer attributes\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: samples.tpch.customer\n"
        + "  on: source.o_custkey = customer.c_custkey\n"
        + "  rely:\n"
        + "    at_most_one_match: true\n"
        + "dimensions:\n"
        + "- name: o_orderkey\n"
        + "  expr: o_orderkey\n"
        + "  comment: Order identifier\n"
        + "- name: o_orderdate\n"
        + "  expr: o_orderdate\n"
        + "  display_name: Order Date\n"
        + "  synonyms:\n"
        + "  - order date\n"
        + "  - date\n"
        + "- name: c_name\n"
        + "  expr: customer.c_name\n"
        + "  comment: Customer name\n"
        + "measures:\n"
        + "- name: total_revenue\n"
        + "  expr: SUM(o_totalprice)\n"
        + "  comment: Total order revenue\n"
        + "  synonyms:\n"
        + "  - revenue\n"
        + "  - total revenue\n"
        + "  - sales\n"
        + "- name: order_count\n"
        + "  expr: COUNT(*)\n"
        + "  comment: Number of orders\n"
    )

    assert parse_yaml(expected) == export(osi, None)


def test_measure_display_name_survives_round_trip_via_stash():
    # A dimension's display_name maps to the Ossie Field `label`, but the Ossie Metric schema has
    # no `label`, so a measure's display_name is preserved in the DATABRICKS custom_extensions
    # stash (like format/window) and restored on the way back.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.fact\n"
        + "dimensions:\n"
        + "- name: region\n"
        + "  expr: region\n"
        + "measures:\n"
        + "- name: total_revenue\n"
        + "  expr: SUM(amount)\n"
        + "  display_name: Total Revenue\n"
    )

    ossie = convert_metric_view_to_ossie(mv, None).yaml
    # The only display_name in the input is on the measure; it rides in the metric's stash, since
    # there is no native Ossie field for it.
    assert "display_name" in ossie, \
        "expected the measure display_name preserved in the stash, got: " + ossie

    mv2 = convert_ossie_to_metric_view(ossie, None).yaml
    assert parse_yaml(mv) == parse_yaml(mv2)


def test_unsupported_version_is_rejected():
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view("version: '9.9'\nname: m\n", None)
    assert "Unsupported Apache Ossie version" in str(excinfo.value)


def test_multiple_candidate_facts_without_source_is_rejected():
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- {name: orders, source: c.s.orders}\n"
        + "- {name: returns, source: c.s.returns}\n"
        + "- {name: customer, source: c.s.customer, primary_key: [c_custkey]}\n"
        + "relationships:\n"
        + "- {name: oc, from: orders, to: customer, from_columns: [o_custkey], to_columns: [c_custkey]}\n"
        + "- {name: rc, from: returns, to: customer, from_columns: [re_custkey], to_columns: [c_custkey]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "multiple candidate fact datasets" in str(excinfo.value)


def test_one_to_many_emits_cardinality():
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: orders\n"
        + "  source: c.s.orders\n"
        + "  primary_key: [o_orderkey]\n"
        + "  fields:\n"
        + "  - {name: o_orderstatus, expression: {dialects: [{dialect: DATABRICKS, expression: o_orderstatus}]}}\n"
        + "- name: lineitem\n"
        + "  source: c.s.lineitem\n"
        + "relationships:\n"
        + "- {name: lio, from: lineitem, to: orders, from_columns: [l_orderkey], to_columns: [o_orderkey]}\n"
        + "metrics:\n"
        + "- {name: qty, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(lineitem.l_quantity)}]}}\n"
    )
    view = export(osi, "orders")
    joins = view.get("joins")
    join = joins[0]
    assert join.get("cardinality") == "one_to_many"


def test_one_to_many_nested_under_many_to_one_is_rejected():
    # A Metric View requires one cardinality per top-level branch, so a one-to-many join nested
    # under a many-to-one parent is rejected by Databricks just as the reverse nesting is. Fact
    # `d`; `f -> d` points at d (many-to-one from d's perspective), and `g -> f` points away from
    # f (one-to-many), which would nest one_to_many inside the many-to-one branch.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "  fields:\n"
        + "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  primary_key: [fk]\n"
        + "- name: g\n"
        + "  source: c.s.g\n"
        + "relationships:\n"
        + "- {name: df, from: d, to: f, from_columns: [fk], to_columns: [fk]}\n"
        + "- {name: gf, from: g, to: f, from_columns: [fk], to_columns: [fk]}\n"
        + "metrics:\n"
        + "- {name: c, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(1)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, "d")
    e = excinfo.value
    assert "share the same cardinality" in str(e), \
        "expected a mixed-cardinality rejection, got: " + str(e)


def test_directed_cycle_with_no_equidistant_edge_is_rejected():
    # a -> b -> c -> d -> e -> b with fact `a`: every edge spans adjacent BFS levels, so the
    # equidistance heuristic sees nothing, and `a` has zero incoming edges so pickFact finds a
    # root. Without a real acyclicity check this expanded into duplicate join paths (`d` under
    # both `c` and `e`), fabricating a tree from a cyclic model.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: a\n"
        + "  source: c.s.a\n"
        + "  fields:\n"
        + "  - {name: acol, expression: {dialects: [{dialect: DATABRICKS, expression: acol}]}}\n"
        + "- name: b\n"
        + "  source: c.s.b\n"
        + "- name: c\n"
        + "  source: c.s.c\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "- name: e\n"
        + "  source: c.s.e\n"
        + "relationships:\n"
        + "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        + "- {name: bc, from: b, to: c, from_columns: [k], to_columns: [k]}\n"
        + "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
        + "- {name: de, from: d, to: e, from_columns: [k], to_columns: [k]}\n"
        + "- {name: eb, from: e, to: b, from_columns: [k], to_columns: [k]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, "a")
    ex = excinfo.value
    assert "directed cycle" in str(ex), \
        "expected a directed-cycle rejection, got: " + str(ex)


def test_diamond_is_still_accepted_by_the_cycle_check():
    # A diamond (`a -> b -> d` plus `a -> c -> d`) is an UNDIRECTED cycle but directed-acyclic, and
    # is supported via the fan-out aliases. The cycle check must not reject it.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: a\n"
        + "  source: c.s.a\n"
        + "- name: b\n"
        + "  source: c.s.b\n"
        + "- name: c\n"
        + "  source: c.s.c\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "  fields:\n"
        + "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        + "  - name: named_branch\n"
        + "    expression: {dialects: [{dialect: DATABRICKS, expression: UPPER(b.b_d.dcol)}]}\n"
        + "relationships:\n"
        + "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        + "- {name: ac, from: a, to: c, from_columns: [k], to_columns: [k]}\n"
        + "- {name: bd, from: b, to: d, from_columns: [k], to_columns: [k]}\n"
        + "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
    )
    view = export(osi, "a")
    assert view.get("source") == "c.s.a"
    # `d` is reached by two paths, so its column is emitted once per fan-out alias.
    dims = view.get("dimensions")
    assert len(dims) == 3
    assert {"name": "b_d_named_branch", "expr": "UPPER(b.b_d.dcol)"} in dims
    assert not any("c_d_named_branch" == value.get("name") for value in dims)


def test_measure_naming_a_diamond_dataset_is_dropped_with_notice():
    # Same diamond as above (`d` reached via `a -> b -> d` and `a -> c -> d`), plus a measure that
    # names `d` by its bare dataset name. A measure addresses a dataset by name, so it cannot be
    # split per fan-out path the way the dimension on `d` is; it is dropped with a notice rather
    # than silently bound to one arbitrary branch.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: a\n"
        + "  source: c.s.a\n"
        + "- name: b\n"
        + "  source: c.s.b\n"
        + "- name: c\n"
        + "  source: c.s.c\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "  fields:\n"
        + "  - {name: dcol, expression: {dialects: [{dialect: DATABRICKS, expression: dcol}]}}\n"
        + "relationships:\n"
        + "- {name: ab, from: a, to: b, from_columns: [k], to_columns: [k]}\n"
        + "- {name: ac, from: a, to: c, from_columns: [k], to_columns: [k]}\n"
        + "- {name: bd, from: b, to: d, from_columns: [k], to_columns: [k]}\n"
        + "- {name: cd, from: c, to: d, from_columns: [k], to_columns: [k]}\n"
        + "metrics:\n"
        + "- {name: dsum, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(d.dcol)}]}}\n"
    )
    result = convert_ossie_to_metric_view(osi, "a")
    view = parse_yaml(result.yaml)
    # The diamond dimension on `d` still fans out to one dimension per path.
    assert len(view.get("dimensions")) == 2
    # The measure that names `d` is dropped rather than bound to an arbitrary branch.
    assert "measures" not in view, \
        "a measure naming a diamond dataset must be dropped, got: " + str(view.get("measures"))
    expected = ("expression references dataset 'd', reached by more than one join path "
                + "(diamond); cannot be unambiguously qualified; dropped")
    assert any(expected in n for n in result.notices), str(result.notices)


def test_nested_join_columns_in_dimensions_and_measures_get_full_alias_path():
    # orders -> customer -> nation: `nation` nests under `customer`, so its columns are addressed
    # as `customer.nation.col`. A bare `nation.` head would be read as struct access on a
    # parameter, so the measure must be re-qualified -- and identically to the dimension path,
    # which already emits `customer.nation.population` for the same column.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: orders\n"
        + "  source: c.s.orders\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  primary_key: [c_custkey]\n"
        + "- name: nation\n"
        + "  source: c.s.nation\n"
        + "  primary_key: [n_nationkey]\n"
        + "  fields:\n"
        + "  - {name: population, expression: {dialects: [{dialect: DATABRICKS, expression: population}]}}\n"
        + "  - name: population_bucket\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "      - dialect: ANSI_SQL\n"
        + "        expression: >-\n"
        + "          CASE WHEN CAST(population AS DECIMAL(12, 0)) > 0\n"
        + "          AND EXTRACT(YEAR FROM census_date) = 2024 THEN 'population' ELSE 'unknown' END\n"
        + "  - name: qualified_population\n"
        + "    expression: {dialects: [{dialect: DATABRICKS, expression: ABS(customer.nation.population)}]}\n"
        + "  - name: mixed_population\n"
        + "    expression: {dialects: [{dialect: DATABRICKS, expression: customer.nation.population + estimate}]}\n"
        + "relationships:\n"
        + "- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}\n"
        + "- {name: cn, from: customer, to: nation, from_columns: [n_nationkey], to_columns: [n_nationkey]}\n"
        + "metrics:\n"
        + "- {name: pop, expression: {dialects: [{dialect: DATABRICKS, expression: SUM(nation.population)}]}}\n"
    )
    view = export(osi, "orders")

    dims = view.get("dimensions")
    dim = dims[0]
    assert dim.get("expr") == "customer.nation.population", \
        "dimension path should qualify with the full alias path"
    computed = dims[1]
    assert computed.get("expr") == (
        "CASE WHEN CAST(customer.nation.population AS DECIMAL(12, 0)) > 0 "
        + "AND EXTRACT(YEAR FROM customer.nation.census_date) = 2024 "
        + "THEN 'population' ELSE 'unknown' END")
    assert dims[2].get("expr") == "ABS(customer.nation.population)"
    assert dims[3].get("expr") == "customer.nation.population + customer.nation.estimate"

    measures = view.get("measures")
    measure = measures[0]
    assert measure.get("expr") == "SUM(customer.nation.population)", \
        "measure must use the same full alias path as the dimension, not a bare nested alias"


def test_joined_integer_sum_handles_formatting_and_only_skips_modified_operands():
    filtered = "SUM(sales.amount) /* note */ FILTER (WHERE active)"
    windowed = "SUM(sales.amount) OVER (PARTITION BY region)"
    total = "SUM(store.employees)"
    expression = filtered + " + " + windowed + " + " + total
    encoded = joined_integer_sum.encode(expression, {"store.employees": "store.id"}, {"store"})
    assert encoded.startswith(filtered + " + " + windowed + " + AGGREGATE(")
    formatted = (encoded.replace("AGGREGATE(COLLECT_SET", "aggregate ( \n  collect_set")
                 .replace(" AS ossie_key, ", " AS ossie_key ,\n  "))
    assert joined_integer_sum.decode(formatted).expression == expression


def test_joined_integer_sum_keeps_the_whole_expression_when_any_operand_is_unsupported():
    for unsupported in ["SUM(store.floor_space)", "COUNT(store.id)",
                        "SUM(store.a + store.b)",
                        "SUM(store.employees) /* note */ FILTER (WHERE active)",
                        "SUM(store.employees) OVER (PARTITION BY region)"]:
        assert joined_integer_sum.encode("SUM(store.employees) / " + unsupported,
                                         {"store.employees": "store.id"}, {"store"}) is None


def test_joined_integer_sum_does_not_decode_conflicting_keys():
    total = "SUM(store.employees)"
    first = joined_integer_sum.encode(total, {"store.employees": "store.id"}, {"store"})
    second = joined_integer_sum.encode(total, {"store.employees": "store.other_id"}, {"store"})
    assert not joined_integer_sum.decode(first + " + " + second).keys


def test_joined_integer_sum_deduplicates_by_primary_key_and_round_trips():
    osi = textwrap.dedent("""\
        version: 0.2.0.dev0
        name: sales
        datasets:
        - name: sales
          source: c.s.sales
          fields:
          - name: amount
            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}
        - name: store
          source: c.s.store
          primary_key: [id]
          fields:
          - name: id
            expression: {dialects: [{dialect: ANSI_SQL, expression: id}]}
          - name: employees
            datatype: Integer
            expression: {dialects: [{dialect: ANSI_SQL, expression: employees}]}
        relationships:
        - {name: sale_store, from: sales, to: store, from_columns: [store_id], to_columns: [id]}
        metrics:
        - name: productivity
          expression:
            dialects:
            - dialect: ANSI_SQL
              expression: SUM(sales.amount) / NULLIF(SUM(store.employees), 0)
          custom_extensions:
          - vendor_name: CUBE
            data: '{invalid'
          - vendor_name: CUBE
            data: '{"integer_sum_grain": ["store"]}'
        - name: different_counts
          expression:
            dialects:
            - dialect: ANSI_SQL
              expression: SUM(DISTINCT store.employees)
        """)
    view = export(osi, "sales")
    measures = view.get("measures")
    expected = ("SUM(amount) / NULLIF(AGGREGATE(COLLECT_SET(STRUCT(store.id AS ossie_key, "
                + "store.employees AS ossie_value)) FILTER (WHERE store.id IS NOT NULL "
                + "AND store.employees IS NOT NULL), CAST(NULL AS BIGINT), "
                + "(ossie_acc, ossie_row) -> COALESCE(ossie_acc, 0L) + "
                + "CAST(ossie_row.ossie_value AS BIGINT)), 0)")
    assert measures[0].get("expr") == expected
    assert measures[1].get("expr") == "SUM(DISTINCT store.employees)"

    exported = convert_ossie_to_metric_view(osi, "sales")
    assert any("[store]" in notice and "memory grows with the key count" in notice
               for notice in exported.notices)
    native_yaml = exported.yaml
    imported = convert_metric_view_to_ossie(native_yaml, "sales").yaml
    assert "SUM(amount) / NULLIF(SUM(store.employees), 0)" in imported, imported
    assert export(imported, "sales") == view
    imported_model = ossie_converter_common.as_map(parse_yaml(imported))
    metric = ossie_converter_common.as_map(
        ossie_converter_common.as_list(imported_model.get("metrics"))[0])
    assert ossie_converter_common.cube_integer_sum_metadata(metric).get("integer_sum_grain") \
        == ["store"]
    expression = ossie_converter_common.as_map(metric.get("expression"))
    # Java removeIf mutates the list in place; as_list returns the same list object.
    dialects = ossie_converter_common.as_list(expression.get("dialects"))
    dialects[:] = [entry for entry in dialects
                   if "DATABRICKS" != ossie_converter_common.as_map(entry).get("dialect")]
    assert export(ossie_converter_common.dump_yaml(imported_model), "sales") == view

    mixed = osi.replace("NULLIF(SUM(store.employees), 0)",
                        "NULLIF(SUM(store.employees) + SUM(store.floor_space), 0)")
    mixed_export = convert_ossie_to_metric_view(mixed, "sales")
    assert "COLLECT_SET" not in mixed_export.yaml
    assert any("cannot preserve the entire Cube aggregate grain for [store]" in notice
               for notice in mixed_export.notices)

    decimal = osi.replace("datatype: Integer", "datatype: Decimal")
    assert "COLLECT_SET" not in convert_ossie_to_metric_view(decimal, "sales").yaml
    no_grain = osi.replace("[\"store\"]", "[]")
    assert "COLLECT_SET" not in convert_ossie_to_metric_view(no_grain, "sales").yaml


def test_model_with_no_fields_or_metrics_is_rejected():
    # A Metric View requires at least one dimension or measure, so emitting a version+source-only
    # view would just fail at CREATE. Fail at conversion time instead.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    e = excinfo.value
    assert "no dimensions or measures" in str(e), \
        "expected an empty-view rejection, got: " + str(e)


def test_empty_after_drops_names_the_dropped_columns():
    # Here the input is non-empty but everything drops: the only metric has no DATABRICKS/ANSI_SQL
    # dialect. The error must name the dropped column so the cause is actionable, since after the
    # cascade the emptiness is a consequence of the drop rather than an empty input.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "metrics:\n"
        + "- {name: only_metric, expression: {dialects: [{dialect: SNOWFLAKE, expression: SUM(x)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    e = excinfo.value
    assert "no dimensions or measures" in str(e), \
        "expected an empty-view rejection, got: " + str(e)
    assert "only_metric" in str(e), \
        "the message must name the dropped column, got: " + str(e)


def test_duplicate_dimension_name_is_rejected():
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: orders\n"
        + "  source: c.s.orders\n"
        + "  fields:\n"
        + "  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  fields:\n"
        + "  - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n"
        + "relationships:\n"
        + "- {name: r, from: orders, to: customer, from_columns: [cid], to_columns: [id]}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "collides" in str(excinfo.value)


def test_foreign_vendor_extension_dropped_with_notice():
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "custom_extensions:\n"
        + "- {vendor_name: SNOWFLAKE, data: '{}'}\n"
        + "datasets:\n"
        + "- name: orders\n"
        + "  source: c.s.orders\n"
        + "  fields:\n"
        + "  - {name: s, expression: {dialects: [{dialect: DATABRICKS, expression: s}]}}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert any("foreign-vendor custom_extensions dropped" in m for m in r.notices)


def test_datatype_dropped_with_notice():
    # `datatype` (a logical type on a field or metric) has no Metric View slot, where a column's
    # type is inferred from its expression, so it is dropped with a notice on both.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - {name: d, datatype: String, expression: "
        + "{dialects: [{dialect: DATABRICKS, expression: d}]}}\n"
        + "metrics:\n"
        + "- {name: n, datatype: Integer, expression: "
        + "{dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert "[field 'd'] datatype has no Metric View counterpart; dropped" in r.notices, \
        str(r.notices)
    assert "[metric 'n'] datatype has no Metric View counterpart; dropped" in r.notices, \
        str(r.notices)


def test_relationship_foreign_vendor_extension_dropped_with_notice():
    # A foreign-vendor custom_extensions on a relationship has no Metric View slot, so it is
    # dropped with a notice, matching the model/dataset/field levels.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: o\n"
        + "  source: c.s.o\n"
        + "  fields:\n"
        + "  - {name: a, expression: {dialects: [{dialect: DATABRICKS, expression: a}]}}\n"
        + "- {name: c, source: c.s.c, primary_key: [k]}\n"
        + "relationships:\n"
        + "- {name: oc, from: o, to: c, from_columns: [k], to_columns: [k], "
        + "custom_extensions: [{vendor_name: SNOWFLAKE, data: '{}'}]}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    r = convert_ossie_to_metric_view(osi, None)
    assert "[relationship 'oc'] foreign-vendor custom_extensions dropped" in r.notices, \
        str(r.notices)


def test_scalar_join_columns_rejected_as_must_be_lists():
    # from_columns/to_columns given as a scalar (not a list) raise a clear "must be lists" error
    # rather than the misleading "are required" (empty-list) or a character-count length error.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- {name: a, source: c.s.a, fields: "
        + "[{name: x, expression: {dialects: [{dialect: DATABRICKS, expression: x}]}}]}\n"
        + "- {name: b, source: c.s.b}\n"
        + "relationships:\n"
        + "- {name: ab, from: a, to: b, from_columns: cid, to_columns: id}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    e = excinfo.value
    assert "must be lists" in str(e), str(e)


def test_empty_explicit_source_falls_back_like_absent():
    # An empty --source (e.g. an unset shell variable) must be treated as absent and fall back to
    # the fact heuristic, not taken as a real override (which would fail as "requested source ''
    # is not a dataset").
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: orders\n"
        + "  source: c.s.orders\n"
        + "  fields:\n"
        + "  - {name: amount, expression: {dialects: [{dialect: DATABRICKS, expression: amount}]}}\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  fields:\n"
        + "  - {name: c_name, expression: {dialects: [{dialect: DATABRICKS, expression: c_name}]}}\n"
        + "relationships:\n"
        + "- {name: oc, from: orders, to: customer, from_columns: [c_custkey], to_columns: [c_custkey]}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    assert export(osi, "") == export(osi, None)


# -- import direction (Metric View -> Apache Ossie) -----------------------


def import_mv(mv):
    return parse_yaml(convert_metric_view_to_ossie(mv, None).yaml)


def test_empty_model_name_falls_back_like_absent():
    # An empty --name (e.g. an unset shell variable) must be treated as absent and fall back to
    # deriving the model name from the source's last identifier, not taken as a literal name: "".
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "dimensions:\n"
        + "- name: amount\n"
        + "  expr: amount\n"
        + "measures:\n"
        + "- name: n\n"
        + "  expr: COUNT(*)\n"
    )
    assert convert_metric_view_to_ossie(mv, "").yaml == convert_metric_view_to_ossie(mv, None).yaml


def metric_view_with_joins(join_count):
    """A Metric View YAML with ``join_count`` sibling equi-joins on the fact source."""
    mv = "version: '1.1'\nsource: c.s.fact\njoins:\n"
    for i in range(1, join_count + 1):
        mv += ("- name: j" + str(i) + "\n"
               + "  source: c.s.j" + str(i) + "\n"
               + "  on: source.k = j" + str(i) + ".k\n")
    return mv


def test_import_decomposes_join_into_relationship():
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  on: source.o_custkey = customer.c_custkey\n"
        + "  rely: {at_most_one_match: true}\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
        + "- {name: c_name, expr: customer.c_name}\n"
        + "measures:\n"
        + "- {name: revenue, expr: SUM(o_totalprice)}\n"
    )
    out = import_mv(mv)
    model = out
    rels = model.get("relationships")
    rel = rels[0]
    assert rel.get("from") == "orders"
    assert rel.get("to") == "customer"
    assert rel.get("from_columns") == ["o_custkey"]
    assert rel.get("to_columns") == ["c_custkey"]


def test_export_rejects_more_joins_than_import_can_rebuild():
    # MV -> Ossie has no join bound of its own, but Ossie -> MV rejects a model with more than
    # MAX_JOIN_NODES datasets. Without a matching bound here, a Metric View with too many joins
    # would export to a model that could never be imported again; reject it at export instead.
    max_nodes = ossie_converter_common.MAX_JOIN_NODES
    mv = metric_view_with_joins(max_nodes)
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie(mv, None)
    e = excinfo.value
    assert "at most " + str(max_nodes) + " are supported" in str(e), \
        "expected a join-count rejection, got: " + str(e)


def test_export_at_the_join_limit_still_succeeds():
    # The fact plus (MAX_JOIN_NODES - 1) joins is exactly MAX_JOIN_NODES datasets, the boundary the
    # reverse conversion accepts, so export must still succeed here.
    max_nodes = ossie_converter_common.MAX_JOIN_NODES
    out = import_mv(metric_view_with_joins(max_nodes - 1))
    model = out
    datasets = model.get("datasets")
    assert len(datasets) == max_nodes, "fact plus joins should be exactly the limit"


def test_non_equi_on_rejected():
    # A non-equi `on` has no Apache Ossie relationship form (from/to columns are required), so it is
    # rejected on import rather than emitting a relationship with empty column lists.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  on: source.o_custkey >= customer.c_custkey\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie(mv, None)
    e = excinfo.value
    assert "non-equi" in str(e), str(e)


def test_complex_equi_on_rejected():
    # An equi `on` whose operand is a SQL fragment (OR-joined, or computed) cannot be decomposed
    # into from/to columns, so it is rejected rather than producing a relationship with empty
    # column lists.
    for cond in ["source.a = dim.b OR source.c = dim.d", "source.a = dim.b + 1"]:
        mv = (
            "version: '1.1'\n"
            + "source: c.s.fact\n"
            + "joins:\n"
            + "- name: dim\n"
            + "  source: c.s.dim\n"
            + "  on: " + cond + "\n"
        )
        with pytest.raises(ConversionError) as excinfo:
            convert_metric_view_to_ossie(mv, None)
        e = excinfo.value
        assert "non-equi" in str(e), str(e)


def test_equi_join_on_preserved_verbatim_when_non_canonical():
    # An equi-join is a valid Ossie relationship (from/to columns), but rebuilding its `on` from
    # those columns canonicalizes the fact qualifier to `source`. When the original used the source
    # table name instead, the raw `on` is stashed so the round trip reproduces it exactly.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  on: orders.o_custkey = customer.c_custkey\n"
        + "measures:\n"
        + "- name: revenue\n"
        + "  expr: SUM(o_totalprice)\n"
    )
    ossie = convert_metric_view_to_ossie(mv, None).yaml
    mv2 = convert_ossie_to_metric_view(ossie, None).yaml
    assert "orders.o_custkey = customer.c_custkey" in mv2, mv2


def test_equi_join_on_over_equal_columns_stays_on_not_using():
    # An equi-join `on` over equal column names rebuilds from columns as `using`, so the original
    # `on` is stashed and restored -- the round trip keeps `on`, it does not collapse to `using`.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  on: source.custkey = customer.custkey\n"
        + "measures:\n"
        + "- name: cnt\n"
        + "  expr: COUNT(1)\n"
    )
    ossie = convert_metric_view_to_ossie(mv, None).yaml
    mv2 = convert_ossie_to_metric_view(ossie, None).yaml
    assert "source.custkey = customer.custkey" in mv2, mv2
    assert "using" not in mv2, mv2


def test_import_rejects_cross_join():
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie(mv, None)
    assert "no join condition" in str(excinfo.value)


def test_import_rejects_unsupported_version():
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie("version: '0.1'\nsource: c.s.t\n", None)
    assert "Unsupported Metric View version" in str(excinfo.value)


def test_mv_to_ossie_to_mv_round_trips_stash():
    # A view with MV-only features (filter/rely/format/window) must survive
    # MV -> Ossie -> MV unchanged (the custom_extensions stash carries them).
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "filter: o_orderstatus = 'F'\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: c.s.customer\n"
        + "  on: source.o_custkey = customer.c_custkey\n"
        + "  rely:\n"
        + "    at_most_one_match: true\n"
        + "dimensions:\n"
        + "- name: net\n"
        + "  expr: o_totalprice\n"
        + "  format:\n"
        + "    type: currency\n"
        + "    currency_code: USD\n"
        + "measures:\n"
        + "- name: running\n"
        + "  expr: SUM(o_totalprice)\n"
        + "  window:\n"
        + "  - order: net\n"
        + "    semiadditive: last\n"
        + "    range: cumulative\n"
    )
    ossie = convert_metric_view_to_ossie(mv, None).yaml
    back = convert_ossie_to_metric_view(ossie, None).yaml
    assert parse_yaml(mv) == parse_yaml(back)


# -- fixture comparisons ---------------------------------------------------
# The fixtures under tests/resources/ossie_*.yaml pin the expected outputs
# checked into apache/ossie. Asserting the output parses equal to them (structure
# + stash blob STRINGS) is the real "one behavior, two implementations" guarantee --
# it catches divergences like stash JSON spacing that a round-trip misses.


def load_fixture(name):
    path = RESOURCES / ("ossie_" + name)
    if not path.is_file():
        raise RuntimeError("fixture not found: ossie_" + name)
    return path.read_text(encoding="utf-8")


def test_fixture_a_export_matches_fixture():
    out = convert_ossie_to_metric_view(load_fixture("fixtureA_ossie.yaml"), None).yaml
    assert parse_yaml(out) == parse_yaml(load_fixture("fixtureA_metric_view.yaml"))


def test_export_writes_java_compatible_yaml_style():
    # Matches the Java writer: values double-quoted, keys plain -- including the join's `on`,
    # which a YAML 1.1 writer would quote as a boolean-like token.
    out = convert_ossie_to_metric_view(load_fixture("fixtureA_ossie.yaml"), None).yaml
    assert out.startswith('version: "1.1"\nsource: "samples.tpch.orders"\n'), out
    assert '\n  on: "source.o_custkey = customer.c_custkey"\n' in out, out


def test_fixture_b_import_matches_fixture():
    # fixtureB exercises the custom_extensions stash (format/rely/filter) -- the parse
    # includes the blob strings, so this is what pins the stash blob's exact spacing.
    out = convert_metric_view_to_ossie(load_fixture("fixtureB_metric_view.yaml"), None).yaml
    assert parse_yaml(out) == parse_yaml(load_fixture("fixtureB_ossie.yaml"))


def test_tpcds_export_matches_fixture():
    out = convert_ossie_to_metric_view(load_fixture("tpcds_ossie.yaml"), None).yaml
    assert parse_yaml(out) == parse_yaml(load_fixture("tpcds_metric_view.yaml"))


def test_stash_blob_uses_expected_spacing():
    # The strongest byte-level check: the emitted stash blob string (the `data` value of a
    # custom_extensions entry) must use the stash format's separators
    # (", " / ": "). Pull the blob out of the parsed model rather than substring-matching
    # the outer YAML (where it appears escaped).
    out = parse_yaml(
        convert_metric_view_to_ossie(load_fixture("fixtureB_metric_view.yaml"), None).yaml)
    model = out
    exts = model.get("custom_extensions")
    blob = exts[0].get("data")
    assert blob.startswith("{\"_v\": 1, "), \
        "stash blob must use the spacing '{\"_v\": 1, ...}', got: " + blob


# -- parity edge cases found in review round 3 ----------------------------


def test_exports_primary_sql_for_dimensions_and_measures():
    for legacy_dialect in ["", "ANSI_SQL", "OSSIE_SQL_2026"]:
        field = "sql: amount * 2"
        metric = "sql: SUM(orders.amount)"
        if legacy_dialect:
            prefix = ", dialects: [{dialect: DATABRICKS, expression: '  '}, {dialect: "
            field += prefix + legacy_dialect + ", expression: amount * 3}]"
            metric += prefix + legacy_dialect + ", expression: MAX(orders.amount)}]"
        ossie = ("version: 0.2.0.dev0\nname: m\ndatasets:\n"
                 + "- name: orders\n  source: c.s.orders\n  fields:\n"
                 + "  - {name: adjusted_amount, expression: {" + field + "}}\n"
                 + "metrics:\n- {name: total_amount, expression: {" + metric + "}}\n")
        view = parse_yaml(convert_ossie_to_metric_view(ossie, None).yaml)
        assert view.get("dimensions") == [{"name": "adjusted_amount", "expr": "amount * 2"}]
        assert view.get("measures") == [{"name": "total_amount", "expr": "SUM(amount)"}]


def test_blank_portable_fallback_is_not_usable():
    for blank in ["", "   "]:
        assert ossie_converter_common.pick_expression(
            {"dialects": [{"dialect": "OSSIE_SQL_2026", "expression": blank}]},
            "field 'amount'") is None


def test_pick_expression_falls_through_empty_databricks_to_ansi():
    # An empty DATABRICKS dialect must fall through to ANSI_SQL,
    # not be selected as the (empty) expression.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - name: d\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "      - {dialect: DATABRICKS, expression: ''}\n"
        + "      - {dialect: ANSI_SQL, expression: ansi_col}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out.get("dimensions")
    dim = dims[0]
    assert dim.get("expr") == "ansi_col"


def test_ossie_sql2026_dimension_exported_not_dropped():
    # A dimension expressed only in OSSIE_SQL_2026 (Apache Ossie's portable, ANSI-compatible
    # dialect) must be exported, not dropped (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - {name: region, expression: {dialects: "
        + "[{dialect: OSSIE_SQL_2026, expression: o_region}]}}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out.get("dimensions")
    dim = dims[0]
    assert dim.get("expr") == "o_region"


def test_ossie_sql2026_measure_exported_not_dropped():
    # A measure expressed only in OSSIE_SQL_2026 must be exported, not dropped (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - {name: region, expression: {dialects: [{dialect: DATABRICKS, expression: region}]}}\n"
        + "metrics:\n"
        + "- {name: rev, expression: {dialects: "
        + "[{dialect: OSSIE_SQL_2026, expression: SUM(amount)}]}}\n"
    )
    out = export(osi, None)
    measures = out.get("measures")
    measure = measures[0]
    assert measure.get("expr") == "SUM(amount)"


def test_databricks_dialect_preferred_over_ossie_sql2026():
    # DATABRICKS wins over OSSIE_SQL_2026 when both are present.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - name: region\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "      - {dialect: OSSIE_SQL_2026, expression: portable_region}\n"
        + "      - {dialect: DATABRICKS, expression: dbx_region}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    out = export(osi, None)
    dims = out.get("dimensions")
    dim = dims[0]
    assert dim.get("expr") == "dbx_region"


def test_unsupported_dialect_only_dropped_with_warning():
    # A field whose only dialect is unsupported (here SNOWFLAKE) is dropped and warns, guarding
    # the allowlist against silently accepting any dialect (apache/ossie#442).
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - {name: kept, expression: {dialects: [{dialect: DATABRICKS, expression: kept}]}}\n"
        + "  - {name: region, expression: {dialects: "
        + "[{dialect: SNOWFLAKE, expression: o_region}]}}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    notices = convert_ossie_to_metric_view(osi, None).notices
    assert any(
        "no usable expression.sql or DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 entry" in n
        for n in notices), str(notices)


def test_pick_expression_rejects_non_string_expression():
    # A non-string dialect expression (e.g. a YAML number) must raise, not be coerced.
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: f\n"
        + "  source: c.s.f\n"
        + "  fields:\n"
        + "  - name: d\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "      - {dialect: DATABRICKS, expression: 123}\n"
        + "metrics:\n"
        + "- {name: n, expression: {dialects: [{dialect: DATABRICKS, expression: COUNT(*)}]}}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(osi, None)
    assert "expression must be a string" in str(excinfo.value)


def test_bare_on_off_values_stay_strings_not_booleans():
    # YAML 1.1 would read a bare `on`/`off`/`yes`/`no` value as a boolean, silently losing
    # a join condition or turning a synonym into `true`. Confirm the converter's parser
    # keeps them as strings (the reader uses YAML 1.2 boolean semantics).
    parsed = parse_yaml("a: on\nb: off\nc: yes\nd: no\n")
    m = parsed
    assert m.get("a") == "on"
    assert m.get("b") == "off"
    assert m.get("c") == "yes"
    assert m.get("d") == "no"


def test_import_drops_empty_optional_fields():
    # Optional fields are mapped only when non-empty, so an empty
    # comment / empty synonyms list are omitted, not emitted as `description: ""` or an
    # empty ai_context. The port must match (empty string / empty list are falsy).
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "comment: ''\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus, comment: '', display_name: '', synonyms: []}\n"
        + "measures:\n"
        + "- {name: revenue, expr: SUM(o_totalprice), comment: '', synonyms: []}\n"
    )
    out = import_mv(mv)
    model = out
    assert "description" not in model, "empty comment must not become a description"
    ds = model.get("datasets")[0]
    field = ds.get("fields")[0]
    assert "description" not in field, "empty comment must not map to description"
    assert "label" not in field, "empty display_name must not map to label"
    assert "ai_context" not in field, "empty synonyms must not map to ai_context"
    metric = model.get("metrics")[0]
    assert "description" not in metric, "empty comment must not map to description"
    assert "ai_context" not in metric, "empty synonyms must not map to ai_context"


def test_stash_escapes_non_ascii_as_lowercase_hex():
    # The stash blob is pure ASCII: non-ASCII is escaped to \\uXXXX. The
    # stash blob must be byte-identical, so a non-ASCII stashed value (here a `filter`
    # literal) escapes rather than emitting raw UTF-8.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "filter: \"region = 'caf\u00e9'\"\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
    )
    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    # Extract the stash blob and assert the exact expected bytes:
    # the non-ASCII char is escaped as lowercase \\u00e9 (a single backslash + 5 chars),
    # not emitted raw. (Comparing the parsed `data` string sidesteps YAML's own quoting.)
    out = parse_yaml(ossie_yaml)
    model = out
    exts = model.get("custom_extensions")
    blob = exts[0].get("data")
    assert blob == "{\"_v\": 1, \"filter\": \"region = 'caf\\u00e9'\"}", \
        "stash blob must use a lowercase \\u escape"
    # And it still round-trips back to the original view.
    back = convert_ossie_to_metric_view(ossie_yaml, None).yaml
    assert parse_yaml(mv) == parse_yaml(back)


def test_stash_preserves_a_value_containing_a_literal_unicode_escape():
    # A stashed value may itself contain the text of a unicode escape. Serialized, that is a
    # DOUBLED backslash, so the hex-lowercasing pass must not treat it as a real escape --
    # doing so silently lowercases the value's own characters.
    # Single-quoted YAML: a backslash is an ordinary character there, so `filter` really holds
    # the six characters \ u A B C D rather than the character U+ABCD.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "filter: 'tag = \\uABCD'\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
    )
    parsed_in = parse_yaml(mv)
    original = parsed_in.get("filter")
    # Guard the fixture itself: the value must contain a real backslash for this to be a test.
    assert "\\" in original, \
        "test setup: filter must hold a literal backslash, got: " + original

    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    back = convert_ossie_to_metric_view(ossie_yaml, None).yaml
    restored = parse_yaml(back)
    assert restored.get("filter") == original, \
        "a literal unicode-escape sequence in a stashed value must survive unchanged"


def test_join_on_takes_precedence_over_using():
    # Metric View validation requires only that one of `on`/`using` is present, so both may be
    # set. Databricks resolves the criteria from `on` when it is present, so the converter must
    # decompose `on` and ignore `using` -- otherwise the relationship joins on other columns.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- name: cust\n"
        + "  source: c.s.customer\n"
        + "  on: source.o_custkey = cust.c_custkey\n"
        + "  using: [nation_key]\n"
        + "dimensions:\n"
        + "- {name: c_name, expr: cust.c_name}\n"
        + "measures:\n"
        + "- {name: cnt, expr: COUNT(1)}\n"
    )
    out = parse_yaml(convert_metric_view_to_ossie(mv, None).yaml)
    model = out
    rels = model.get("relationships")
    rel = rels[0]
    assert rel.get("from_columns") == ["o_custkey"], \
        "`on` must win over `using`: expected the o_custkey/c_custkey pair"
    assert rel.get("to_columns") == ["c_custkey"], \
        "`on` must win over `using`: expected the o_custkey/c_custkey pair"


def test_measure_rewrite_leaves_string_literals_alone():
    # The fact qualifier is added/stripped by rewriting the measure expression. That rewrite must
    # skip string literals: rewriting inside one changes the predicate and therefore the value.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
        + "measures:\n"
        + "- name: tagged\n"
        + "  expr: \"SUM(IF(source.region = 'source.us', 1, 0))\"\n"
    )
    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    assert "'source.us'" in ossie_yaml, \
        "a literal mentioning the qualifier must not be rewritten, got:\n" + ossie_yaml
    # The literal also survives the trip back. Note the *code* qualifier is normalized on the
    # way through (`source.region` -> bare `region`, the Metric View idiom for fact columns);
    # only the literal is required to come back byte-identical.
    back = convert_ossie_to_metric_view(ossie_yaml, None).yaml
    assert "'source.us'" in back, "the literal must survive the round trip, got:\n" + back


def nested_join_model(metric_expr):
    """A fact with `customer` joined to it and `region` nested under `customer`, plus one metric."""
    return ("version: \"0.2.0.dev0\"\n"
            + "name: sales\n"
            + "datasets:\n"
            + "  - name: lineitem\n"
            + "    source: cat.sch.lineitem\n"
            + "    fields:\n"
            + "      - name: l_key\n"
            + "        expression:\n"
            + "          dialects: [{dialect: DATABRICKS, expression: l_key}]\n"
            + "  - name: customer\n"
            + "    source: cat.sch.customer\n"
            + "    primary_key: [c_key]\n"
            + "  - name: region\n"
            + "    source: cat.sch.region\n"
            + "    primary_key: [r_key]\n"
            + "relationships:\n"
            + "  - name: l_to_c\n"
            + "    from: lineitem\n"
            + "    to: customer\n"
            + "    from_columns: [c_key]\n"
            + "    to_columns: [c_key]\n"
            + "  - name: c_to_r\n"
            + "    from: customer\n"
            + "    to: region\n"
            + "    from_columns: [r_key]\n"
            + "    to_columns: [r_key]\n"
            + "metrics:\n"
            + "  - name: pop\n"
            + "    expression:\n"
            + "      dialects: [{dialect: DATABRICKS, expression: \"" + metric_expr + "\"}]\n")


def first_measure_expr(view):
    mv = view
    measures = mv.get("measures")
    assert not (measures is None or len(measures) == 0), "expected one measure, got: " + str(mv)
    return measures[0].get("expr")


def test_export_qualifies_a_nested_measure_with_the_full_join_path():
    # A Metric View addresses a nested join column by its full path, so a metric naming the
    # dataset must come out as `customer.region.population`.
    assert first_measure_expr(export(nested_join_model("SUM(region.population)"), None)) \
        == "SUM(customer.region.population)"


def test_export_does_not_qualify_a_measure_path_twice():
    # The rewrite used to run one dataset name at a time, so it matched the `region.` inside the
    # path it had just written and produced `SUM(customer.customer.region.population)` -- silently,
    # with no notice. An expression that already carries the full path (what an import leaves
    # behind) must come out unchanged.
    assert first_measure_expr(
        export(nested_join_model("SUM(customer.region.population)"), None)) \
        == "SUM(customer.region.population)"


def test_nested_qualified_measure_round_trips_both_ways():
    mv = (
        "version: '1.1'\n"
        + "source: cat.sch.lineitem\n"
        + "joins:\n"
        + "- name: customer\n"
        + "  source: cat.sch.customer\n"
        + "  using: [c_key]\n"
        + "  joins:\n"
        + "  - name: region\n"
        + "    source: cat.sch.region\n"
        + "    using: [r_key]\n"
        + "dimensions:\n"
        + "- {name: l_key, expr: l_key}\n"
        + "measures:\n"
        + "- {name: pop, expr: SUM(customer.region.population)}\n"
    )
    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    # The import de-aliases the path down to the dataset that owns the column...
    assert "SUM(region.population)" in ossie_yaml, \
        "import should de-alias the join path to the dataset name, got:\n" + ossie_yaml
    # ...and the export puts it back, so the measure survives MV -> Ossie -> MV.
    assert first_measure_expr(parse_yaml(convert_ossie_to_metric_view(ossie_yaml, None).yaml)) \
        == "SUM(customer.region.population)"


def test_cascade_drop_ignores_a_dropped_name_inside_a_string_literal():
    # A field dropped for lacking a usable dialect must not take an unrelated measure with it just
    # because its name appears in a string literal: `'us'` is not a reference to a column `us`.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: sales\n"
        + "datasets:\n"
        + "  - name: orders\n"
        + "    source: cat.sch.orders\n"
        + "    fields:\n"
        + "      - name: amount\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: amount}]\n"
        + "      - name: us\n"
        + "        expression:\n"
        + "          dialects: [{dialect: SNOWFLAKE, expression: us_col}]\n"
        + "metrics:\n"
        + "  - name: amt_us\n"
        + "    expression:\n"
        + "      dialects:\n"
        + "        - dialect: DATABRICKS\n"
        + "          expression: \"SUM(IF(region = 'us', amount, 0))\"\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    assert first_measure_expr(parse_yaml(result.yaml)) == "SUM(IF(region = 'us', amount, 0))"
    for notice in result.notices:
        assert not ("amt_us" in notice and "references dropped" in notice), \
            "the measure only mentions 'us' in a literal, got notice: " + notice


def test_cascade_drop_matches_a_dropped_name_case_insensitively():
    # Databricks SQL identifiers are case-insensitive. A metric that references a dropped field in a
    # different case (COUNT(DISTINCT REGION_NAME) over a dropped region_name) must cascade-drop
    # rather than survive as a dangling reference.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: m\n"
        + "datasets:\n"
        + "  - name: d\n"
        + "    source: cat.sch.t\n"
        + "    fields:\n"
        + "      - name: id\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: id}]\n"
        + "      - name: region_name\n"
        + "        expression:\n"
        + "          dialects: [{dialect: T_SQL, expression: region_name}]\n"
        + "metrics:\n"
        + "  - name: region_count\n"
        + "    expression:\n"
        + "      dialects: [{dialect: DATABRICKS, expression: COUNT(DISTINCT REGION_NAME)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    # The measure references the dropped field in upper case, so it is cascade-dropped.
    assert "measures" not in view, \
        "region_count must cascade-drop, got: " + str(view.get("measures"))
    # The unrelated dimension survives.
    assert len(view.get("dimensions")) == 1
    assert cascade_notice("measure", "region_count", "region_name") in result.notices, \
        str(result.notices)


def test_cascade_drop_keeps_a_function_call_named_like_a_dropped_field():
    # A dropped field whose name collides with a SQL function token in a surviving expression must
    # not cascade-drop it: COUNT_IF(YEAR(order_date) = 2026) calls the YEAR function, it is not a
    # reference to a dropped `year` field. The reference match excludes a NAME(...) function call.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: m\n"
        + "datasets:\n"
        + "  - name: d\n"
        + "    source: cat.sch.t\n"
        + "    fields:\n"
        + "      - name: order_date\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: order_date}]\n"
        + "      - name: year\n"
        + "        expression:\n"
        + "          dialects: [{dialect: T_SQL, expression: year}]\n"
        + "metrics:\n"
        + "  - name: orders_2026\n"
        + "    expression:\n"
        + "      dialects: [{dialect: DATABRICKS, expression: COUNT_IF(YEAR(order_date) = 2026)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    measures = view.get("measures")
    # YEAR(...) is a function call, not a reference to the dropped `year`, so the measure survives.
    assert len(measures) == 1, result.yaml
    assert measures[0].get("name") == "orders_2026"


def test_cascade_drop_keeps_a_survivor_differing_from_a_dropped_field_only_in_case():
    # A dropped field and a surviving dimension whose names differ only in case can coexist. The
    # survivor's bare self-reference (`region`) is its own name, not a reference to the dropped
    # `REGION`, so the self-guard is case-folded and the survivor is kept.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: m\n"
        + "datasets:\n"
        + "  - name: d\n"
        + "    source: cat.sch.t\n"
        + "    fields:\n"
        + "      - name: REGION\n"
        + "        expression:\n"
        + "          dialects: [{dialect: T_SQL, expression: REGION}]\n"
        + "      - name: region\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: region}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    dims = view.get("dimensions")
    assert len(dims) == 1, result.yaml
    assert dims[0].get("name") == "region"


def test_cascade_drop_drops_a_measure_named_like_a_dropped_dimension():
    # A measure that shares a dropped dimension's name -- same name, SAME case -- is not a
    # self-reference: the guard is scoped to the column's own kind, so the measure's reference to
    # the dropped dimension is detected and the measure is cascade-dropped rather than emitted
    # dangling. The same-case clash is what pins the kind-scoping: a case-only difference already
    # cascade-drops without it (the pre-fix guard was case-sensitive), so this must match case.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: m\n"
        + "datasets:\n"
        + "  - name: d\n"
        + "    source: cat.sch.t\n"
        + "    fields:\n"
        + "      - name: id\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: id}]\n"
        + "      - name: region\n"
        + "        expression:\n"
        + "          dialects: [{dialect: T_SQL, expression: region}]\n"
        + "metrics:\n"
        + "  - name: region\n"
        + "    expression:\n"
        + "      dialects: [{dialect: DATABRICKS, expression: SUM(region)}]\n"
    )
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    assert "measures" not in view, \
        "measure 'region' must cascade-drop, got: " + str(view.get("measures"))
    assert cascade_notice("measure", "region", "region") in result.notices, str(result.notices)


def test_stash_preserves_a_backslash_before_a_unicode_escape():
    # The stash lowercases the hex of the JSON writer's \\uXXXX escapes. That rewrite must re-emit
    # any escaped-backslash run in front of the escape verbatim; it used to halve it, so a value
    # holding a literal backslash came back as the six characters "u00e9".
    value = "tag = \\" + "\u00e9"
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "filter: '" + value + "'\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
    )
    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    back = parse_yaml(convert_ossie_to_metric_view(ossie_yaml, None).yaml)
    assert back.get("filter") == value, \
        "the stashed filter must survive MV -> Ossie -> MV byte-for-byte"


def test_export_warns_when_a_column_ai_context_object_is_dropped():
    # `ai_context` is `string | object`; only the object's `synonyms` has a Metric View slot, so
    # the other members are dropped -- and the README promises a notice when that happens. The
    # same goes for foreign-vendor extensions on a metric.
    osi = (
        "version: \"0.2.0.dev0\"\n"
        + "name: sales\n"
        + "datasets:\n"
        + "  - name: orders\n"
        + "    source: cat.sch.orders\n"
        + "    fields:\n"
        + "      - name: amount\n"
        + "        expression:\n"
        + "          dialects: [{dialect: DATABRICKS, expression: amount}]\n"
        + "        ai_context:\n"
        + "          instructions: do not use this column\n"
        + "          synonyms: [amt]\n"
        + "metrics:\n"
        + "  - name: total\n"
        + "    expression:\n"
        + "      dialects: [{dialect: DATABRICKS, expression: SUM(amount)}]\n"
        + "    ai_context:\n"
        + "      examples: [how much did we sell]\n"
        + "    custom_extensions:\n"
        + "      - vendor_name: SNOWFLAKE\n"
        + "        data: \"{}\"\n"
    )
    notices = convert_ossie_to_metric_view(osi, None).notices
    assert ("[field 'amount'] ai_context [instructions] dropped "
            + "(only 'synonyms' has a Metric View counterpart)") in notices, \
        "expected a notice for the field's dropped ai_context members, got: " + str(notices)
    assert ("[metric 'total'] ai_context [examples] dropped "
            + "(only 'synonyms' has a Metric View counterpart)") in notices, \
        "expected a notice for the metric's dropped ai_context members, got: " + str(notices)
    assert "[metric 'total'] foreign-vendor custom_extensions dropped" in notices, \
        "expected a notice for the metric's foreign-vendor extensions, got: " + str(notices)


def test_import_validates_a_join_source_the_way_the_export_does():
    # A 2-part join source used to import cleanly and then fail on the way back out, so a view
    # that imported could not be exported. Reject it at the same point either direction would.
    mv = (
        "version: '1.1'\n"
        + "source: cat.sch.orders\n"
        + "joins:\n"
        + "- name: d\n"
        + "  source: sch.d\n"
        + "  using: [k]\n"
        + "dimensions:\n"
        + "- {name: o_status, expr: o_orderstatus}\n"
    )
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie(mv, None)
    e = excinfo.value
    assert "3-part" in str(e), \
        "expected the same source-shape error the export raises, got: " + str(e)


# -- import direction: coverage ported from the Python converter suite -----


def test_import_accepts_fields_as_alias_for_dimensions():
    # `fields:` is a v1.1 alias for `dimensions:` (the form the Databricks docs use); the fact
    # dataset's fields come from `fields` when `dimensions` is absent.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "fields:\n"
        + "- {name: region, expr: region}\n"
    )
    model = import_mv(mv)
    fields = model.get("datasets")[0].get("fields")
    assert len(fields) == 1
    assert fields[0].get("name") == "region"


def test_import_warns_and_uses_dimensions_when_both_dimensions_and_fields_set():
    # `fields` is a v1.1 alias for `dimensions`; if a malformed view sets both, `dimensions` wins
    # and the `fields` list is ignored with a notice.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "dimensions:\n"
        + "- {name: kept, expr: kept}\n"
        + "fields:\n"
        + "- {name: ignored, expr: ignored}\n"
    )
    r = convert_metric_view_to_ossie(mv, None)
    assert any("both 'dimensions' and 'fields' are set" in n for n in r.notices), str(r.notices)
    model = parse_yaml(r.yaml)
    fields = model.get("datasets")[0].get("fields")
    assert len(fields) == 1
    assert fields[0].get("name") == "kept"


def test_import_rejects_malformed_source():
    # The top-level `source` must be a 3-part catalog.schema.table or a SELECT/WITH subquery: a
    # 2-part name, an empty part, or a whitespace-laden part is rejected (the same rule the export
    # applies to a join source; see test_import_validates_a_join_source_the_way_the_export_does).
    for src in ["a.b", ".s.t", "c..t", "c.s.", "cat . sch . tbl"]:
        mv = "version: '1.1'\nsource: " + src + "\n"
        with pytest.raises(ConversionError) as excinfo:
            convert_metric_view_to_ossie(mv, None)
        e = excinfo.value
        assert "must be a 3-part" in str(e), src + " -> " + str(e)


def test_import_accepts_a_with_subquery_source():
    # A `WITH(...)` subquery with no space after the keyword is still recognized as SQL (not
    # mistaken for a dotted identifier) and passes through as the fact source.
    mv = (
        "version: '1.1'\n"
        + "source: WITH(t AS (SELECT 1 AS a)) SELECT a FROM t\n"
    )
    model = import_mv(mv)
    source = model.get("datasets")[0].get("source")
    assert source.startswith("WITH("), source


def test_import_rejects_a_join_named_source():
    # `source` is reserved for the fact; a join named `source` (case-insensitively) is rejected
    # rather than colliding with the fact alias.
    for name in ["source", "SOURCE", "Source"]:
        mv = ("version: '1.1'\nsource: c.s.fact\n"
              + "joins:\n- {name: " + name + ", source: c.s.dim, using: [id]}\n")
        with pytest.raises(ConversionError) as excinfo:
            convert_metric_view_to_ossie(mv, None)
        e = excinfo.value
        assert "reserved" in str(e), name + " -> " + str(e)


def test_import_rejects_a_duplicate_join_name():
    # Join names and the source must be distinct case-insensitively; a join named like the fact
    # (derived from the source's last part) collides and is rejected.
    mv = ("version: '1.1'\nsource: c.s.fact\n"
          + "joins:\n- {name: fact, source: c.s.other, using: [id]}\n")
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie(mv, None)
    e = excinfo.value
    assert "Duplicate dataset/join name" in str(e), str(e)


def test_import_flips_one_to_many_join_and_stashes_the_grain():
    # A one_to_many MV join becomes an Apache Ossie relationship with the MANY side as `from` (the
    # joined table) and the source/grain on `to`. The cardinality rides the DATABRICKS stash and
    # the grain is recorded at model level, so a round trip restores one_to_many.
    mv = ("version: '1.1'\n"
          + "source: c.s.orders\n"
          + "joins:\n"
          + "- {name: line_items, source: c.s.line_items, "
          + "on: 'source.order_id = line_items.l_order_id', cardinality: one_to_many}\n"
          + "measures:\n- {name: order_count, expr: COUNT(*)}\n")
    ossie_yaml = convert_metric_view_to_ossie(mv, None).yaml
    model = parse_yaml(ossie_yaml)
    rel = model.get("relationships")[0]
    assert rel.get("from") == "line_items"
    assert rel.get("to") == "orders"
    assert rel.get("from_columns")[0] == "l_order_id"
    assert rel.get("to_columns")[0] == "order_id"
    # The stashed cardinality and grain restore one_to_many on the way back to a Metric View.
    back = parse_yaml(convert_ossie_to_metric_view(ossie_yaml, None).yaml)
    rt_join = back.get("joins")[0]
    assert rt_join.get("cardinality") == "one_to_many"


def test_import_recovers_unique_key_from_at_most_one_match():
    # A many_to_one join whose `rely.at_most_one_match` is set records the join key as a
    # unique_keys entry on the target dataset (the inverse of how export sets at_most_one_match).
    mv = ("version: '1.1'\n"
          + "source: c.s.orders\n"
          + "joins:\n"
          + "- {name: customer, source: c.s.customer, on: 'source.cid = customer.id', "
          + "rely: {at_most_one_match: true}}\n"
          + "dimensions:\n- {name: cn, expr: customer.name}\n")
    model = import_mv(mv)
    customer = None
    for ds in model.get("datasets"):
        if "customer" == ds.get("name"):
            customer = ds
    assert customer is not None, str(model)
    unique_keys = customer.get("unique_keys")
    assert unique_keys[0][0] == "id"


def test_import_rejects_non_string_scalars_where_strings_required():
    # A non-string scalar where a string is required (a join name, a measure expr) raises a clean
    # ConversionError naming the field, not a raw cast failure.
    bad_join_name = ("version: '1.1'\nsource: c.s.f\n"
                     + "joins:\n- {name: 5, source: c.s.d, using: [id]}\n")
    with pytest.raises(ConversionError) as excinfo1:
        convert_metric_view_to_ossie(bad_join_name, None)
    e1 = excinfo1.value
    assert "must be a string" in str(e1), str(e1)
    bad_measure_expr = ("version: '1.1'\nsource: c.s.o\n"
                        + "measures:\n- {name: rev, expr: 5}\n")
    with pytest.raises(ConversionError) as excinfo2:
        convert_metric_view_to_ossie(bad_measure_expr, None)
    e2 = excinfo2.value
    assert "must be a string" in str(e2), str(e2)


# -- import direction: expression dialects ---------------------------------


def dialect_of(column):
    """The single dialect entry of a converted field or metric, as [dialect, expression]."""
    dialects = column.get("expression").get("dialects")
    assert len(dialects) == 1, str(dialects)
    d = dialects[0]
    return [d.get("dialect"), d.get("expression")]


def test_import_labels_each_expression_with_its_most_portable_dialect():
    # A plain expression is labeled OSSIE_SQL_2026, standard SQL outside the Ossie language
    # ANSI_SQL, and anything Databricks-specific stays DATABRICKS.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "dimensions:\n"
        + "- {name: status, expr: o_status}\n"
        + "- {name: name_length, expr: CHAR_LENGTH(o_name)}\n"
        + "- {name: order_month, expr: \"date_format(o_date, 'yyyy-MM')\"}\n"
        + "measures:\n"
        + "- {name: revenue, expr: SUM(o_amount)}\n"
    )
    model = import_mv(mv)
    fields = model.get("datasets")[0].get("fields")
    assert dialect_of(fields[0]) == ["OSSIE_SQL_2026", "o_status"]
    assert dialect_of(fields[1]) == ["ANSI_SQL", "CHAR_LENGTH(o_name)"]
    assert dialect_of(fields[2]) == ["DATABRICKS", "date_format(o_date, 'yyyy-MM')"]
    revenue = model.get("metrics")[0]
    assert dialect_of(revenue) == ["OSSIE_SQL_2026", "SUM(o_amount)"]


def test_import_keeps_window_and_partition_measures_databricks():
    # A window or partition measure's value depends on settings that live only in the DATABRICKS
    # stash, so its expression keeps the DATABRICKS label even when the text alone is portable.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "dimensions:\n"
        + "- {name: order_date, expr: o_date}\n"
        + "measures:\n"
        + "- {name: revenue, expr: SUM(o_amount)}\n"
        + "- name: running_revenue\n"
        + "  expr: SUM(o_amount)\n"
        + "  window:\n"
        + "  - {order: order_date, range: cumulative, semiadditive: last}\n"
        + "- name: daily_revenue\n"
        + "  expr: SUM(o_amount)\n"
        + "  partition: {include: [order_date], outer_aggregate: SUM}\n"
    )
    assert dialects_by_name(mv) == {
        "order_date": "OSSIE_SQL_2026", "revenue": "OSSIE_SQL_2026",
        "running_revenue": "DATABRICKS", "daily_revenue": "DATABRICKS"}


def test_import_keeps_every_measure_of_a_filtered_view_databricks():
    # A view filter changes every measure's value and lives only in the DATABRICKS stash, so the
    # measures stay DATABRICKS; a dimension's per-row value does not depend on it.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "filter: o_status = 'done'\n"
        + "dimensions:\n"
        + "- {name: status, expr: o_status}\n"
        + "measures:\n"
        + "- {name: revenue, expr: SUM(o_amount)}\n"
    )
    assert dialects_by_name(mv) == {"status": "OSSIE_SQL_2026", "revenue": "DATABRICKS"}


def test_import_keeps_names_defined_by_the_view_databricks():
    # Portable only when every name is a column or `dataset.column`: a parameter, a dimension
    # that may shadow a column, the `source.` fact alias, and a struct field are not. A
    # dimension naming itself (`status`) reads the source column.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "parameters:\n"
        + "- {name: min_amount, data_type: DOUBLE}\n"
        + "joins:\n"
        + "- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}\n"
        + "dimensions:\n"
        + "- {name: status, expr: UPPER(status)}\n"
        + "- {name: status_code, expr: 'LEFT(status, 1)'}\n"
        + "- {name: source_status, expr: UPPER(source.o_status)}\n"
        + "- {name: city, expr: address.city}\n"
        + "- {name: customer_name, expr: UPPER(customer.c_name)}\n"
        + "measures:\n"
        + "- {name: big_orders, expr: 'COUNT(IF(o_amount > min_amount, 1, NULL))'}\n"
        + "- {name: revenue, expr: SUM(o_amount)}\n"
    )
    assert dialects_by_name(mv) == {
        "status": "OSSIE_SQL_2026", "status_code": "DATABRICKS",
        "source_status": "DATABRICKS", "city": "DATABRICKS", "customer_name": "OSSIE_SQL_2026",
        "big_orders": "DATABRICKS", "revenue": "OSSIE_SQL_2026"}


def test_import_keeps_measure_references_and_shadowed_qualified_columns_databricks():
    # A bare name outside an aggregate refers to another measure, which may carry a window; and
    # `source.o_amount` reads the raw column, where the Apache Ossie `orders.o_amount` would name
    # the shadowing `o_amount` dimension.
    mv = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}\n"
        + "dimensions:\n"
        + "- {name: order_date, expr: o_date}\n"
        + "- {name: o_amount, expr: o_amount * 100}\n"
        + "- {name: c_acctbal, expr: customer.c_acctbal * 2}\n"
        + "measures:\n"
        + "- {name: cost, expr: SUM(cost)}\n"
        + "- {name: row_count, expr: COUNT(*)}\n"
        + "- {name: avg_cost, expr: cost / row_count}\n"
        + "- name: running_cost\n"
        + "  expr: SUM(cost)\n"
        + "  window:\n"
        + "  - {order: order_date, range: cumulative, semiadditive: last}\n"
        + "- {name: running_x2, expr: running_cost * 2}\n"
        + "- {name: raw_amount, expr: SUM(source.o_amount)}\n"
        + "- {name: raw_balance, expr: SUM(customer.c_acctbal)}\n"
    )
    assert dialects_by_name(mv) == {
        "order_date": "OSSIE_SQL_2026", "o_amount": "OSSIE_SQL_2026",
        "c_acctbal": "OSSIE_SQL_2026", "cost": "OSSIE_SQL_2026", "row_count": "OSSIE_SQL_2026",
        "avg_cost": "DATABRICKS", "running_cost": "DATABRICKS", "running_x2": "DATABRICKS",
        "raw_amount": "DATABRICKS", "raw_balance": "DATABRICKS"}


def test_import_keeps_views_with_unseen_dimension_names_databricks():
    # A joined wildcard or a derived name adds dimensions the import cannot see, and any of them
    # may shadow a column (`customer.*` adds `comment`), so nothing in the view is portable. A
    # fact wildcard only adds the fact's own columns under their own names.
    head = (
        "version: '1.1'\n"
        + "source: c.s.orders\n"
        + "joins:\n"
        + "- {name: customer, source: c.s.customer, on: source.o_custkey = customer.c_custkey}\n"
        + "dimensions:\n"
        + "- {name: status, expr: o_status}\n"
    )
    measures = "measures:\n- {name: commenters, expr: COUNT(DISTINCT comment)}\n"
    hidden = {"status": "DATABRICKS", "commenters": "DATABRICKS"}
    assert dialects_by_name(head + "- expr: customer.*\n" + measures) == hidden
    assert dialects_by_name(head + "- expr: customer.comment\n" + measures) == hidden
    assert dialects_by_name(head + "- expr: source.*\n" + measures) \
        == {"status": "OSSIE_SQL_2026", "commenters": "OSSIE_SQL_2026"}


def dialects_by_name(mv):
    """The dialect label of every field and metric imported from ``mv``, by name."""
    model = import_mv(mv)
    columns = []
    for ds in model.get("datasets"):
        columns.extend(ds.get("fields", []))
    columns.extend(model.get("metrics", []))
    out = {}
    for column in columns:
        out[column.get("name")] = dialect_of(column)[0]
    return out


def hello_world_model(dialect):
    """A single sales dataset and one SUM metric, written in ``dialect``."""
    return ("version: 0.2.0.dev0\n"
            + "name: hello_world\n"
            + "datasets:\n"
            + "- name: sales\n"
            + "  source: hello.public.sales\n"
            + "  fields:\n"
            + "  - {name: amount, expression: {dialects: [{dialect: " + dialect
            + ", expression: amount}]}}\n"
            + "metrics:\n"
            + "- {name: total_amount, expression: {dialects: [{dialect: " + dialect
            + ", expression: SUM(sales.amount)}]}}\n")


def test_portable_dialect_survives_ossie_to_metric_view_to_ossie():
    # Apache Ossie -> Metric View -> Apache Ossie keeps a portable expression portable: an
    # OSSIE_SQL_2026 model comes back OSSIE_SQL_2026, and an ANSI_SQL one comes back in the
    # Ossie language (ANSI SQL's portable subset) rather than as DATABRICKS.
    for dialect in ["OSSIE_SQL_2026", "ANSI_SQL"]:
        mv = convert_ossie_to_metric_view(hello_world_model(dialect), None).yaml
        back = import_mv(mv)
        amount = back.get("datasets")[0].get("fields")[0]
        total = back.get("metrics")[0]
        assert dialect_of(amount)[0] == "OSSIE_SQL_2026", dialect
        assert dialect_of(total)[0] == "OSSIE_SQL_2026", dialect


# -- hardening: strict schema/stash parsing and structured failures -------


def test_bare_null_optional_collections_convert_like_absent():
    # A present-but-null optional collection (the bare `relationships:` YAML idiom) must convert
    # like an absent key, as the base reader (as_list) did. Only a scalar/map there is rejected.
    yaml = ("version: '" + OSSIE_VERSION + "'\n"
            + "name: m\n"
            + "datasets:\n"
            + "- name: d\n"
            + "  source: c.s.t\n"
            + "  fields:\n"
            + "  - name: region\n"
            + "    expression: {dialects: [{dialect: DATABRICKS, expression: region}]}\n"
            + "relationships:\n"
            + "metrics:\n"
            + "- name: cnt\n"
            + "  expression: {dialects: [{dialect: DATABRICKS, expression: 'count(*)'}]}\n")
    mv = convert_ossie_to_metric_view(yaml, None).yaml
    assert "cnt" in mv, "expected the model to convert, got: " + mv


def test_invalid_ossie_input_has_structured_failure_details():
    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view("just a scalar", None)
    e = excinfo.value
    assert e.kind == ConversionErrorKind.INVALID_INPUT
    assert e.reason == "it is not a mapping at the root"


def test_malformed_schema_collections_are_rejected():
    assert_invalid_ossie_input(
        "version: '" + OSSIE_VERSION + "'\nsemantic_model:\n- name: m\n",
        "Legacy 'semantic_model' wrappers are not supported; place the model properties "
        + "directly at the document root")
    assert_invalid_ossie_input(
        "version: '" + OSSIE_VERSION + "'\ndatasets: []\n",
        "Apache Ossie model requires a string 'name' at the document root")
    assert_invalid_ossie_input(
        ossie_model_with_body("  datasets: nope\n"),
        "Model 'm': 'datasets' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body("  datasets:\n  - nope\n"),
        "Model 'm': 'datasets[0]' must be a mapping")

    dataset = (
        "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.t\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  relationships: nope\n"),
        "Model 'm': 'relationships' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  relationships:\n  - nope\n"),
        "Model 'm': 'relationships[0]' must be a mapping")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "    fields: nope\n"),
        "Dataset 'd': 'fields' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "    fields:\n    - nope\n"),
        "Dataset 'd': 'fields[0]' must be a mapping")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  metrics: nope\n"),
        "Model 'm': 'metrics' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset + "  metrics:\n  - nope\n"),
        "Model 'm': 'metrics[0]' must be a mapping")


def test_malformed_expression_collections_are_rejected():
    dataset_prefix = (
        "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.t\n"
        + "    fields:\n"
        + "    - name: id\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: nope\n"),
        "field 'id': 'expression' must be a mapping")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: {dialects: nope}\n"),
        "field 'id': 'expression.dialects' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body(dataset_prefix + "      expression: {dialects: [nope]}\n"),
        "field 'id': 'expression.dialects[0]' must be a mapping")

    valid_field = (dataset_prefix
                   + "      expression:\n"
                   + "        dialects:\n"
                   + "        - {dialect: DATABRICKS, expression: id}\n")
    assert_invalid_ossie_input(
        ossie_model_with_body(valid_field
                              + "  metrics:\n"
                              + "  - name: count_id\n"
                              + "    expression: {dialects: nope}\n"),
        "metric 'count_id': 'expression.dialects' must be a list")


def test_malformed_custom_extensions_are_rejected():
    valid_model_body = (
        "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.t\n"
        + "    fields:\n"
        + "    - name: id\n"
        + "      expression:\n"
        + "        dialects:\n"
        + "        - {dialect: DATABRICKS, expression: id}\n"
    )
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions: {vendor_name: DATABRICKS, data: '{}'}\n" + valid_model_body),
        "'custom_extensions' must be a list")
    assert_invalid_ossie_input(
        ossie_model_with_body("  custom_extensions: [nope]\n" + valid_model_body),
        "'custom_extensions[0]' must be a mapping")
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions:\n"
            + "  - {vendor_name: DATABRICKS, data: 123}\n"
            + valid_model_body),
        "DATABRICKS custom_extensions data must be a string")
    assert_invalid_ossie_input(
        ossie_model_with_body(
            "  custom_extensions:\n"
            + "  - {vendor_name: DATABRICKS, data: '[]'}\n"
            + valid_model_body),
        "DATABRICKS custom_extensions data must be a JSON object")

    for data in [
            "filter: x = 1",
            "{\"filter\": \"x\", \"filter\": \"y\"}",
            "{\"filter\": \"x\"} true"]:
        yaml = ossie_model_with_body(
            "  custom_extensions:\n"
            + "  - vendor_name: DATABRICKS\n"
            + "    data: '" + data + "'\n"
            + valid_model_body)
        with pytest.raises(ConversionError) as excinfo:
            convert_ossie_to_metric_view(yaml, None)
        e = excinfo.value
        assert e.kind == ConversionErrorKind.INVALID_INPUT
        assert e.reason.startswith("DATABRICKS custom_extensions data is not valid JSON:"), \
            e.reason


def test_duplicate_databricks_stash_keeps_first_and_warns():
    # Two DATABRICKS custom_extensions entries on one object is malformed, but rather than fail we
    # keep the first, ignore the rest, and warn so the dropped entry is not lost silently.
    osi = ossie_model_with_body(
        "  custom_extensions:\n"
        + "  - {vendor_name: DATABRICKS, data: '{\"filter\": \"a = 1\"}'}\n"
        + "  - {vendor_name: DATABRICKS, data: '{\"filter\": \"b = 2\"}'}\n"
        + "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.t\n"
        + "    fields:\n"
        + "    - {name: id, expression: {dialects: [{dialect: DATABRICKS, expression: id}]}}\n")
    result = convert_ossie_to_metric_view(osi, None)
    view = parse_yaml(result.yaml)
    assert view.get("filter") == "a = 1", "the first DATABRICKS entry must win"
    assert any("more than one DATABRICKS custom_extensions entry" in n
               for n in result.notices), str(result.notices)


def test_oversized_dataset_graph_is_rejected_before_recursive_traversal():
    body = "  datasets:\n"
    for i in range(ossie_converter_common.MAX_JOIN_NODES + 1):
        body += "  - {name: d" + str(i) + ", source: c.s.t" + str(i) + "}\n"
    body += "  relationships:\n"
    for i in range(ossie_converter_common.MAX_JOIN_NODES):
        body += ("  - {name: r" + str(i)
                 + ", from: d" + str(i)
                 + ", to: d" + str(i + 1)
                 + ", from_columns: [id], to_columns: [id]}\n")

    with pytest.raises(ConversionError) as excinfo:
        convert_ossie_to_metric_view(ossie_model_with_body(body), None)
    e = excinfo.value

    assert "at most 200 are supported" in str(e), str(e)


def test_duplicate_yaml_keys_are_rejected_at_every_depth():
    top_level = (
        "version: '" + OSSIE_VERSION + "'\n"
        + "version: '" + OSSIE_VERSION + "'\n"
        + "name: m\n"
    )
    nested = ossie_model_with_body(
        "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.first\n"
        + "    source: c.s.second\n")
    for yaml in [top_level, nested]:
        with pytest.raises(ConversionError) as excinfo:
            convert_ossie_to_metric_view(yaml, None)
        e = excinfo.value
        assert e.kind == ConversionErrorKind.INVALID_INPUT
        assert "duplicate key" in e.reason, e.reason


def test_reverse_ordered_drop_chain_is_propagated_without_repeated_full_scans():
    # Every field depends on the preceding field, but reverse source order means the old repeated
    # pass implementation could discover only one new drop per pass.
    chain_length = 400
    body = (
        "  datasets:\n"
        + "  - name: d\n"
        + "    source: c.s.d\n"
        + "    fields:\n"
    )
    for index in range(chain_length, 0, -1):
        body += ("    - name: d" + str(index) + "\n"
                 + "      expression:\n"
                 + "        dialects:\n"
                 + "        - {dialect: DATABRICKS, expression: d"
                 + str(index - 1) + "}\n")
    body += ("    - name: d0\n"
             + "      expression:\n"
             + "        dialects:\n"
             + "        - {dialect: SNOWFLAKE, expression: d0}\n"
             + "  metrics:\n"
             + "  - name: row_count\n"
             + "    expression:\n"
             + "      dialects:\n"
             + "      - {dialect: DATABRICKS, expression: count(*)}\n")

    result = convert_ossie_to_metric_view(ossie_model_with_body(body), None)
    view = parse_yaml(result.yaml)

    assert "dimensions" not in view, result.yaml
    assert len(view.get("measures")) == 1, result.yaml
    assert len(result.notices) == chain_length + 1, str(result.notices)
    cascade_notices = [notice for notice in result.notices
                       if "downstream of a dropped field/metric" in notice]
    assert len(cascade_notices) == chain_length, str(cascade_notices)
    for index in range(1, chain_length + 1):
        assert cascade_notices[index - 1] \
            == cascade_notice("dimension", "d" + str(index), "d" + str(index - 1))


def test_cascade_drop_preserves_dimension_then_measure_phase_order():
    osi = (
        "version: 0.2.0.dev0\n"
        + "name: m\n"
        + "datasets:\n"
        + "- name: d\n"
        + "  source: c.s.d\n"
        + "  fields:\n"
        + "    - {name: d1, expression: {dialects: "
        + "[{dialect: DATABRICKS, expression: bad_dim}]}}\n"
        + "    - {name: d0, expression: {dialects: "
        + "[{dialect: DATABRICKS, expression: 'measure(m1)'}]}}\n"
        + "    - {name: bad_dim, expression: {dialects: "
        + "[{dialect: SNOWFLAKE, expression: bad_dim}]}}\n"
        + "metrics:\n"
        + "- {name: m1, expression: {dialects: "
        + "[{dialect: DATABRICKS, expression: 'measure(bad_measure)'}]}}\n"
        + "- {name: m0, expression: {dialects: "
        + "[{dialect: DATABRICKS, expression: d0}]}}\n"
        + "- {name: bad_measure, expression: {dialects: "
        + "[{dialect: SNOWFLAKE, expression: bad_measure}]}}\n"
        + "- {name: keep, expression: {dialects: "
        + "[{dialect: DATABRICKS, expression: 'count(*)'}]}}\n"
    )

    result = convert_ossie_to_metric_view(osi, None)
    cascade_notices = [notice for notice in result.notices
                       if "downstream of a dropped field/metric" in notice]

    assert cascade_notices == [
        cascade_notice("dimension", "d1", "bad_dim"),
        cascade_notice("measure", "m1", "bad_measure"),
        cascade_notice("dimension", "d0", "m1"),
        cascade_notice("measure", "m0", "d0")]


def test_invalid_metric_view_input_has_structured_failure_details():
    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie("just a scalar", None)
    scalar_error = excinfo.value
    assert scalar_error.kind == ConversionErrorKind.INVALID_INPUT
    assert scalar_error.reason == "it is not a mapping at the root"

    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie("version: [1", None)
    parse_error = excinfo.value
    assert parse_error.kind == ConversionErrorKind.INVALID_INPUT
    assert parse_error.reason.startswith("failed to parse YAML:")

    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie("   ", None)
    empty_error = excinfo.value
    assert empty_error.kind == ConversionErrorKind.INVALID_INPUT
    assert empty_error.reason == "the input is empty"

    with pytest.raises(ConversionError) as excinfo:
        convert_metric_view_to_ossie("source: c.s.t", None)
    missing_version_error = excinfo.value
    assert missing_version_error.kind == ConversionErrorKind.INVALID_INPUT
    assert missing_version_error.reason == "it is missing the required 'version' field"


def test_reverse_direction_serialization_failures_are_internal_errors():
    invalid = value_that_fails_serialization()

    with pytest.raises(ConversionError) as excinfo:
        serialize_ossie(invalid, Notices())
    yaml_error = excinfo.value
    assert yaml_error.kind == ConversionErrorKind.INTERNAL_ERROR

    with pytest.raises(ConversionError) as excinfo:
        ossie_converter_common.write_stash({}, invalid)
    stash_error = excinfo.value
    assert stash_error.kind == ConversionErrorKind.INTERNAL_ERROR


def test_public_yaml_helpers_have_structured_failure_kinds():
    with pytest.raises(ConversionError) as excinfo:
        parse_yaml("version: [1")
    parse_error = excinfo.value
    assert parse_error.kind == ConversionErrorKind.INVALID_INPUT
    assert parse_error.reason == str(parse_error)
    # Java getCause() != null -> the exception is chained (raise ... from ...).
    assert parse_error.__cause__ is not None

    with pytest.raises(ConversionError) as excinfo:
        dump_yaml(value_that_fails_serialization())
    dump_error = excinfo.value
    assert dump_error.kind == ConversionErrorKind.INTERNAL_ERROR
    assert dump_error.reason == str(dump_error)
    assert dump_error.__cause__ is not None
