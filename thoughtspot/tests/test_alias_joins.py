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

"""Joins in a model that aliases a table.

ThoughtSpot has no multi-condition role-playing join. The same table is listed
in `model_tables[]` once per role, each entry carrying a distinct `alias`, and
every `column_id` then uses that alias as its prefix.

**Two namespaces meet in one join, and they are not the same namespace.**

* The MODEL namespace is what `alias or name` produces. It names the datasets,
  it is what `column_id` prefixes use, and it is what `joins[].with` names.
* The PHYSICAL namespace is the table's own `name`. A Table TML's
  `joins_with[]` lives on the table, so its `destination.name` and every
  prefix inside its `on` condition are physical -- the table knows nothing
  about the aliases a model gives it.

ThoughtSpot enforces that split: a join condition "may name ONLY the two
joined entries, by PHYSICAL name", and an alias name inside `on` is rejected
with "Invalid srcTable or destTable in join expression" (live-probed on a
2026-10-21 cluster; re-confirmed 2026-11-16 by importing every shape below).

Reading one namespace as the other costs every join in an aliased model:

* the referencing form resolved its target from `destination.name`, a
  physical name that is no dataset when the target is aliased, and the
  relationship was dropped as having an unknown target;
* both forms then matched the condition's physical prefixes against the
  model-level ones, found no equality pair, and demoted the join to an
  unrepresentable stash entry.

Measured on a document exported from a real cluster: three datasets, zero
relationships. Role-playing is the ordinary way to model a dimension used
twice, so this was every join in every such model.
"""
import pytest

from ossie_thoughtspot import ossie_to_thoughtspot, tml_to_ossie
from ossie_thoughtspot.tml import DocumentSet, TmlDocument

FACT_JOIN = "fact_to_dim"


def table(name, columns, joins_with=None):
    body = {
        "name": name, "db": "DL", "schema": "PUBLIC", "db_table": name,
        "connection": {"name": "Probe Connection"},
        "columns": [{"name": c, "db_column_name": c,
                     "db_column_properties": {"data_type": "INT64"}} for c in columns],
    }
    if joins_with is not None:
        body["joins_with"] = joins_with
    return TmlDocument(kind="table", body=body, guid=None)


def fact(joins_with=None):
    return table("FACT", ["QTY", "ACCOUNTID"], joins_with)


def dim():
    return table("DIM", ["ACCOUNTID", "EMP_NAME"])


#: A Table-level join, exactly as a cluster exports one: the destination and
#: both prefixes inside `on` are PHYSICAL names.
def physical_joins_with():
    return [{
        "name": FACT_JOIN,
        "destination": {"name": "DIM"},
        "on": "[FACT::ACCOUNTID] = [DIM::ACCOUNTID]",
        "type": "INNER",
        "cardinality": "MANY_TO_ONE",
    }]


def model(model_tables, columns=None):
    return TmlDocument(kind="model", body={
        "name": "probe",
        "model_tables": model_tables,
        "columns": columns if columns is not None else [
            {"name": "Qty", "column_id": "FACT::QTY",
             "properties": {"column_type": "MEASURE", "aggregation": "SUM"}},
        ],
    }, guid=None)


def convert(model_doc, tables):
    return tml_to_ossie.convert(DocumentSet(model=model_doc, tables=tuple(tables)))


def relationships(result):
    return result.model.get("relationships") or []


def codes(result):
    return [i.get("code") for i in result.issues.as_dicts()]


class TestAnAliasedJoinSurvives:
    """One relationship out, named in the MODEL namespace, columns intact."""

    def test_the_dimension_is_aliased(self):
        result = convert(
            model([
                {"name": "FACT", "joins": [{"with": "EMP_A", "referencing_join": FACT_JOIN}]},
                {"name": "DIM", "alias": "EMP_A"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        rels = relationships(result)
        assert len(rels) == 1
        assert rels[0]["from"] == "FACT"
        assert rels[0]["to"] == "EMP_A"
        assert rels[0]["from_columns"] == ["ACCOUNTID"]
        assert rels[0]["to_columns"] == ["ACCOUNTID"]
        assert "TS-JOIN-UNKNOWN-TARGET" not in codes(result)
        assert "TS-JOIN-UNREPRESENTABLE" not in codes(result)

    def test_the_fact_is_aliased(self):
        result = convert(
            model([
                {"name": "FACT", "alias": "FACT_A",
                 "joins": [{"with": "DIM", "referencing_join": FACT_JOIN}]},
                {"name": "DIM"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        rels = relationships(result)
        assert len(rels) == 1
        assert rels[0]["from"] == "FACT_A"
        assert rels[0]["to"] == "DIM"
        assert rels[0]["from_columns"] == ["ACCOUNTID"]
        assert "TS-JOIN-UNREPRESENTABLE" not in codes(result)

    def test_both_ends_are_aliased(self):
        result = convert(
            model([
                {"name": "FACT", "alias": "FACT_A",
                 "joins": [{"with": "EMP_A", "referencing_join": FACT_JOIN}]},
                {"name": "DIM", "alias": "EMP_A"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        rels = relationships(result)
        assert len(rels) == 1
        assert rels[0]["from"] == "FACT_A"
        assert rels[0]["to"] == "EMP_A"

    def test_an_inline_join_reads_the_physical_prefixes_too(self):
        # The inline form carries its own `on`, but ThoughtSpot holds it to
        # the same rule: physical names only. Accepted by a cluster on
        # 2026-11-16 with status OK.
        result = convert(
            model([
                {"name": "FACT", "joins": [{
                    "with": "EMP_A",
                    "on": "[FACT::ACCOUNTID] = [DIM::ACCOUNTID]",
                    "type": "INNER", "cardinality": "MANY_TO_ONE"}]},
                {"name": "DIM", "alias": "EMP_A"},
            ]),
            [fact(), dim()],
        )
        rels = relationships(result)
        assert len(rels) == 1
        assert rels[0]["to"] == "EMP_A"
        assert rels[0]["to_columns"] == ["ACCOUNTID"]

    def test_two_roles_of_one_dimension_both_survive(self):
        # The shape the whole thing exists for. Both roles cite the SAME
        # Table join, which a cluster accepts.
        result = convert(
            model([
                {"name": "FACT", "joins": [
                    {"with": "EMP_A", "referencing_join": FACT_JOIN},
                    {"with": "EMP_B", "referencing_join": FACT_JOIN}]},
                {"name": "DIM", "alias": "EMP_A"},
                {"name": "DIM", "alias": "EMP_B"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        assert sorted(r["to"] for r in relationships(result)) == ["EMP_A", "EMP_B"]


class TestTheKeyFollowsTheAlias:
    def test_an_aliased_dimension_gets_the_derived_key(self):
        # A dropped relationship is not key evidence either, so the aliased
        # dataset previously lost its primary key along with its join.
        result = convert(
            model([
                {"name": "FACT", "joins": [{"with": "EMP_A", "referencing_join": FACT_JOIN}]},
                {"name": "DIM", "alias": "EMP_A"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        by_name = {d["name"]: d for d in result.model["datasets"]}
        assert by_name["EMP_A"].get("primary_key") == ["ACCOUNTID"]


class TestTheUnaliasedCaseIsUnchanged:
    """The regression guard. Without an alias the two namespaces coincide,
    so nothing about this may move."""

    def test_a_plain_referencing_join_still_converts(self):
        result = convert(
            model([
                {"name": "FACT", "joins": [{"with": "DIM", "referencing_join": FACT_JOIN}]},
                {"name": "DIM"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        rels = relationships(result)
        assert len(rels) == 1
        assert (rels[0]["from"], rels[0]["to"]) == ("FACT", "DIM")

    def test_a_join_naming_a_dataset_the_model_does_not_have_is_still_reported(self):
        # Folding the two namespaces together must not become a way to
        # accept a target that genuinely is not there.
        result = convert(
            model([
                {"name": "FACT", "joins": [{"with": "NOT_A_DATASET",
                                            "referencing_join": FACT_JOIN}]},
                {"name": "DIM"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        assert relationships(result) == []
        assert "TS-JOIN-UNKNOWN-TARGET" in codes(result)


class TestAnAmbiguousConditionIsNotGuessed:
    """Defensive only: ThoughtSpot rejects every self-join (13125 / 14565),
    so a condition whose two ends share one physical name cannot reach us
    from a cluster. It can reach us from hand-written TML, and guessing an
    orientation there would silently reverse a join."""

    def test_two_aliases_of_one_table_do_not_resolve_by_physical_name(self):
        self_join = [{
            "name": "self", "destination": {"name": "DIM"},
            "on": "[DIM::ACCOUNTID] = [DIM::EMP_NAME]",
            "type": "INNER", "cardinality": "MANY_TO_ONE",
        }]
        result = convert(
            model([
                {"name": "DIM", "alias": "D_A",
                 "joins": [{"with": "D_B", "referencing_join": "self"}]},
                {"name": "DIM", "alias": "D_B"},
            ]),
            [table("DIM", ["ACCOUNTID", "EMP_NAME"], self_join)],
        )
        assert relationships(result) == []
        assert "TS-JOIN-AMBIGUOUS-PREFIX" in codes(result)


class TestTheReturnLegWritesPhysicalNames:
    """Ossie -> TML. The same split, in the other direction.

    A Table's `joins_with[]` entry is written for the TABLE, so its
    `destination` and its `on` have to be physical whatever the model calls
    the datasets. Writing the dataset name produced a Model that ThoughtSpot
    refuses: the round trip below was rejected with `Schema validation
    failed` until 2026-11-16, and is accepted now (both `--policy
    VALIDATE_ONLY`, same cluster, same document).
    """

    def _round_trip(self):
        forward = convert(
            model([
                {"name": "FACT", "joins": [{"with": "EMP_A", "referencing_join": FACT_JOIN}]},
                {"name": "DIM", "alias": "EMP_A"},
            ]),
            [fact(physical_joins_with()), dim()],
        )
        assert len(relationships(forward)) == 1, "the forward leg must produce the join"
        return ossie_to_thoughtspot.convert(forward.model)

    def test_the_table_join_destination_is_the_table_not_the_alias(self):
        back = self._round_trip()
        fact_doc = next(d for d in back.documents.tables if d.body["name"] == "FACT")
        entry = fact_doc.body["joins_with"][0]
        assert entry["destination"]["name"] == "DIM"

    def test_the_condition_names_the_table_not_the_alias(self):
        back = self._round_trip()
        fact_doc = next(d for d in back.documents.tables if d.body["name"] == "FACT")
        on = fact_doc.body["joins_with"][0]["on"]
        assert "DIM::" in on
        assert "EMP_A" not in on

    def test_the_model_entry_still_targets_the_alias(self):
        # `with` is the one place the MODEL namespace belongs, and it must
        # not be dragged along by the fix to the other two.
        back = self._round_trip()
        entries = back.documents.model.body["model_tables"]
        fact_entry = next(e for e in entries if e.get("name") == "FACT")
        assert fact_entry["joins"][0]["with"] == "EMP_A"
        assert any(e.get("alias") == "EMP_A" for e in entries)


class TestASelfRelationshipIsDeclaredUnwritable:
    """Ossie permits two datasets over one source with a relationship between
    them. ThoughtSpot has no self-join in any spelling, so there is no TML to
    write -- and emitting one anyway produced a Model the platform refuses,
    with nothing said about why."""

    def _document_with_a_self_relationship(self):
        forward = convert(
            model([
                {"name": "DIM", "alias": "D_A"},
                {"name": "DIM", "alias": "D_B"},
            ], columns=[{"name": "A Name", "column_id": "D_A::EMP_NAME",
                         "properties": {"column_type": "ATTRIBUTE"}},
                        {"name": "B Name", "column_id": "D_B::EMP_NAME",
                         "properties": {"column_type": "ATTRIBUTE"}}]),
            [dim()],
        )
        doc = forward.model
        doc["relationships"] = [{
            "name": "d_a_to_d_b", "from": "D_A", "to": "D_B",
            "from_columns": ["ACCOUNTID"], "to_columns": ["ACCOUNTID"],
        }]
        return doc

    def test_no_join_is_written_and_the_loss_is_reported(self):
        back = ossie_to_thoughtspot.convert(self._document_with_a_self_relationship())
        entries = back.documents.model.body["model_tables"]
        assert all(not e.get("joins") for e in entries)
        issues = [i for i in back.issues.as_dicts() if i["code"] == "TS-JOIN-SELF-UNWRITABLE"]
        assert len(issues) == 1
        assert issues[0]["severity"] == "ERROR"
        assert "d_a_to_d_b" in issues[0]["message"]

    def test_no_table_join_is_written_either(self):
        back = ossie_to_thoughtspot.convert(self._document_with_a_self_relationship())
        for doc in back.documents.tables:
            assert not doc.body.get("joins_with")
