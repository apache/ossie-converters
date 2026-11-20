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

"""The two names the vendor-extensions field goes by.

`custom_extensions` is what the spec says today. apache/ossie#522 renames it to
`nonportable_extensions` and does NOT accept the old name, so unlike the object
form of `data` -- where a reader could in principle wait, because the string
form stays valid -- there is no version of that change under which waiting
costs nothing. The reader takes both names from now; the writer keeps each
document on the name it already carries.

The name and the shape of `data` are independent axes. `test_object_form_types`
owns the shape axis; this file owns the name axis and crosses the two once.
"""
import json

import pytest

from ossie_thoughtspot import stash
from ossie_thoughtspot.constants import (
    DATASET_STASH_CONNECTION_NAME,
    DATASET_STASH_SOURCE_PARTS,
    EXTENSIONS_FIELD,
    EXTENSIONS_FIELD_RENAMED,
    STASH_VERSION,
    VENDOR_KEY,
)
from ossie_thoughtspot.errors import ConversionError
from ossie_thoughtspot.issues import IssueLog
from ossie_thoughtspot.ossie_to_thoughtspot import build_table

BOTH_NAMES = [EXTENSIONS_FIELD, EXTENSIONS_FIELD_RENAMED]


def _entry(data):
    return [{"vendor_name": VENDOR_KEY, "data": data}]


def _obj(field, data, name="orders"):
    return {"name": name, field: _entry(data)}


def _payload():
    return {"_v": STASH_VERSION, "alias": "X", "join_type": "LEFT_OUTER"}


class TestTheReaderTakesEitherName:
    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_the_string_form_reads_under_either_name(self, field):
        assert stash.read_stash(_obj(field, json.dumps(_payload()))) == _payload()

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_the_object_form_reads_under_either_name(self, field):
        # The crossing of the two axes: #522 renames the field AND reshapes
        # `data`, but a producer may land on either independently.
        assert stash.read_stash(_obj(field, _payload())) == _payload()

    def test_the_name_makes_no_difference_to_what_is_returned(self):
        old = stash.read_stash(_obj(EXTENSIONS_FIELD, json.dumps(_payload())))
        new = stash.read_stash(_obj(EXTENSIONS_FIELD_RENAMED, json.dumps(_payload())))
        assert old == new == _payload()

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_an_object_with_no_entry_of_ours_reads_empty_under_either_name(self, field):
        obj = {"name": "orders", field: [{"vendor_name": "SNOWFLAKE", "data": "{}"}]}
        assert stash.read_stash(obj) == {}

    def test_an_object_with_neither_name_reads_empty(self):
        assert stash.read_stash({"name": "orders"}) == {}

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_the_shape_version_gate_still_applies_under_either_name(self, field):
        # Taking a second name must not become a way around the version gate,
        # for the same reason taking a second `data` shape must not.
        with pytest.raises(ConversionError, match="orders") as excinfo:
            stash.read_stash(_obj(field, {"_v": 999, "alias": "X"}))
        assert "999" in str(excinfo.value)


def _other(field):
    return EXTENSIONS_FIELD_RENAMED if field == EXTENSIONS_FIELD else EXTENSIONS_FIELD


class TestAnErrorNamesTheFieldTheDocumentUsed:
    """A message that names the other name sends the reader to the wrong line."""

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_malformed_json_names_the_field_in_use(self, field):
        with pytest.raises(ConversionError) as excinfo:
            stash.read_stash(_obj(field, "{not json"))
        assert field in str(excinfo.value)
        assert _other(field) not in str(excinfo.value)

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_a_non_list_field_names_the_field_in_use(self, field):
        with pytest.raises(ConversionError) as excinfo:
            stash.read_stash({"name": "orders", field: "not a list"})
        assert field in str(excinfo.value)
        assert _other(field) not in str(excinfo.value)

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_a_non_mapping_entry_names_the_field_in_use(self, field):
        with pytest.raises(ConversionError) as excinfo:
            stash.read_stash({"name": "orders", field: ["not a mapping"]})
        assert field in str(excinfo.value)
        assert _other(field) not in str(excinfo.value)

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_data_of_the_wrong_type_names_the_field_in_use(self, field):
        with pytest.raises(ConversionError) as excinfo:
            stash.read_stash(_obj(field, 7))
        assert field in str(excinfo.value)
        assert _other(field) not in str(excinfo.value)


class TestBothNamesAtOnceIsRefused:
    """Not resolved by precedence. Reading one while writing the other would
    drop whatever was in the loser, silently."""

    def test_both_populated_raises_naming_the_object_and_both_names(self):
        obj = {
            "name": "orders",
            EXTENSIONS_FIELD: _entry(json.dumps(_payload())),
            EXTENSIONS_FIELD_RENAMED: _entry(json.dumps(_payload())),
        }
        with pytest.raises(ConversionError, match="orders") as excinfo:
            stash.read_stash(obj)
        assert EXTENSIONS_FIELD in str(excinfo.value)
        assert EXTENSIONS_FIELD_RENAMED in str(excinfo.value)

    @pytest.mark.parametrize("empty", [[], None])
    def test_the_test_is_the_key_not_a_truthy_value(self, empty):
        # An empty old field beside a real new one is still a document using a
        # name #522 does not accept. Keying off truthiness would quietly read
        # the new one and then, on write, resurrect the old.
        obj = {
            "name": "orders",
            EXTENSIONS_FIELD: empty,
            EXTENSIONS_FIELD_RENAMED: _entry(json.dumps(_payload())),
        }
        with pytest.raises(ConversionError, match="orders"):
            stash.read_stash(obj)

    def test_the_writer_refuses_it_too(self):
        obj = {
            "name": "orders",
            EXTENSIONS_FIELD: _entry(json.dumps(_payload())),
            EXTENSIONS_FIELD_RENAMED: _entry(json.dumps(_payload())),
        }
        with pytest.raises(ConversionError, match="orders"):
            stash.write_stash(obj, {"alias": "Y"})


class TestTheWriterHasNotMoved:
    """Readers first. The writer emits today's name, and keeps a document that
    already uses the new one on the new one -- it never introduces it."""

    def test_an_object_with_no_extensions_gets_todays_name(self):
        out = stash.write_stash({"name": "orders"}, {"alias": "X"})
        assert EXTENSIONS_FIELD in out
        assert EXTENSIONS_FIELD_RENAMED not in out

    def test_a_document_on_todays_name_stays_on_it(self):
        obj = _obj(EXTENSIONS_FIELD, json.dumps({"_v": STASH_VERSION, "alias": "X"}))
        out = stash.write_stash(obj, {"join_type": "LEFT_OUTER"})
        assert EXTENSIONS_FIELD_RENAMED not in out
        written = json.loads(out[EXTENSIONS_FIELD][0]["data"])
        assert written["alias"] == "X"
        assert written["join_type"] == "LEFT_OUTER"

    def test_a_document_on_the_new_name_stays_on_the_new_name(self):
        # The case that makes this more than a rename: writing
        # EXTENSIONS_FIELD here would hand back a document carrying both, which
        # the reader above refuses -- we would have broken our own round trip.
        obj = _obj(EXTENSIONS_FIELD_RENAMED, json.dumps({"_v": STASH_VERSION, "alias": "X"}))
        out = stash.write_stash(obj, {"join_type": "LEFT_OUTER"})
        assert EXTENSIONS_FIELD not in out
        written = json.loads(out[EXTENSIONS_FIELD_RENAMED][0]["data"])
        assert written["alias"] == "X"
        assert written["join_type"] == "LEFT_OUTER"

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_data_is_still_a_json_string_under_either_name(self, field):
        # The shape axis does not move either, whichever name carries it.
        out = stash.write_stash(_obj(field, {"_v": STASH_VERSION, "alias": "X"}),
                                {"join_type": "LEFT_OUTER"})
        assert isinstance(out[field][0]["data"], str)

    @pytest.mark.parametrize("field", BOTH_NAMES)
    def test_foreign_vendor_entries_survive_under_either_name(self, field):
        foreign = {"vendor_name": "SNOWFLAKE", "data": "{}"}
        obj = {"name": "orders", field: [foreign, {"vendor_name": VENDOR_KEY,
                                                   "data": json.dumps({"_v": STASH_VERSION})}]}
        out = stash.write_stash(obj, {"alias": "X"})
        assert foreign in out[field]
        assert len([e for e in out[field] if e["vendor_name"] == VENDOR_KEY]) == 1

    def test_an_empty_payload_writes_no_field_at_all(self):
        assert stash.write_stash({"name": "orders"}, {}) == {"name": "orders"}


def test_a_dataset_carrying_the_new_name_converts_the_same_as_the_old():
    """End to end, not just at the stash boundary: every reader on the
    `Ossie -> TML` leg goes through `read_stash`, so the name is invisible
    above it. Asserted rather than assumed."""
    def dataset(field):
        return {
            "name": "orders",
            "source": "SALES.PUBLIC.ORDERS",
            "fields": [{"name": "amount",
                        "expression": {"dialects": [
                            {"dialect": "ANSI_SQL", "expression": "amount"}]}}],
            field: _entry(json.dumps({
                "_v": STASH_VERSION,
                DATASET_STASH_CONNECTION_NAME: "My Snowflake",
                DATASET_STASH_SOURCE_PARTS: {
                    "db": "SALES", "schema": "PUBLIC", "db_table": "ORDERS"},
            })),
        }

    old_log, new_log = IssueLog(), IssueLog()
    old = build_table(dataset(EXTENSIONS_FIELD), old_log)
    new = build_table(dataset(EXTENSIONS_FIELD_RENAMED), new_log)

    assert [i.code for i in old_log.issues] == [] == [i.code for i in new_log.issues]
    assert new.body == old.body
    assert new.body["db"] == "SALES"
    assert new.body["schema"] == "PUBLIC"
    assert new.body["db_table"] == "ORDERS"
