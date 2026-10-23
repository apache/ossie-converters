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

"""Apache Ossie -> Solid semantic model."""

import warnings

import pytest
import yaml
from conftest import (
    by_name,
    convert_quietly,
    example,
    fixture,
    solid_model_of,
)

from ossie_solid import ConversionError, convert_ossie_to_solid, convert_solid_to_ossie


@pytest.fixture(scope="module")
def from_fixture():
    """The TPC-DS Apache Ossie fixture exported back to Solid."""
    solid, _ = convert_quietly(convert_ossie_to_solid, fixture("tpcds_ossie.yaml"))
    return solid_model_of(solid)


@pytest.fixture(scope="module")
def from_example():
    """The repository's hand-authored TPC-DS model exported to Solid.

    It carries no SOLID stash, so this exercises every fallback path.
    """
    solid, _ = convert_quietly(
        convert_ossie_to_solid, example("tpcds_semantic_model.yaml")
    )
    return solid_model_of(solid)


def test_key_order_matches_solids_own_export_template(from_fixture):
    assert list(from_fixture) == [
        "name",
        "business_context",
        "model_llm_description",
        "tables",
        "metrics",
        "relationships",
        "example_queries",
        "benchmark_questions",
    ]


def test_structured_custom_instructions_take_solids_place_in_the_key_order():
    ossie, _ = convert_quietly(convert_solid_to_ossie, fixture("databricks_solid.yaml"))
    solid, _ = convert_quietly(convert_ossie_to_solid, ossie)
    model = solid_model_of(solid)
    assert list(model)[:4] == [
        "name", "business_context", "custom_instructions", "model_llm_description"]
    # Solid writes the structured list instead of the free text, never both.
    assert "custom_instructions" not in model["business_context"]


def test_edited_instructions_replace_the_structured_entries_with_a_warning():
    ossie, _ = convert_quietly(convert_solid_to_ossie, fixture("databricks_solid.yaml"))
    model = yaml.safe_load(ossie)
    model["ai_context"]["instructions"] = "Late means not shipped within two days."
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(model))
    restored = solid_model_of(solid)
    assert "custom_instructions" not in restored
    assert restored["business_context"]["custom_instructions"] == (
        "Late means not shipped within two days.")
    assert any("structured custom instructions recorded at import were not restored"
               in w for w in warnings_)


def test_table_key_order_matches_solids_own_export_template(from_fixture):
    assert list(by_name(from_fixture["tables"])["tpcds.public.store_sales"]) == [
        "name",
        "description",
        "manual_description",
        "synonyms",
        "primary_key",
        "quality_rank",
        "indexes",
        "dimensions",
        "facts",
    ]


# --- datasets ---------------------------------------------------------------------


def test_the_dataset_source_becomes_the_table_name(from_fixture):
    assert "tpcds.public.store_sales" in by_name(from_fixture["tables"])


def test_a_composite_primary_key_is_rejoined_into_one_scalar(from_fixture):
    table = by_name(from_fixture["tables"])["tpcds.public.store_sales"]
    assert table["primary_key"] == "ss_item_sk, ss_ticket_number"


def test_quality_rank_is_always_emitted_even_when_unknown(from_example):
    # The repository example has no SOLID stash, so no rank is known.
    assert all(t["quality_rank"] == "" for t in from_example["tables"])


def test_facts_is_always_emitted_even_when_empty(from_fixture):
    table = by_name(from_fixture["tables"])["tpcds.public.date_dim"]
    assert table["facts"] == []


def test_a_duplicate_dataset_name_is_rejected():
    # Relationships and metrics resolve datasets by name, so accepting a duplicate
    # would join against whichever one came last.
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    duplicate = dict(ossie["datasets"][0], source="tpcds.archive.store_sales")
    ossie["datasets"].append(duplicate)
    with pytest.raises(ConversionError, match="more than once"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


def test_unique_keys_are_dropped_with_a_warning():
    _, warnings_ = convert_quietly(
        convert_ossie_to_solid, example("tpcds_semantic_model.yaml")
    )
    assert any("unique_keys have no Solid equivalent" in w for w in warnings_)


def test_foreign_vendor_extensions_are_dropped_with_a_warning():
    _, warnings_ = convert_quietly(
        convert_ossie_to_solid, example("tpcds_semantic_model.yaml")
    )
    assert any("DBT, SALESFORCE" in w for w in warnings_)


# --- fields -----------------------------------------------------------------------


def test_the_dimension_block_decides_the_fact_dimension_split(from_fixture):
    table = by_name(from_fixture["tables"])["tpcds.public.store_sales"]
    assert "ss_customer_sk" in by_name(table["dimensions"])
    assert "ss_net_profit" in by_name(table["facts"])


def test_without_dimension_metadata_the_split_falls_back_to_solids_type_rule():
    # An Apache Ossie model whose fields carry no `dimension` block: numeric fields
    # become facts and the rest dimensions, which is how Solid itself splits them.
    ossie = yaml.safe_load(example("tpcds_semantic_model.yaml"))
    for dataset in ossie["datasets"]:
        for field in dataset["fields"]:
            field.pop("dimension", None)
    solid, _ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    table = by_name(solid_model_of(solid)["tables"])["tpcds.public.store_sales"]
    assert "ss_quantity" in by_name(table["facts"])
    assert "ss_customer_sk" in by_name(table["facts"])  # Integer -> fact
    table = by_name(solid_model_of(solid)["tables"])["tpcds.public.customer"]
    assert "c_email_address" in by_name(table["dimensions"])  # String -> dimension


def _split(fields, **dataset):
    """Export one dataset holding `fields`; return (dimension names, fact names)."""
    ossie = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [{"name": "orders", "source": "db.s.orders", "fields": [
            {"expression": {"dialects": [{"dialect": "ANSI_SQL",
                                          "expression": f["name"]}]}, **f}
            for f in fields
        ], **dataset}],
    }
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    table = solid_model_of(solid)["tables"][0]
    return ([c["name"] for c in table.get("dimensions") or []],
            [c["name"] for c in table["facts"]]), warnings_


def test_a_time_only_dimension_block_does_not_turn_other_fields_into_facts():
    # The Microsoft and Omni converters mark only temporal fields with a block, so
    # its absence elsewhere says nothing; the datatype decides those.
    split, _ = _split([
        {"name": "region", "datatype": "String"},
        {"name": "placed_at", "datatype": "DateTime", "dimension": {"is_time": True}},
        {"name": "amount", "datatype": "Decimal"},
    ])
    assert split == (["region", "placed_at"], ["amount"])


def test_an_untyped_field_follows_a_producer_that_marks_its_dimensions():
    # GoodData and OrionBelt put a block on every dimension, time or not, and carry no
    # datatype, so there the unmarked fields are the measures.
    split, _ = _split([
        {"name": "customer_sk", "dimension": {"is_time": False}},
        {"name": "sold_on", "dimension": {"is_time": True}},
        {"name": "quantity"},
    ])
    assert split == (["customer_sk", "sold_on"], ["quantity"])


def test_an_untyped_field_is_a_dimension_when_only_time_fields_are_marked():
    # With no datatype and no convention to read, an unmarked field cannot be shown to
    # be numeric, which leaves it a dimension, as an untyped column is in Solid.
    split, _ = _split([
        {"name": "order_key"},
        {"name": "ordered_on", "dimension": {"is_time": True}},
    ])
    assert split == (["order_key", "ordered_on"], [])


def test_a_block_on_a_temporal_field_counts_as_time_only_by_the_spec_default():
    # `dimension: {}` on a DateTime field defaults to is_time: true, so it is no sign
    # that the producer marks its other dimensions.
    split, _ = _split([
        {"name": "placed_at", "datatype": "DateTime", "dimension": {}},
        {"name": "order_key"},
    ])
    assert split == (["placed_at", "order_key"], [])


def _role_edit(edit):
    """Import the TPC-DS fixture, apply `edit` to the Apache Ossie model, export."""
    ossie, _ = convert_quietly(convert_solid_to_ossie, fixture("tpcds_solid.yaml"))
    model = yaml.safe_load(ossie)
    fields = {f["name"]: f for f in model["datasets"][0]["fields"]}
    edit(fields)
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(model))
    table = solid_model_of(solid)["tables"][0]
    return by_name(table.get("dimensions") or []), by_name(table["facts"]), warnings_


def test_the_stashed_role_survives_a_tool_dropping_the_dimension_block():
    # The block is optional, and a tool that does not read it may drop it; that is not
    # an edit, as a dropped `datatype` is not.
    def drop_blocks(fields):
        for field in fields.values():
            field.pop("dimension", None)

    dimensions, facts, warnings_ = _role_edit(drop_blocks)
    assert "ss_customer_sk" in dimensions
    assert "ss_net_profit" in facts
    assert not any("dimension block" in w for w in warnings_)


def test_a_dimension_block_added_in_ossie_wins_over_the_stashed_role():
    def mark(fields):
        fields["ss_net_profit"]["dimension"] = {"is_time": False}

    dimensions, facts, warnings_ = _role_edit(mark)
    assert "ss_net_profit" in dimensions and "ss_net_profit" not in facts
    assert any("'store_sales.ss_net_profit' was given a dimension block" in w
               for w in warnings_)


def test_a_computed_dimension_keeps_its_expression_until_it_is_made_plain():
    solid = yaml.safe_load(fixture("tpcds_solid.yaml"))
    solid["semantic_model"]["tables"][0]["dimensions"][0]["expression"] = (
        "CAST(ss_sold_date_sk AS VARCHAR)")
    name = solid["semantic_model"]["tables"][0]["dimensions"][0]["name"]
    ossie, _ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))

    back, warnings_ = convert_quietly(convert_ossie_to_solid, ossie)
    column = by_name(solid_model_of(back)["tables"][0]["dimensions"])[name]
    assert column["expression"] == "CAST(ss_sold_date_sk AS VARCHAR)"
    assert not any("computed field" in w for w in warnings_)

    # Edited back to a plain reference in Apache Ossie: the edit wins.
    model = yaml.safe_load(ossie)
    model["datasets"][0]["fields"][0]["expression"]["dialects"][0]["expression"] = name
    back, _ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(model))
    assert "expression" not in by_name(solid_model_of(back)["tables"][0]["dimensions"])[name]


def test_an_expression_added_to_a_plain_dimension_in_ossie_is_dropped_with_a_warning():
    # Solid dimensions are catalog columns. Only one that already carried an
    # expression in Solid keeps one.
    def compute(fields):
        fields["ss_customer_sk"]["expression"]["dialects"][0]["expression"] = (
            "ss_customer_sk + 1")

    dimensions, _, warnings_ = _role_edit(compute)
    assert "expression" not in dimensions["ss_customer_sk"]
    assert any("'store_sales.ss_customer_sk' is a computed field" in w for w in warnings_)


def test_a_metric_whose_columns_share_a_name_keeps_its_qualifiers():
    ossie = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {"name": name, "source": f"db.s.{name}", "fields": [
                {"name": "amount",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "amount"}]}},
            ]}
            for name in ("orders", "returns")
        ],
        "metrics": [{"name": "net", "expression": {"dialects": [
            {"dialect": "ANSI_SQL",
             "expression": "SUM(orders.amount) - SUM(returns.amount)"}]}}],
    }
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    metric = solid_model_of(solid)["metrics"][0]
    assert metric["expression"] == "SUM(orders.amount) - SUM(returns.amount)"
    assert metric["tables"] == ["db.s.orders", "db.s.returns"]
    assert any("'net': the dataset qualifiers could not be removed safely (ambiguous)"
               in w for w in warnings_)


def test_the_stashed_raw_type_wins_over_the_portable_datatype(from_fixture):
    table = by_name(from_fixture["tables"])["tpcds.public.store_sales"]
    assert by_name(table["facts"])["ss_sales_price"]["type"] == "NUMBER(7,2)"


def test_without_a_stash_the_type_is_derived_from_the_datatype(from_example):
    table = by_name(from_example["tables"])["tpcds.public.store_sales"]
    # The example is ANSI_SQL, so the ANSI type names are used, with a scale so the
    # column is still a decimal: a bare DECIMAL has a scale of 0.
    assert by_name(table["facts"])["ss_sales_price"]["type"] == "DECIMAL(38,9)"


def test_a_field_with_no_datatype_gets_an_empty_type_and_a_warning():
    solid, warnings_ = convert_quietly(
        convert_ossie_to_solid, example("tpcds_semantic_model.yaml")
    )
    table = by_name(solid_model_of(solid)["tables"])["tpcds.public.date_dim"]
    assert by_name(table["dimensions"])["d_quarter_name"]["type"] == ""
    assert any("d_quarter_name" in w and "no datatype" in w for w in warnings_)


def test_an_expression_only_fact_round_trips_as_an_expression(from_fixture):
    table = by_name(from_fixture["tables"])["tpcds.public.store_sales"]
    fact = by_name(table["facts"])["ss_discount_pct"]
    assert fact["expression"] == "1 - (ss_sales_price / NULLIF(ss_list_price, 0))"
    assert "type" not in fact


def test_a_computed_dimension_drops_its_expression_with_a_warning():
    _, warnings_ = convert_quietly(
        convert_ossie_to_solid, example("tpcds_semantic_model.yaml")
    )
    assert any("customer_full_name" in w and "computed field" in w for w in warnings_)


def test_a_field_label_is_dropped_with_a_warning():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["datasets"][0]["fields"][0]["label"] = "filter"
    _, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("label 'filter'" in w for w in warnings_)


# --- metrics ----------------------------------------------------------------------


def test_a_qualified_metric_is_written_back_with_bare_columns(from_fixture):
    metric = by_name(from_fixture["metrics"])["TOTAL_SALES"]
    assert metric["expression"] == "SUM(ss_ext_sales_price)"
    assert metric["tables"] == ["tpcds.public.store_sales"]


def test_the_owning_tables_are_recovered_from_the_expression_without_a_stash(
    from_example,
):
    metric = by_name(from_example["metrics"])["customer_lifetime_value"]
    assert metric["tables"] == ["tpcds.public.store_sales", "tpcds.public.customer"]


def test_metric_key_order_matches_solids_own_export_template(from_fixture):
    assert list(by_name(from_fixture["metrics"])["TOTAL_SALES"]) == [
        "name",
        "description",
        "expression",
        "synonyms",
        "tables",
    ]


# --- relationships ----------------------------------------------------------------


def test_a_flipped_relationship_is_restored_to_solids_left_right_order(from_fixture):
    relationship = from_fixture["relationships"][0]
    assert relationship["left_table"] == "tpcds.public.date_dim"
    assert relationship["right_table"] == "tpcds.public.store_sales"


def test_relationship_order_is_preserved(from_fixture):
    assert [r["right_table"] for r in from_fixture["relationships"]] == [
        "tpcds.public.store_sales",
        "tpcds.public.customer",
        "tpcds.public.item",
        "tpcds.public.store",
    ]


def test_a_relationship_naming_an_undeclared_dataset_is_rejected():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["relationships"][0]["to"] = "nope"
    with pytest.raises(ConversionError, match="not declared in 'datasets'"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


# --- dialect selection ------------------------------------------------------------


def test_the_dialect_recorded_at_import_is_reused(from_fixture):
    # NUMBER(7,2) is a Snowflake type name; reading it back proves the SNOWFLAKE
    # dialect recorded in the stash was used.
    table = by_name(from_fixture["tables"])["tpcds.public.store_sales"]
    assert by_name(table["facts"])["ss_sales_price"]["type"] == "NUMBER(7,2)"


def test_an_explicit_dialect_overrides_the_stash():
    ossie, _ = convert_quietly(convert_solid_to_ossie, fixture("databricks_solid.yaml"))
    solid, _ = convert_quietly(convert_ossie_to_solid, ossie, dialect="DATABRICKS")
    assert solid_model_of(solid)["name"] == "orders_analytics"


def test_forcing_a_dialect_the_model_does_not_carry_is_an_error():
    # The TPC-DS fixture is SNOWFLAKE-only, with no ANSI_SQL fallback to read.
    with pytest.raises(
        ConversionError, match="no DATABRICKS, ANSI_SQL or OSSIE_SQL_2026 expression"
    ):
        convert_ossie_to_solid(fixture("tpcds_ossie.yaml"), dialect="DATABRICKS")


def test_an_unsupported_dialect_is_rejected():
    with pytest.raises(ConversionError, match="Unsupported dialect"):
        convert_ossie_to_solid(fixture("tpcds_ossie.yaml"), dialect="ORACLE")


def test_a_dialect_listed_twice_with_different_sql_is_rejected():
    # Which of the two is read would otherwise come down to list order.
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    by_name(ossie["metrics"])["TOTAL_SALES"]["expression"]["dialects"].append(
        {"dialect": "SNOWFLAKE", "expression": "AVG(store_sales.ss_ext_sales_price)"})
    with pytest.raises(ConversionError,
                       match="TOTAL_SALES.*SNOWFLAKE dialect more than once"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


def test_an_exact_repeat_of_a_dialect_is_read_once():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    dialects = by_name(ossie["metrics"])["TOTAL_SALES"]["expression"]["dialects"]
    dialects.append(dict(dialects[0]))
    solid, _ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    metric = by_name(solid_model_of(solid)["metrics"])["TOTAL_SALES"]
    assert metric["expression"] == "SUM(ss_ext_sales_price)"


def test_a_missing_dialect_expression_falls_back_to_ansi_with_a_warning():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    field = ossie["datasets"][0]["fields"][0]
    field["expression"]["dialects"] = [
        {"dialect": "ANSI_SQL", "expression": "ss_sold_date_sk"}
    ]
    _, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("no SNOWFLAKE expression" in w for w in warnings_)


# --- edits made in Apache Ossie ----------------------------------------------------
#
# A stashed value that stands in for a live Apache Ossie field is used only while that
# field still holds what import wrote; once it is edited in Apache Ossie, the edit wins.


def _export_edited(edit, solid_yaml=None):
    """Import a Solid model (the TPC-DS fixture by default), apply `edit` to the Apache
    Ossie model, and export it back. Returns (Solid model, [warnings])."""
    ossie, _ = convert_quietly(
        convert_solid_to_ossie, solid_yaml or fixture("tpcds_solid.yaml"))
    ossie = yaml.safe_load(ossie)
    edit(ossie)
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    return solid_model_of(solid), warnings_


@pytest.mark.parametrize("instructions", ["Always filter to completed sales.", None])
def test_edited_or_removed_instructions_win_over_the_stashed_markup(instructions):
    def edit(ossie):
        if instructions is None:
            del ossie["ai_context"]["instructions"]
        else:
            ossie["ai_context"]["instructions"] = instructions

    solid, warnings_ = _export_edited(edit)
    assert solid["business_context"].get("custom_instructions") == instructions
    assert any("ai_context.instructions was" in w for w in warnings_)


@pytest.mark.parametrize("has_llm_description", [True, False])
def test_an_edited_description_replaces_the_one_it_was_written_from(
        has_llm_description):
    # Import writes the LLM description to `description`, or the customer one when
    # there is no LLM one; the edit lands in whichever that was.
    original = yaml.safe_load(fixture("tpcds_solid.yaml"))["semantic_model"]
    if not has_llm_description:
        del original["model_llm_description"]
    customer = original["business_context"]["model_description"].strip()

    solid, warnings_ = _export_edited(
        lambda ossie: ossie.update(description="Edited."),
        yaml.safe_dump({"semantic_model": original}))
    if has_llm_description:
        assert solid["model_llm_description"] == "Edited."
        assert solid["business_context"]["model_description"] == customer
    else:
        assert "model_llm_description" not in solid
        assert solid["business_context"]["model_description"] == "Edited."
    assert any("description was edited" in w for w in warnings_)


@pytest.mark.parametrize(
    "datatype, expected", [("String", "TEXT"), (None, "NUMBER(7,2)")])
def test_an_edited_datatype_wins_over_the_stashed_raw_type(datatype, expected):
    # A removed datatype is not an edit: a tool that does not know the field may drop
    # it, and that should not wipe the warehouse type.
    def edit(ossie):
        store_sales = by_name(ossie["datasets"])["store_sales"]
        field = by_name(store_sales["fields"])["ss_sales_price"]
        if datatype is None:
            del field["datatype"]
        else:
            field["datatype"] = datatype

    solid, warnings_ = _export_edited(edit)
    table = by_name(solid["tables"])["tpcds.public.store_sales"]
    assert by_name(table["facts"])["ss_sales_price"]["type"] == expected
    assert any("datatype was changed" in w for w in warnings_) == (datatype is not None)


def test_an_edited_metric_is_not_bound_to_its_stashed_tables():
    # ROW_COUNT is `COUNT(*)`, so import stashed its table. Edited to read another
    # dataset, it must bind to that one rather than to the stashed table.
    def edit(ossie):
        dialects = by_name(ossie["metrics"])["ROW_COUNT"]["expression"]["dialects"]
        dialects[0]["expression"] = "COUNT(DISTINCT customer.c_customer_id)"

    solid, warnings_ = _export_edited(edit)
    assert by_name(solid["metrics"])["ROW_COUNT"]["tables"] == ["tpcds.public.customer"]
    assert any("ROW_COUNT" in w and "edited in Apache Ossie" in w for w in warnings_)


# --- input validation --------------------------------------------------------------


def test_an_unknown_spec_version_is_read_with_a_warning():
    # Rejecting every version but the known ones would make every document fail the
    # day the spec releases one.
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["version"] = "0.2.0"
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert solid_model_of(solid)["tables"]
    assert any("declares Apache Ossie v0.2.0, which this converter does not know" in w
               for w in warnings_)


def test_a_document_with_no_version_is_rejected():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    del ossie["version"]
    with pytest.raises(ConversionError, match="missing required 'version'"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


def test_a_solid_document_is_rejected():
    with pytest.raises(ConversionError, match="Did you mean `ossie-solid import`"):
        convert_ossie_to_solid(fixture("tpcds_solid.yaml"))


def test_keys_outside_the_spec_are_dropped_with_a_warning():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["datasets"][0]["fields"][0]["grain"] = "day"
    ossie["metrics"][0]["ai_context"] = {"instructions": "Exclude refunds.",
                                         "owner": "finance"}
    _, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("'grain' is not part of Apache Ossie" in w for w in warnings_)
    assert any("ai_context.owner has no Solid equivalent" in w for w in warnings_)
    assert any("ai_context.instructions has no Solid equivalent" in w for w in warnings_)


def test_model_level_synonyms_are_dropped_with_a_warning():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["ai_context"]["synonyms"] = ["retail"]
    _, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("ai_context.synonyms has no Solid equivalent at the model level" in w
               for w in warnings_)


def _wrapped(document, extra_models=()):
    """`document` in the legacy layout: its model inside a `semantic_model` list."""
    model = {k: v for k, v in document.items() if k != "version"}
    return {"version": document["version"],
            "semantic_model": [model, *extra_models]}


def test_a_legacy_wrapped_document_is_read_with_a_warning():
    flat = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    solid, warnings_ = convert_quietly(
        convert_ossie_to_solid, yaml.safe_dump(_wrapped(flat))
    )
    assert any("legacy wrapped 'semantic_model' format" in w for w in warnings_)
    expected, _ = convert_quietly(convert_ossie_to_solid, fixture("tpcds_ossie.yaml"))
    assert solid == expected


def test_extra_models_in_a_legacy_wrapper_are_dropped_with_a_warning():
    flat = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    second = {k: v for k, v in flat.items() if k != "version"}
    second["name"] = "second_model"
    ossie = _wrapped(flat, extra_models=[second])
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("holds 2 semantic models" in w for w in warnings_)
    assert solid_model_of(solid)["name"] == "tpcds_retail_model"


@pytest.mark.parametrize(
    "wrapper", [[], {"name": "m"}, "m"], ids=["empty", "mapping", "string"]
)
def test_a_legacy_wrapper_that_is_not_a_non_empty_list_is_rejected(wrapper):
    ossie = {"version": "0.2.0.dev0", "semantic_model": wrapper}
    with pytest.raises(ConversionError, match="not a non-empty list"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


def test_a_legacy_wrapper_entry_that_is_not_a_mapping_is_rejected():
    ossie = {"version": "0.2.0.dev0", "semantic_model": ["m"]}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ConversionError, match="must be a mapping"):
            convert_ossie_to_solid(yaml.safe_dump(ossie))


# --- constructs Solid's format cannot hold -----------------------------------------


def test_a_metric_datatype_is_dropped_with_a_warning():
    """Solid types a metric by evaluating its formula, so a declared type has no slot."""
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["metrics"][0]["datatype"] = "Decimal"
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("datatype 'Decimal' has no Solid equivalent" in w for w in warnings_)
    assert "datatype" not in solid_model_of(solid)["metrics"][0]


def test_a_relationship_annotation_is_dropped_with_a_warning():
    """A Solid relationship carries only its tables and join keys."""
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    ossie["relationships"][0]["ai_context"] = {
        "instructions": "join only on settled sales",
        "synonyms": ["sold on"],
    }
    _, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    dropped = [w for w in warnings_ if "no Solid equivalent" in w and "relationship" in w]
    assert len(dropped) == 1
    assert "ai_context.instructions" in dropped[0]
    assert "ai_context.synonyms" in dropped[0]


def test_the_one_to_one_note_import_writes_is_not_reported_as_a_loss():
    """That note is this converter's own marker, not a user annotation.

    Import adds it where Apache Ossie's from/to direction is arbitrary; dropping it on
    the way back loses nothing, so it must not be reported as a dropped annotation.
    """
    solid = yaml.safe_load(fixture("tpcds_solid.yaml"))
    # Make both ends of the first join unique on their join columns -> a one-to-one.
    join = solid["semantic_model"]["relationships"][0]
    tables = {t["name"]: t for t in solid["semantic_model"]["tables"]}
    tables[join["left_table"]]["primary_key"] = ", ".join(join["join_keys"]["left"])
    tables[join["right_table"]]["primary_key"] = ", ".join(join["join_keys"]["right"])

    ossie, _ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))
    annotated = [
        r for r in yaml.safe_load(ossie)["relationships"]
        if (r.get("ai_context") or {}).get("instructions")
    ]
    assert annotated, "expected import to annotate the one-to-one"

    _, warnings_ = convert_quietly(convert_ossie_to_solid, ossie)
    assert not [w for w in warnings_ if "ai_context.instructions" in w], warnings_


# --- OSSIE_SQL_2026 ----------------------------------------------------------------


def _one_field_model(dialects, metric_dialects=None):
    """A minimal Apache Ossie document: one dataset `a` holding one Decimal field `amt`
    whose expression carries `dialects`, plus a metric `total` when `metric_dialects`
    is given."""
    model = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [{
            "name": "a",
            "source": "db.s.a",
            "fields": [{
                "name": "amt",
                "datatype": "Decimal",
                "expression": {"dialects": dialects},
            }],
        }],
    }
    if metric_dialects is not None:
        model["metrics"] = [
            {"name": "total", "expression": {"dialects": metric_dialects}}
        ]
    return model


def test_an_ossie_sql_2026_only_model_is_read_as_portable_sql():
    """OSSIE_SQL_2026 is the spec's portable dialect, so it is read like ANSI_SQL:
    silently, and without being reported as a dialect this converter cannot read."""
    ossie = _one_field_model(
        [{"dialect": "OSSIE_SQL_2026", "expression": "amt"}],
        metric_dialects=[{"dialect": "OSSIE_SQL_2026", "expression": "SUM(a.amt)"}],
    )
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert warnings_ == []
    model = solid_model_of(solid)
    assert model["tables"][0]["facts"][0]["name"] == "amt"
    assert model["metrics"][0]["expression"] == "SUM(amt)"
    assert model["metrics"][0]["tables"] == ["db.s.a"]


def test_ansi_sql_is_preferred_over_ossie_sql_2026():
    ossie = _one_field_model([], metric_dialects=[
        {"dialect": "OSSIE_SQL_2026", "expression": "SUM(a.amt)"},
        {"dialect": "ANSI_SQL", "expression": "MAX(a.amt)"},
    ])
    ossie["datasets"][0]["fields"][0]["expression"]["dialects"] = [
        {"dialect": "ANSI_SQL", "expression": "amt"}
    ]
    solid, _ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert solid_model_of(solid)["metrics"][0]["expression"] == "MAX(amt)"


def test_a_vendor_dialect_falling_back_to_ossie_sql_2026_names_it():
    """The fallback warning names the dialect actually read, not ANSI_SQL."""
    ossie = _one_field_model(
        [{"dialect": "OSSIE_SQL_2026", "expression": "amt"}],
        metric_dialects=[{"dialect": "OSSIE_SQL_2026", "expression": "SUM(a.amt)"}],
    )
    _, warnings_ = convert_quietly(
        convert_ossie_to_solid, yaml.safe_dump(ossie), dialect="SNOWFLAKE"
    )
    assert any(
        "'a.amt' has no SNOWFLAKE expression; the OSSIE_SQL_2026 one was used" in w
        for w in warnings_
    )
    assert any(
        "'total' has no SNOWFLAKE expression; the OSSIE_SQL_2026 one was used" in w
        for w in warnings_
    )


def test_a_vendor_dialect_beside_ossie_sql_2026_is_still_resolved():
    """OSSIE_SQL_2026 does not vote, so a model's one vendor dialect still wins."""
    ossie = _one_field_model([
        {"dialect": "OSSIE_SQL_2026", "expression": "amt"},
        {"dialect": "SNOWFLAKE", "expression": "amt"},
    ])
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert warnings_ == []
    # The Decimal column is typed in Snowflake's vocabulary, not ANSI's `DECIMAL`, and
    # with a scale: a bare NUMBER is NUMBER(38,0), an integer.
    assert solid_model_of(solid)["tables"][0]["facts"][0]["type"] == "NUMBER(38,9)"


# --- non-SQL dialects --------------------------------------------------------------


def test_a_non_sql_dialect_never_becomes_the_resolved_dialect():
    """MDX/TABLEAU/MAQL are in Apache Ossie's enum but are not SQL.

    Resolving to one would hand a non-SQL formula to the expression rewriter, and to
    Solid, as though it were SQL. The ANSI_SQL form is read instead.
    """
    ossie = _one_field_model([
        {"dialect": "TABLEAU", "expression": "[Amount]"},
        {"dialect": "ANSI_SQL", "expression": "amt"},
    ])
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("not SQL this converter can read" in w for w in warnings_)
    column = solid_model_of(solid)["tables"][0]["facts"][0]
    # The ANSI_SQL form was read, so the column is a plain reference to itself and no
    # Tableau formula leaked into the Solid model.
    assert "expression" not in column
    assert column["name"] == "amt"


def test_a_non_sql_dialect_with_no_ansi_form_is_an_error():
    """Better a clear failure than a Tableau formula in a SQL field."""
    ossie = _one_field_model([
        {"dialect": "MDX", "expression": "[Measures].[Amt]"},
    ])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(
            ConversionError, match="no ANSI_SQL or OSSIE_SQL_2026 expression"
        ):
            convert_ossie_to_solid(yaml.safe_dump(ossie))


def test_a_hand_edited_stash_dialect_is_rejected():
    ossie = yaml.safe_load(fixture("tpcds_ossie.yaml"))
    for ext in ossie["custom_extensions"]:
        if ext["vendor_name"] == "SOLID":
            ext["data"] = ext["data"].replace("SNOWFLAKE", "MAQL")
    with pytest.raises(ConversionError, match="Unsupported dialect"):
        convert_ossie_to_solid(yaml.safe_dump(ossie))


# --- spec versions -----------------------------------------------------------------


def test_the_released_0_1_1_spec_is_read_with_a_warning():
    """0.1.1 is the only released spec version, so models in the wild declare it.

    A 0.1.1 document wraps its model in a `semantic_model` list and otherwise differs
    from 0.2.0.dev0 only by additive fields, so it is read rather than rejected -- but
    the version gap is reported, since the missing `datatype` is what leaves a
    column's Solid `type` empty.
    """
    ossie = _wrapped(yaml.safe_load(fixture("tpcds_ossie.yaml")))
    ossie["version"] = "0.1.1"
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(ossie))
    assert any("declares Apache Ossie v0.1.1" in w for w in warnings_)
    assert any("predates the `datatype` field" in w for w in warnings_)
    # The model still converts in full: the stashed raw types carry the column types.
    assert solid_model_of(solid)["tables"][0]["facts"][0]["type"]


def test_reading_an_older_spec_still_writes_the_current_one():
    solid = yaml.safe_load(fixture("tpcds_solid.yaml"))
    ossie, _ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))
    aged = yaml.safe_load(ossie)
    aged["version"] = "0.1.1"
    back, _ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(aged))
    again, _ = convert_quietly(convert_solid_to_ossie, back)
    assert yaml.safe_load(again)["version"] == "0.2.0.dev0"
