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

"""Tests for the DAX function registry: every DAX function has a defined outcome."""

import re

import pytest

from ossie_microsoft._dax_catalog import CATALOG, classify, refusal_reason
from ossie_microsoft._dax_to_sql import TRANSLATED_FUNCTIONS, translate_measure

TIERS = {f"T{n}" for n in range(8)}
VERDICT = re.compile(
    r"PORTABLE|PORTABLE\+RULE|EXTENDED-SQL|QUERY|REFUSE|N/A|SPEC-GAP\([a-z-]+\)"
)


def test_every_dax_function_is_classified():
    assert len(CATALOG) == 392
    for name, (tier, verdict) in CATALOG.items():
        assert name == name.upper(), name
        assert tier in TIERS, name
        assert VERDICT.fullmatch(verdict), (name, verdict)


def test_lookup_is_case_insensitive():
    assert classify("calculate") == classify("CALCULATE")
    assert classify("NOT_A_FUNCTION") is None


def test_translated_functions_are_classified_as_portable():
    for name in TRANSLATED_FUNCTIONS:
        assert CATALOG[name][1] in ("PORTABLE", "PORTABLE+RULE"), name


@pytest.mark.parametrize("name", sorted(CATALOG))
def test_every_function_translates_or_is_refused_by_name(name):
    """The whole registry, through the real translator: each function either has a
    translation or is refused with a reason that names it. Nothing is silently dropped
    and nothing falls through to a generic error."""

    def resolve(table, column):
        return (table, column) if (table, column) == ("Sales", "Amount") else None

    sql, reason = translate_measure(f"{name}('Sales'[Amount])", resolve)
    if name in TRANSLATED_FUNCTIONS and sql is not None:
        assert reason is None
        return
    assert sql is None
    assert reason
    if name not in TRANSLATED_FUNCTIONS:
        assert name in reason.upper(), (name, reason)


def test_unknown_function_reason():
    assert "not a DAX function known" in refusal_reason("FOO")
