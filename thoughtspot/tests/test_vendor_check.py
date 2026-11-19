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

"""The THOUGHTSPOT vendor validator (apache/ossie#697's `check`)."""
import json

import pytest

from ossie_thoughtspot import stash, vendor_check
from ossie_thoughtspot.constants import STASH_VERSION, VENDOR_KEY


class _Site:
    """Stand-in for the hook's site object.

    The real one carries `kind`, `name` and the owning `dataset`. `check`
    accepts it and does not read it, so this exists to prove the signature the
    entry point promises, not to exercise a path.
    """

    kind = "field"
    name = "amount"
    dataset = "orders"


def test_a_payload_this_converter_wrote_has_nothing_to_report():
    # The whole-pipeline version of this is the fixture test below; this is
    # the unit form, so a failure says which half is wrong.
    obj = stash.write_stash({"name": "orders"}, {"alias": "O", "db_column_name": "amt"})
    payload = json.loads(obj["custom_extensions"][0]["data"])
    assert vendor_check.check(payload, _Site()) == []


class TestTheThreeThingsItReports:
    def test_a_payload_that_is_not_an_object(self):
        found = vendor_check.check(["_v", 1], _Site())
        assert len(found) == 1
        assert "not an object" in found[0]
        assert "list" in found[0]

    @pytest.mark.parametrize("version", [999, None, "1", 0, [1], {"v": 1}])
    def test_a_shape_version_this_converter_does_not_know(self, version):
        payload = {"alias": "O"}
        if version is not None:
            payload["_v"] = version
        found = vendor_check.check(payload, _Site())
        assert len(found) == 1
        assert "shape version" in found[0]

    @pytest.mark.parametrize("spelling", [1.0, True])
    def test_a_value_python_equates_to_the_current_version_is_accepted(self, spelling):
        """`_v: 1.0` and `_v: true` pass, and the reason is the invariant.

        Python reads both `1.0 == 1` and `True == 1` as true, so neither this
        checker nor `read_stash` distinguishes them from `1`. That agreement
        is the point: a checker that rejected a payload its own converter
        reads happily would report documents we can ourselves process, which
        is a worse failure than accepting an odd spelling of a version we do
        recognise. Tightening `_v` to reject a bool is a change to the reader
        first and the checker second, never the other way round.
        """
        assert vendor_check.check({"_v": spelling, "alias": "O"}, _Site()) == []
        obj = {"name": "orders", "custom_extensions": [
            {"vendor_name": VENDOR_KEY, "data": json.dumps({"_v": spelling, "alias": "O"})}
        ]}
        assert stash.read_stash(obj)["alias"] == "O"   # the reader agrees

    @pytest.mark.parametrize(
        "payload",
        [
            {"_v": STASH_VERSION, "guid": "abc"},
            {"_v": STASH_VERSION, "tml_object": {"guid": "abc"}},
            {"_v": STASH_VERSION, "cols": [{"properties": {"fqn": "x"}}]},
        ],
    )
    def test_instance_local_identity_at_any_depth(self, payload):
        found = vendor_check.check(payload, _Site())
        assert len(found) == 1
        assert "instance-local identity key" in found[0]

    def test_a_version_problem_and_an_identity_problem_are_both_reported(self):
        # A validator names everything wrong in one pass; it does not stop at
        # the first. This is the behaviour that separates it from read_stash.
        found = vendor_check.check({"_v": 999, "guid": "abc"}, _Site())
        assert len(found) == 2


class TestItReportsRatherThanRaises:
    """The distinction that made this a new module instead of a reuse."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"_v": 999},
            {"_v": STASH_VERSION, "guid": "abc"},
            ["not", "an", "object"],
            "a bare string",
            7,
        ],
    )
    def test_no_input_makes_the_checker_raise(self, payload):
        # The same inputs put through read_stash raise ConversionError, which
        # in a validator would end the file and hide every later object.
        assert isinstance(vendor_check.check(payload, _Site()), list)

    def test_read_stash_still_raises_on_the_same_payload(self):
        # Pins the asymmetry as deliberate: if someone later makes read_stash
        # collect instead, this fails and they have to come here too.
        obj = {"name": "orders", "custom_extensions": [
            {"vendor_name": VENDOR_KEY, "data": json.dumps({"_v": 999})}
        ]}
        from ossie_thoughtspot.errors import ConversionError

        with pytest.raises(ConversionError):
            stash.read_stash(obj)
        assert vendor_check.check({"_v": 999}, _Site()) != []


class TestTheMessagesSuitTheHook:
    def test_a_message_does_not_repeat_the_object_name(self):
        # The hook prefixes "[Vendor THOUGHTSPOT] Field 'amount' in model
        # 'sales': ". A message naming the object again would print it twice.
        for found in (
            vendor_check.check({"_v": 999}, _Site()),
            vendor_check.check({"_v": STASH_VERSION, "guid": "g"}, _Site()),
            vendor_check.check([], _Site()),
        ):
            for message in found:
                assert "amount" not in message
                assert "orders" not in message

    def test_check_works_without_a_site_at_all(self):
        # `site` is accepted and unused; nothing may come to depend on it
        # until a check actually reads it.
        assert vendor_check.check({"_v": 999}) != []
        assert vendor_check.check({"_v": STASH_VERSION}) == []


def test_every_payload_in_the_shipped_fixtures_passes_its_own_checker():
    """The end-to-end form: our own output must satisfy our own validator.

    This is the test that would have caught a checker which disagrees with the
    writer -- the failure mode where a converter reports its own documents.
    """
    import pathlib

    import yaml

    fixtures = pathlib.Path(__file__).parent / "fixtures"
    seen = 0
    def walk(node, where):
        nonlocal seen
        if isinstance(node, dict):
            for entry in node.get("custom_extensions") or []:
                if entry.get("vendor_name") != VENDOR_KEY:
                    continue
                seen += 1
                found = vendor_check.check(json.loads(entry["data"]))
                assert found == [], f"{where}: {found}"
            for key, value in node.items():
                if key != "custom_extensions":
                    walk(value, where)
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    for path in sorted(fixtures.glob("*/expected.ossie.yaml")):
        walk(yaml.safe_load(path.read_text()), path.parent.name)

    # Guards the loop itself: a glob that matched nothing, or fixtures that
    # stopped carrying a payload, would otherwise pass silently.
    assert seen > 0, "no THOUGHTSPOT payloads found in the fixtures"


def test_the_declared_entry_point_resolves_to_this_checker():
    """pyproject.toml's `ossie.vendor_validators` entry really loads.

    A typo in that module path fails nothing else in this suite -- every other
    test imports `vendor_check` directly. The hook reaches it only through the
    entry point, so this is the one test standing between a rename and a
    validator that reports "No validator is installed for vendor
    'THOUGHTSPOT'" at the moment someone tries to use it.
    """
    from importlib.metadata import entry_points

    registered = {e.name: e for e in entry_points(group="ossie.vendor_validators")}
    assert VENDOR_KEY in registered, (
        f"no entry point named {VENDOR_KEY}; found {sorted(registered)}"
    )
    loaded = registered[VENDOR_KEY].load()
    assert loaded is vendor_check.check
    # And it answers through that path, not merely imports.
    assert loaded({"_v": 999}, None) != []
    assert loaded({"_v": STASH_VERSION}, None) == []
