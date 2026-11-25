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

"""`write_stash` must not write a payload that is not JSON.

`json.dumps` defaults to `allow_nan=True`, under which a float `nan` or `inf`
is emitted as the bare token `NaN` or `Infinity`. Neither is JSON, so a strict
parser on the far side refuses the document this converter just produced.

It reaches both `data` shapes. The object form is `json.loads` of the very
string the string form writes, and `json.loads` accepts those tokens by
default, so the value survives the round trip and lands in the document either
way.

`read_stash` has refused an unrepresentable payload on the way in since it
started round-tripping through JSON. This is the same refusal on the way out.

The refusal is not fatal. `_write_stash_safely` drops the offending key and
logs it, exactly as it already does for an identity key, because a value like
this is content read out of a source file: expected input, not a programming
error. Aborting the conversion would trade a silently malformed document for a
dead one.
"""
import datetime
import json

import pytest

from ossie_thoughtspot.errors import ConversionError
from ossie_thoughtspot.stash import find_unserialisable_key, read_stash, write_stash
from ossie_thoughtspot.tml import DocumentSet, TmlDocument
from ossie_thoughtspot.tml_to_ossie import convert

NAN = float("nan")
INF = float("inf")


class TestWriteStashRefusesAPayloadWithNoJsonForm:

    @pytest.mark.parametrize("value", [NAN, INF, -INF])
    def test_a_non_finite_float_is_refused(self, value):
        with pytest.raises(ConversionError):
            write_stash({"name": "orders"}, {"alias": value})

    def test_a_value_yaml_produces_that_json_cannot_hold_is_refused(self):
        # An unquoted 2026-01-01 in TML is a `datetime.date` after the YAML
        # loader, and has no JSON form at all.
        with pytest.raises(ConversionError):
            write_stash({"name": "orders"}, {"as_of": datetime.date(2026, 1, 1)})

    def test_the_refusal_names_the_object_and_the_reason(self):
        with pytest.raises(ConversionError) as exc:
            write_stash({"name": "orders"}, {"alias": NAN})
        assert "orders" in str(exc.value)
        assert "not JSON-representable" in str(exc.value)

    def test_a_non_finite_float_nested_inside_the_payload_is_refused_too(self):
        with pytest.raises(ConversionError):
            write_stash({"name": "orders"}, {"geo": {"bounds": [1.0, NAN]}})

    def test_the_string_data_form_is_refused_on_the_same_terms(self):
        # A document still on the pre-#522 name takes the JSON-string branch.
        obj = {"name": "orders", "custom_extensions": []}
        with pytest.raises(ConversionError):
            write_stash(obj, {"alias": NAN})

    def test_a_clean_payload_is_written_unchanged(self):
        out = write_stash({"name": "orders"}, {"alias": "ok", "n": 1.5})
        assert read_stash(out)["alias"] == "ok"
        assert read_stash(out)["n"] == 1.5


class TestFindUnserialisableKey:

    def test_it_names_the_offending_key(self):
        assert find_unserialisable_key({"good": 1, "bad": NAN}) == "bad"

    def test_it_returns_none_for_a_clean_payload(self):
        assert find_unserialisable_key({"good": 1, "also": "fine"}) is None

    def test_it_agrees_with_what_the_writer_refuses(self):
        # The two must not disagree: the caller that turns a refusal into a
        # drop can only drop a key this names, and re-raises otherwise.
        payload = {"ok": 1, "bad": INF}
        with pytest.raises(ConversionError):
            write_stash({"name": "orders"}, payload)
        assert find_unserialisable_key(payload) == "bad"


def _model_carrying(value):
    table = TmlDocument(kind="table", body={
        "name": "ORDERS", "db": "SALES", "schema": "PUBLIC", "db_table": "ORDERS",
        "connection": {"name": "My Snowflake"},
        "columns": [{"name": "AMOUNT", "db_column_name": "AMOUNT",
                     "db_column_properties": {"data_type": "INT64"}}],
    }, guid=None)
    model = TmlDocument(kind="model", body={
        "name": "sales", "model_tables": [{"name": "ORDERS"}],
        "columns": [{"name": "Amount", "column_id": "ORDERS::AMOUNT",
                     "properties": {"column_type": "ATTRIBUTE",
                                    "index_priority": value}}],
    }, guid=None)
    return convert(DocumentSet(model=model, tables=(table,)))


class TestAnUnrepresentableValueIsDroppedRatherThanFatal:

    def test_the_conversion_completes_and_says_what_it_dropped(self):
        result = _model_carrying(NAN)
        codes = [i.get("code") for i in result.issues.as_dicts()]
        assert "TS-STASH-UNSERIALISABLE-DROPPED" in codes

    def test_the_emitted_document_carries_no_nan_token(self):
        assert "NaN" not in json.dumps(_model_carrying(NAN).model)

    def test_the_emitted_document_survives_a_strict_json_parser(self):
        def refuse(constant):
            raise AssertionError(f"a strict parser refuses {constant}")

        blob = json.dumps(_model_carrying(NAN).model)
        json.loads(blob, parse_constant=refuse)

    def test_a_clean_model_logs_no_such_issue(self):
        codes = [i.get("code") for i in _model_carrying(5).issues.as_dicts()]
        assert "TS-STASH-UNSERIALISABLE-DROPPED" not in codes
