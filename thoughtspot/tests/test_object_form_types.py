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

"""YAML types arriving in an object-form `custom_extensions` payload.

A JSON string is parsed by a JSON parser, so every scalar in it is typed by
what was written. The object form apache/ossie#522 proposes has been through a
YAML loader instead, which types unquoted scalars by guessing. `Yaml12Loader`
narrows the bool resolver and nothing else, so SafeLoader's YAML 1.1 rules
still apply to the rest:

    2024          -> int 2024
    2024-01-01    -> datetime.date
    12:30         -> int 750        (sexagesimal)
    0755          -> int 493        (octal)

Every test here starts from a document loaded through this converter's own
loader, not from a hand-built dict, because the premise is what the loader
does and a hand-built dict would assume it.
"""
import json

import pytest

from ossie_thoughtspot import _yaml, stash
from ossie_thoughtspot.constants import (
    DATASET_STASH_CONNECTION_NAME,
    DATASET_STASH_SOURCE_PARTS,
    FIELD_STASH_DB_COLUMN_NAME,
    FIELD_STASH_DB_COLUMN_NAME_WITNESS,
    MODEL_STASH_UNATTRIBUTED_FORMULAS,
    VENDOR_KEY,
)
from ossie_thoughtspot.errors import ConversionError
from ossie_thoughtspot.issues import IssueLog
from ossie_thoughtspot.ossie_to_thoughtspot import build_model, build_table


def _object_stash(**payload):
    """A THOUGHTSPOT entry in the object form, as a producer would write it."""
    return [{"vendor_name": VENDOR_KEY, "data": {"_v": 1, **payload}}]


def _codes(log):
    return [issue.code for issue in log.issues]


def _bracket_field(display, table, *, field_stash=None):
    """A field carrying the THOUGHTSPOT verbatim bracket reference a prior
    TML -> Ossie trip produces. That is the only shape for which
    `_physical_identity` consults the stashed warehouse column name."""
    field = {"name": display,
             "expression": {"dialects": [
                 {"dialect": "THOUGHTSPOT", "expression": f"[{table}::{display}]"}]}}
    if field_stash is not None:
        field["custom_extensions"] = _object_stash(**field_stash)
    return field


class TestTheLoaderReallyDoesThis:
    """The premise, asserted rather than assumed."""

    @pytest.mark.parametrize(
        ("written", "becomes"),
        [("2024", int), ("2024-01-01", __import__("datetime").date),
         ("12:30", int), ("0755", int)],
    )
    def test_an_unquoted_scalar_is_retyped(self, written, becomes):
        loaded = _yaml.load(f"k: {written}")
        assert isinstance(loaded["k"], becomes), loaded["k"]

    def test_12_30_is_seven_hundred_and_fifty(self):
        # The least guessable of them, and the one no pitfall list names.
        assert _yaml.load("k: 12:30")["k"] == 750

    def test_quoting_is_what_makes_it_a_string(self):
        assert _yaml.load('k: "2024"')["k"] == "2024"


class TestReadStashNormalisesTheObjectForm:
    def test_a_value_json_cannot_hold_is_refused_by_name(self):
        # Before: this reached write_stash's json.dumps and raised a bare
        # TypeError the CLI does not catch.
        obj = _yaml.load(
            "name: orders\n"
            "custom_extensions:\n"
            "  - vendor_name: THOUGHTSPOT\n"
            "    data:\n"
            "      _v: 1\n"
            "      alias: 2024-01-01\n"
        )
        with pytest.raises(ConversionError, match="orders") as excinfo:
            stash.read_stash(obj)
        assert "not JSON-representable" in str(excinfo.value)

    def test_the_payload_returned_is_not_a_reference_into_the_document(self):
        # Before: `parsed = raw` handed back the caller's own dict, and what
        # read_stash returns is put straight into TML bodies.
        obj = {"name": "orders", "custom_extensions": _object_stash(alias="O")}
        inner = obj["custom_extensions"][0]["data"]
        returned = stash.read_stash(obj)
        assert returned == inner
        returned["injected"] = "MUTATED"
        assert "injected" not in inner, "read_stash aliases the input document"

    def test_an_ordinary_object_payload_still_reads(self):
        obj = {"name": "orders", "custom_extensions": _object_stash(alias="O")}
        assert stash.read_stash(obj)["alias"] == "O"

    def test_write_over_an_unrepresentable_payload_names_the_object(self):
        # The same guard, reached through the writer rather than the reader.
        obj = _yaml.load(
            "name: orders\n"
            "custom_extensions:\n"
            "  - vendor_name: THOUGHTSPOT\n"
            "    data:\n"
            "      _v: 1\n"
            "      alias: 2024-01-01\n"
        )
        with pytest.raises(ConversionError, match="orders"):
            stash.write_stash(obj, {"x": "y"})


class TestARetypedStringIsReportedNotCrashedOn:
    """The three readers that require a string. Each says what is wrong."""

    def test_source_parts_with_a_non_string_part(self):
        # Before: `".".join(...)` raised TypeError, uncaught by the CLI.
        dataset = {
            "name": "orders",
            "source": "SALES.PUBLIC.ORDERS",
            "fields": [{"name": "amount",
                        "expression": {"dialects": [
                            {"dialect": "ANSI_SQL", "expression": "amount"}]}}],
            "custom_extensions": _object_stash(**{
                DATASET_STASH_CONNECTION_NAME: "My Snowflake",
                DATASET_STASH_SOURCE_PARTS: {
                    "db": "SALES", "schema": "PUBLIC", "db_table": 2024},
            }),
        }
        log = IssueLog()
        table = build_table(dataset, log)
        assert "TS-DATASET-SOURCE-PARTS-MALFORMED" in _codes(log)
        # Not the stale code: nothing was edited.
        assert "TS-DATASET-SOURCE-PARTS-STALE" not in _codes(log)
        # And it recovered by re-splitting the live source.
        assert table.body["db"] == "SALES"
        assert table.body["schema"] == "PUBLIC"
        assert table.body["db_table"] == "ORDERS"

    def test_a_db_column_name_that_is_not_a_string(self):
        # Before: reported TS-FIELD-DB-COLUMN-NAME-STALE, whose message says
        # the field was retargeted. It was not. A wrong diagnosis of a real
        # problem is worse than none.
        dataset = {
            "name": "orders",
            "source": "SALES.PUBLIC.ORDERS",
            "fields": [_bracket_field("Amount", "ORDERS", field_stash={
                FIELD_STASH_DB_COLUMN_NAME: 2024,
                FIELD_STASH_DB_COLUMN_NAME_WITNESS: "Amount",
            })],
            "custom_extensions": _object_stash(**{
                DATASET_STASH_CONNECTION_NAME: "My Snowflake"}),
        }
        log = IssueLog()
        table = build_table(dataset, log)
        assert "TS-FIELD-DB-COLUMN-NAME-MALFORMED" in _codes(log)
        assert "TS-FIELD-DB-COLUMN-NAME-STALE" not in _codes(log)
        assert table.body["columns"][0]["db_column_name"] == "Amount"

    def test_a_genuinely_stale_db_column_name_still_says_stale(self):
        # The guard must not swallow the case the stale code is FOR.
        dataset = {
            "name": "orders",
            "source": "SALES.PUBLIC.ORDERS",
            "fields": [_bracket_field("Amount", "ORDERS", field_stash={
                FIELD_STASH_DB_COLUMN_NAME: "o_amount",
                FIELD_STASH_DB_COLUMN_NAME_WITNESS: "a_different_column",
            })],
            "custom_extensions": _object_stash(**{
                DATASET_STASH_CONNECTION_NAME: "My Snowflake"}),
        }
        log = IssueLog()
        build_table(dataset, log)
        assert "TS-FIELD-DB-COLUMN-NAME-STALE" in _codes(log)
        assert "TS-FIELD-DB-COLUMN-NAME-MALFORMED" not in _codes(log)

    def test_an_unattributed_formula_whose_name_is_not_a_string(self):
        # Before: _formula_id_from raised TypeError from normalize().
        model = {
            "name": "sales",
            "datasets": [{
                "name": "orders",
                "source": "SALES.PUBLIC.ORDERS",
                "fields": [{"name": "amount",
                            "expression": {"dialects": [
                                {"dialect": "ANSI_SQL", "expression": "amount"}]}}],
                "custom_extensions": _object_stash(**{
                    DATASET_STASH_CONNECTION_NAME: "My Snowflake"}),
            }],
            "custom_extensions": _object_stash(**{
                MODEL_STASH_UNATTRIBUTED_FORMULAS: [
                    {"name": 2024, "expr": "1 + 1"}]}),
        }
        log = IssueLog()
        tables = [build_table(model["datasets"][0], IssueLog())]
        build_model(model, tables, log)
        assert "TS-MODEL-UNATTRIBUTED-FORMULA-MALFORMED" in _codes(log)


def test_the_string_form_is_unaffected_by_any_of_this():
    """The regression guard: nothing above may change the shipped path.

    Every one of these values is a plain string in the string form, because
    JSON typed it. If a guard above started firing here, it would be
    reporting documents this converter itself writes.
    """
    dataset = {
        "name": "orders",
        "source": "SALES.PUBLIC.ORDERS",
        "fields": [{
            "name": "Amount",
            "expression": {"dialects": [
                {"dialect": "THOUGHTSPOT", "expression": "[ORDERS::Amount]"}]},
            "custom_extensions": [{"vendor_name": VENDOR_KEY, "data": json.dumps({
                "_v": 1,
                FIELD_STASH_DB_COLUMN_NAME: "2024",
                FIELD_STASH_DB_COLUMN_NAME_WITNESS: "Amount",
            })}],
        }],
        "custom_extensions": [{"vendor_name": VENDOR_KEY, "data": json.dumps({
            "_v": 1,
            DATASET_STASH_CONNECTION_NAME: "My Snowflake",
            DATASET_STASH_SOURCE_PARTS: {
                "db": "SALES", "schema": "PUBLIC", "db_table": "ORDERS"},
        })}],
    }
    log = IssueLog()
    table = build_table(dataset, log)
    assert _codes(log) == []
    assert table.body["schema"] == "PUBLIC"
    assert table.body["columns"][0]["db_column_name"] == "2024"
