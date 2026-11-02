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

"""Metric expression qualification.

The property that matters most here is that the converter never rewrites SQL it was only
asked to qualify: the splice must leave every byte outside the inserted qualifier alone.
"""

import pytest

from ossie_solid.expressions import (
    AMBIGUOUS,
    QUALIFIED,
    UNCHANGED,
    UNPARSED,
    UNQUALIFIED,
    column_reference,
    qualify_metric,
    referenced_datasets,
    unqualify_metric,
)

COLUMNS = ["amount", "status", "order_id", "region", "ss_sales_price", "ss_list_price"]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("SUM(amount)", "SUM(orders.amount)"),
        ("SUM(amount) / 12.0", "SUM(orders.amount) / 12.0"),
        ("COUNT(DISTINCT order_id)", "COUNT(DISTINCT orders.order_id)"),
        ("SUM(SUM(amount)) OVER ()", "SUM(SUM(orders.amount)) OVER ()"),
        (
            "SUM(CASE WHEN status = 'shipped' THEN amount ELSE 0 END)",
            "SUM(CASE WHEN orders.status = 'shipped' THEN orders.amount ELSE 0 END)",
        ),
        (
            "1 - (ss_sales_price / NULLIF(ss_list_price, 0))",
            "1 - (orders.ss_sales_price / NULLIF(orders.ss_list_price, 0))",
        ),
    ],
)
def test_bare_columns_are_prefixed_with_the_owning_dataset(expression, expected):
    assert qualify_metric(expression, "SNOWFLAKE", "orders", COLUMNS) == (
        expected,
        QUALIFIED,
    )


def test_a_column_name_inside_a_string_literal_is_not_touched():
    result, status = qualify_metric(
        "COUNT(CASE WHEN status = 'amount' THEN 1 END)", "SNOWFLAKE", "orders", COLUMNS
    )
    assert result == "COUNT(CASE WHEN orders.status = 'amount' THEN 1 END)"
    assert status == QUALIFIED


def test_an_already_qualified_column_is_left_alone():
    assert qualify_metric("SUM(o.amount)", "SNOWFLAKE", "orders", COLUMNS) == (
        "SUM(o.amount)",
        UNCHANGED,
    )


def test_a_function_whose_name_matches_a_column_is_not_qualified():
    assert qualify_metric("status(amount)", "SNOWFLAKE", "orders", COLUMNS)[0] == (
        "status(orders.amount)"
    )


def test_a_column_the_dataset_does_not_own_is_left_bare():
    assert qualify_metric("SUM(other_col)", "SNOWFLAKE", "orders", COLUMNS) == (
        "SUM(other_col)",
        UNCHANGED,
    )


def test_column_matching_is_case_insensitive():
    result, status = qualify_metric("SUM(AMOUNT)", "SNOWFLAKE", "orders", COLUMNS)
    assert result == "SUM(orders.AMOUNT)"
    assert status == QUALIFIED


def test_an_expression_with_no_columns_is_returned_verbatim():
    assert qualify_metric("COUNT(*)", "SNOWFLAKE", "orders", COLUMNS) == (
        "COUNT(*)",
        UNCHANGED,
    )


def test_a_name_used_in_a_non_column_position_blocks_the_edit():
    # `year` is a column here *and* the datepart keyword inside EXTRACT, so the token
    # scan and the parse disagree and the expression is left exactly as written.
    result, status = qualify_metric(
        "SUM(EXTRACT(year FROM closed_at))", "SNOWFLAKE", "orders", ["year", "closed_at"]
    )
    assert result == "SUM(EXTRACT(year FROM closed_at))"
    assert status == AMBIGUOUS


@pytest.mark.parametrize(
    ("expression", "dialect"),
    [
        # sqlglot canonicalizes both of these when it re-renders a parsed tree; a splice
        # must not.
        ("ROUND(CAST(amount AS FLOAT), 2)", "SNOWFLAKE"),
        ("SUM(amount)   +   1", "SNOWFLAKE"),
        ("COUNTIF(status = 'active')", "BIGQUERY"),
        ("SUM(amount) /* trailing comment */", "SNOWFLAKE"),
    ],
)
def test_everything_outside_the_inserted_qualifier_is_preserved_byte_for_byte(
    expression, dialect
):
    result, status = qualify_metric(expression, dialect, "orders", COLUMNS)
    assert status == QUALIFIED, "otherwise this asserts nothing"
    assert result.replace("orders.", "") == expression


def test_an_unparseable_expression_is_returned_verbatim():
    assert qualify_metric("SUM(amount", "SNOWFLAKE", "orders", COLUMNS) == (
        "SUM(amount",
        UNPARSED,
    )


def test_a_quoted_identifier_is_qualified_with_its_quotes_intact():
    result, status = qualify_metric(
        'SUM("odd name")', "SNOWFLAKE", "orders", ["odd name"]
    )
    assert result == 'SUM(orders."odd name")'
    assert status == QUALIFIED


# --- the inverse -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("SUM(orders.amount)", "SUM(amount)"),
        (
            "SUM(CASE WHEN orders.status = 'x' THEN orders.amount END)",
            "SUM(CASE WHEN status = 'x' THEN amount END)",
        ),
    ],
)
def test_a_dataset_qualifier_is_stripped_back_out(expression, expected):
    assert unqualify_metric(expression, "SNOWFLAKE", ["orders"]) == (
        expected,
        UNQUALIFIED,
    )


def test_a_qualifier_that_is_not_a_dataset_is_preserved():
    assert unqualify_metric("SUM(p.amount)", "SNOWFLAKE", ["orders"]) == (
        "SUM(p.amount)",
        UNCHANGED,
    )


def test_a_schema_qualified_reference_is_left_alone():
    assert unqualify_metric("SUM(db.orders.amount)", "SNOWFLAKE", ["orders"]) == (
        "SUM(db.orders.amount)",
        UNCHANGED,
    )


@pytest.mark.parametrize(
    ("expression", "dialect"),
    [
        ("SUM(orders.amount) / NULLIF(COUNT(orders.order_id), 0)", "SNOWFLAKE"),
        ("ROUND(CAST(orders.amount AS FLOAT), 2)", "SNOWFLAKE"),
        ("COUNTIF(orders.status = 'active')", "BIGQUERY"),
    ],
)
def test_qualifying_and_unqualifying_returns_the_original_text(expression, dialect):
    bare, _ = unqualify_metric(expression, dialect, ["orders"])
    again, _ = qualify_metric(bare, dialect, "orders", COLUMNS)
    assert again == expression


# --- helpers -----------------------------------------------------------------------


def test_the_datasets_a_metric_references_are_reported_in_declaration_order():
    assert referenced_datasets(
        "SUM(orders.amount) / COUNT(customers.id)",
        "SNOWFLAKE",
        ["customers", "orders", "items"],
    ) == ["customers", "orders"]


def test_no_datasets_are_reported_for_a_bare_expression():
    assert referenced_datasets("SUM(amount)", "SNOWFLAKE", ["orders"]) == []


@pytest.mark.parametrize(
    ("name", "dialect", "expected"),
    [
        ("amount", "SNOWFLAKE", "amount"),
        ("odd name", "SNOWFLAKE", '"odd name"'),
        ("odd name", "DATABRICKS", "`odd name`"),
        ("select", "SNOWFLAKE", '"SELECT"'),
        # Reserved words sqlglot reads back as ordinary columns, so a parse round trip
        # never caught them.
        ("order", "SNOWFLAKE", '"ORDER"'),
        ("group", "DATABRICKS", "`group`"),
        ("limit", "ANSI_SQL", '"limit"'),
        ("table", "BIGQUERY", "`table`"),
        # Snowflake reserves fewer words than the standard; LIMIT is not one of them.
        ("limit", "SNOWFLAKE", "limit"),
        ("a$b", "SNOWFLAKE", "a$b"),
        ("a$b", "ANSI_SQL", '"a$b"'),
    ],
)
def test_a_column_name_renders_as_a_valid_reference(name, dialect, expected):
    assert column_reference(name, dialect) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # A quoted name is case-sensitive on Snowflake and a bare one folds to upper
        # case, so a reserved name is quoted in upper case: it names the column the
        # bare name would have, as every other name does. Solid reports Snowflake names
        # as INFORMATION_SCHEMA stores them, upper case for a column created unquoted,
        # so for its own exports this is the name as reported.
        ("ORDER", '"ORDER"'),
        ("order", '"ORDER"'),
        ("Order", '"ORDER"'),
        # A name that is not reserved there is never quoted: `"comment"` would not
        # match a column named COMMENT.
        ("COMMENT", "COMMENT"),
        ("comment", "comment"),
        # Restricted only in SHOW commands, so an ordinary column name.
        ("account", "account"),
        ("schema", "schema"),
        # Not a regular identifier, so there is no bare form to follow.
        ("my col", '"my col"'),
    ],
)
def test_snowflake_quotes_only_reserved_names_in_the_case_they_fold_to(name, expected):
    assert column_reference(name, "SNOWFLAKE") == expected


@pytest.mark.parametrize("dialect", ["SNOWFLAKE", "DATABRICKS", "BIGQUERY", "ANSI_SQL"])
def test_every_reserved_word_is_quoted_and_reads_back_as_its_column(dialect):
    """Each listed word is quoted, and the quoted form is a valid column reference to
    exactly that name in the dialect."""
    import sqlglot
    from sqlglot import exp

    from ossie_solid.expressions import _RESERVED, _read

    # Snowflake quotes in the case a bare name folds to (see column_reference).
    folded = str.upper if dialect == "SNOWFLAKE" else str.lower
    for word in sorted(_RESERVED[dialect]):
        rendered = column_reference(word.lower(), dialect)
        assert rendered != word.lower(), word
        parsed = sqlglot.parse_one(f"SELECT {rendered} FROM t", read=_read(dialect))
        (column,) = parsed.expressions
        assert isinstance(column, exp.Column) and column.name == folded(word), word


def test_bigquerys_list_covers_the_one_sqlglot_ships():
    """sqlglot keeps BigQuery's documented reserved words for its own generator; a word
    added there and missing here means this list went stale."""
    from sqlglot.dialects.bigquery import BigQuery

    from ossie_solid._reserved_words import BIGQUERY

    missing = {w.upper() for w in BigQuery.Generator.RESERVED_KEYWORDS} - BIGQUERY
    assert not missing, sorted(missing)


def test_the_snowflake_list_is_its_documented_column_and_alias_restrictions():
    """Snowflake's documented 92 reserved and limited keywords, less the eight it
    restricts only in SHOW commands. It must not grow past that, since a quoted name is
    case-sensitive there."""
    from ossie_solid._reserved_words import SNOWFLAKE

    show_only = {"ACCOUNT", "CONNECTION", "DATABASE", "GSCLUSTER", "ISSUE",
                 "ORGANIZATION", "SCHEMA", "VIEW"}
    assert len(SNOWFLAKE) == 92 - len(show_only)
    assert not show_only & SNOWFLAKE
    assert {"ORDER", "GROUP", "TABLE", "QUALIFY", "MINUS", "WINDOW"} <= SNOWFLAKE
    assert not {"COMMENT", "LIMIT", "DATE", "NAME", "VALUE"} & SNOWFLAKE


def test_unqualifying_two_datasets_columns_of_one_name_is_ambiguous():
    # Stripped, both would read `amount`: the formula would be ambiguous in Solid, or
    # always zero.
    expression = "SUM(orders.amount) - SUM(returns.amount)"
    assert unqualify_metric(expression, "SNOWFLAKE", ["orders", "returns"]) == (
        expression, AMBIGUOUS)


def test_unqualifying_a_name_another_read_dataset_also_has_is_ambiguous():
    # No two references collide in the text, but `id` is a column of both datasets the
    # metric reads, so a bare `id` would not say which.
    expression = "SUM(orders.amount) / COUNT(customers.id)"
    fields = {"orders": ["amount", "id"], "customers": ["id", "name"]}
    assert unqualify_metric(
        expression, "SNOWFLAKE", ["orders", "customers"], fields) == (
        expression, AMBIGUOUS)


def test_a_name_only_a_dataset_the_metric_does_not_read_has_is_no_collision():
    fields = {"orders": ["amount"], "customers": ["cid"], "returns": ["amount"]}
    assert unqualify_metric(
        "SUM(orders.amount) / COUNT(customers.cid)", "SNOWFLAKE",
        ["orders", "customers", "returns"], fields,
    ) == ("SUM(amount) / COUNT(cid)", UNQUALIFIED)


# Names sqlglot tokenizes as keywords, though they are ordinary table and column names.
KEYWORD_NAMES = ["date", "time", "comment", "index", "first", "range", "timestamp",
                 "filter", "format", "partition", "session", "view", "schema", "rows",
                 "year", "value", "name", "level", "role", "position", "order", "limit"]
DIALECTS = ["SNOWFLAKE", "DATABRICKS", "BIGQUERY", "ANSI_SQL"]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_a_dataset_named_like_a_keyword_qualifies_and_unqualifies(name, dialect):
    qualified, status = qualify_metric("SUM(amount)", dialect, name, ["amount"])
    assert status == QUALIFIED
    assert unqualify_metric(qualified, dialect, [name]) == ("SUM(amount)", UNQUALIFIED)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("name", KEYWORD_NAMES)
def test_a_column_named_like_a_keyword_is_qualified_or_left_exactly_as_written(
        name, dialect):
    """Qualified when the formula parses; a formula that uses a reserved word bare is
    not valid SQL in the first place, and is left alone."""
    expression = f"COUNT(DISTINCT {name})"
    qualified, status = qualify_metric(expression, dialect, "orders", [name])
    if status == QUALIFIED:
        assert qualified == f"COUNT(DISTINCT orders.{name})"
    else:
        assert (qualified, status) == (expression, UNPARSED)
    assert unqualify_metric(qualified, dialect, ["orders"])[0] == expression


def test_the_head_of_a_struct_path_is_qualified():
    assert qualify_metric("SUM(customer.lifetime_value)", "BIGQUERY", "orders",
                          ["customer", "amount"]) == (
        "SUM(orders.customer.lifetime_value)", QUALIFIED)


def test_a_dataset_qualified_struct_path_unqualifies_to_the_bare_path():
    fields = {"orders": ["customer", "amount"]}
    assert unqualify_metric("SUM(orders.customer.lifetime_value)", "BIGQUERY",
                            ["orders"], fields) == (
        "SUM(customer.lifetime_value)", UNQUALIFIED)


def test_a_schema_qualified_path_whose_middle_is_not_a_field_is_left_alone():
    # `orders.public.amount`: `public` is not a field of `orders`, so this is not a
    # dataset qualifier this converter added.
    fields = {"orders": ["customer", "amount"]}
    assert unqualify_metric("SUM(orders.public.amount)", "BIGQUERY", ["orders"],
                            fields) == ("SUM(orders.public.amount)", UNCHANGED)
