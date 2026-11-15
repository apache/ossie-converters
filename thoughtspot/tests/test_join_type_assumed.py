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

"""The join type a foreign document does not carry.

An Ossie `Relationship` is a name, two endpoints and two column lists. It has
no join type, so a document this converter did not write has none to recover:
the stash a ThoughtSpot export leaves behind is the only place one is ever
found. ThoughtSpot, meanwhile, *requires* `type` on an inline join -- there is
no "omit it and let the platform decide" (live-verified 2026-07-31: dropping
the sibling `cardinality` fails with "both type and cardinality should be
defined"). So a type has to be chosen here, and the choice is a wrong answer
rather than an error: every value in the vocabulary imports.

`INNER` was the wrong one. `from` is the many side and `to` is the one side,
so an inner join drops every `from` row whose key matches nothing on the `to`
side -- and a dropped fact row lowers every measure computed over it, with
nothing on screen to say so. `LEFT_OUTER` keeps those rows. It is the same
choice this project's own Databricks reference settled on, for the same
reason: Databricks fixes every metric-view join as `LEFT OUTER` and offers no
`type:` field precisely because the semantics are not the author's to pick,
and "anything narrower silently drops rows and lowers every measure"
(`databricks-metric-view.md`, 2026-07-31).

A relationship that *does* carry a stashed type keeps it, unchanged and
unreported -- it is recovered fact, not an assumption.
"""
import json

from ossie_thoughtspot.issues import IssueLog, Severity
from ossie_thoughtspot.ossie_to_thoughtspot import build_model
from ossie_thoughtspot.tml import TmlDocument


def _stash_ext(**payload):
    return [{"vendor_name": "THOUGHTSPOT", "data": json.dumps({"_v": 1, **payload})}]


def _table(name, columns):
    return TmlDocument(
        kind="table",
        body={
            "name": name, "db": "SALES", "schema": "PUBLIC", "db_table": name,
            "connection": {"name": "My Snowflake"},
            "columns": [{"name": c, "db_column_name": c,
                         "db_column_properties": {"data_type": "INT64"}} for c in columns],
        },
        guid=None,
    )


def _dataset(name, columns):
    return {
        "name": name,
        "source": {"database": "SALES", "schema": "PUBLIC", "table": name},
        "fields": [
            {"name": c, "expression": {"dialects": [
                {"dialect": "THOUGHTSPOT", "expression": f"[{name}::{c}]"},
                {"dialect": "ANSI_SQL", "expression": c},
            ]}}
            for c in columns
        ],
    }


def _model(rel_stash=None):
    relationship: dict = {
        "name": "orders_to_customers",
        "from": "ORDERS", "to": "CUSTOMERS",
        "from_columns": ["CUSTOMER_ID"], "to_columns": ["CUSTOMER_ID"],
    }
    if rel_stash is not None:
        relationship["custom_extensions"] = _stash_ext(**rel_stash)
    return {
        "name": "sales",
        "datasets": [_dataset("ORDERS", ["CUSTOMER_ID", "AMOUNT"]),
                     _dataset("CUSTOMERS", ["CUSTOMER_ID", "NAME"])],
        "relationships": [relationship],
    }


def _build(rel_stash=None):
    log = IssueLog()
    doc = build_model(
        _model(rel_stash),
        [_table("ORDERS", ["CUSTOMER_ID", "AMOUNT"]),
         _table("CUSTOMERS", ["CUSTOMER_ID", "NAME"])],
        log,
    )
    orders = next(t for t in doc.body["model_tables"] if t["name"] == "ORDERS")
    [join] = orders["joins"]
    return join, log


def _issues(log, code):
    return [i for i in log.as_dicts() if i.get("code") == code]


class TestAForeignRelationshipKeepsEveryFromRow:
    """No stash means no declared type, so the safe one is chosen and said."""

    def test_the_join_is_left_outer(self):
        join, _log = _build()
        assert join["type"] == "LEFT_OUTER"

    def test_an_issue_names_the_assumption(self):
        _join, log = _build()
        [issue] = _issues(log, "TS-JOIN-TYPE-ASSUMED")
        assert issue["severity"] == Severity.WARNING.value
        assert "orders_to_customers" in issue["message"]
        assert "LEFT_OUTER" in issue["message"]

    def test_the_issue_names_the_relationship_it_is_about(self):
        _join, log = _build()
        [issue] = _issues(log, "TS-JOIN-TYPE-ASSUMED")
        assert issue["object_ref"] == "relationship:orders_to_customers"


class TestAStashedTypeIsRecoveredFactNotAnAssumption:
    """A ThoughtSpot-sourced document carries the author's own choice."""

    def test_a_stashed_inner_stays_inner(self):
        join, log = _build({"type": "INNER"})
        assert join["type"] == "INNER"
        assert _issues(log, "TS-JOIN-TYPE-ASSUMED") == []

    def test_a_stashed_left_outer_stays_left_outer(self):
        join, log = _build({"type": "LEFT_OUTER"})
        assert join["type"] == "LEFT_OUTER"
        assert _issues(log, "TS-JOIN-TYPE-ASSUMED") == []

    def test_a_stashed_full_outer_is_still_renamed_to_outer(self):
        # `_normalise_join_type` runs on the stashed value either way; the
        # assumption must not have displaced it.
        join, log = _build({"type": "FULL_OUTER"})
        assert join["type"] == "OUTER"
        assert _issues(log, "TS-JOIN-TYPE-ASSUMED") == []
