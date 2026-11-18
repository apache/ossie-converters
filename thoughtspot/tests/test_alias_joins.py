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

A Table's `joins_with[]` is unambiguously physical: it lives on the table,
which has no knowledge of the aliases a model gives it. Every shape written
in physical names was imported on a 2026-11-16 cluster with status OK.

The MODEL's own inline `joins[]` is where the two namespaces meet, and the
evidence there is thinner than it first looked. An alias-spelled condition
was refused with "Invalid srcTable or destTable in join expression" -- but
on a document that was independently invalid (it was also a self-join), so
the refusal cannot be pinned on the alias names alone, and the
two-different-tables case has not been probed.

So the converter is strict in what it sends and liberal in what it accepts:
the writer emits physical names, which are known to import; the reader takes
either spelling, because real documents use both and refusing one on an
unproven rule costs a relationship that would have converted.

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
import json

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


def codes_of(conversion):
    return [i.get("code") for i in conversion.issues.as_dicts()]


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

    def test_a_stashed_referencing_join_is_dropped_too(self):
        # The sibling above cannot fail on its own: with no stash there is
        # no `referencing_join` pointer, so the joins_with[] branch is never
        # reached and the assertion holds however the guard behaves. This
        # one takes that branch -- a stashed pointer is exactly what makes
        # the converter try to write a Table join -- so it fails if the
        # guard stops dropping the relationship.
        doc = self._document_with_a_self_relationship()
        doc["relationships"][0]["custom_extensions"] = [{
            "vendor_name": "THOUGHTSPOT",
            "data": json.dumps({"_v": 1, "referencing_join": "d_a_to_d_b",
                                "join_shape": "referencing"}),
        }]
        back = ossie_to_thoughtspot.convert(doc)
        for table_doc in back.documents.tables:
            assert not table_doc.body.get("joins_with")
        assert "TS-JOIN-SELF-UNWRITABLE" in codes_of(back)

    def test_a_self_relationship_assumes_no_join_type(self):
        # The assumption is reported before the guard runs, so a WARNING
        # arrived for a join that is never written, carrying a remedy
        # ("set `type` on this join in the generated Model TML") pointing at
        # something not in the output.
        back = ossie_to_thoughtspot.convert(self._document_with_a_self_relationship())
        assert "TS-JOIN-TYPE-ASSUMED" not in codes_of(back)


class TestEitherNamespaceIsAcceptedInAnInlineCondition:
    """Strict in what we send, liberal in what we accept.

    The WRITE leg emits physical names, because that is what a Table's
    `joins_with[]` requires and what a cluster accepted. The READ leg must
    not mirror that strictness: an inline `joins[]` entry lives on the
    MODEL, whose every other reference (`with`, each `column_id` prefix) is
    in the alias namespace, and real documents write its `on` that way --
    the two-role fixture in this project's own audit corpus does.

    Matching only one namespace does not fix the class, it moves it. Before
    the alias work this function matched the model namespace alone, so an
    alias-spelled condition converted and a physical-spelled one was demoted
    to an unrepresentable stash entry. Matching the physical namespace alone
    simply swapped which half was broken, and the swap is invisible to every
    round-trip test: the verbatim `on_expression` is stashed and replayed,
    so TML -> Ossie -> TML still comes back byte-identical. What is lost is
    the RELATIONSHIP in the Ossie document -- the thing every other
    converter in this repository reads, and the thing the alias work exists
    to deliver.

    The two names cannot collide: a prefix is either the physical name of
    one endpoint or the model name of one endpoint, and `with` already had
    to name a model_tables entry for the join to be parsed at all.
    """

    ALIASED = [
        {"name": "FACT", "joins": [{
            "with": "EMP_A",
            "on": "[{f}::ACCOUNTID] = [{t}::ACCOUNTID]",
            "type": "INNER", "cardinality": "MANY_TO_ONE"}]},
        {"name": "DIM", "alias": "EMP_A"},
    ]

    def _convert(self, from_prefix, to_prefix):
        import copy
        tables = copy.deepcopy(self.ALIASED)
        tables[0]["joins"][0]["on"] = (
            tables[0]["joins"][0]["on"].format(f=from_prefix, t=to_prefix)
        )
        return convert(model(tables), [fact(), dim()])

    def test_the_condition_may_name_the_alias(self):
        result = self._convert("FACT", "EMP_A")
        rels = relationships(result)
        assert len(rels) == 1, codes(result)
        assert (rels[0]["from"], rels[0]["to"]) == ("FACT", "EMP_A")
        assert rels[0]["to_columns"] == ["ACCOUNTID"]
        assert "TS-JOIN-UNREPRESENTABLE" not in codes(result)

    def test_the_condition_may_name_the_physical_table(self):
        result = self._convert("FACT", "DIM")
        rels = relationships(result)
        assert len(rels) == 1, codes(result)
        assert (rels[0]["from"], rels[0]["to"]) == ("FACT", "EMP_A")
        assert rels[0]["to_columns"] == ["ACCOUNTID"]
        assert "TS-JOIN-UNREPRESENTABLE" not in codes(result)

    def test_an_aliased_from_side_is_accepted_either_way(self):
        for from_prefix in ("FACT_A", "FACT"):
            result = convert(
                model([
                    {"name": "FACT", "alias": "FACT_A", "joins": [{
                        "with": "DIM",
                        "on": f"[{from_prefix}::ACCOUNTID] = [DIM::ACCOUNTID]",
                        "type": "INNER", "cardinality": "MANY_TO_ONE"}]},
                    {"name": "DIM"},
                ]),
                [fact(), dim()],
            )
            rels = relationships(result)
            assert len(rels) == 1, (from_prefix, codes(result))
            assert rels[0]["from"] == "FACT_A"

    def test_a_condition_naming_neither_endpoint_is_still_residual(self):
        # The widening must not turn an unrelated prefix into a match.
        result = self._convert("FACT", "SOMEWHERE_ELSE")
        assert "TS-JOIN-UNREPRESENTABLE" in codes(result)
        assert relationships(result) == []


class TestADatasetJoinedToItselfIsUnwritableToo:
    """The plainest self-join of all, and the guard let it through.

    `TS-JOIN-SELF-UNWRITABLE` was conditioned on the two datasets having
    DIFFERENT names -- two aliases over one table. A relationship whose
    `from` and `to` are the SAME dataset is the more obvious self-join and
    skipped the guard entirely, emitting `[EMPLOYEE::MGR_ID] =
    [EMPLOYEE::ID]`, which ThoughtSpot refuses outright (14565, a table
    against itself; 13125 against an alias of itself). The two share one
    cause -- one physical table on both ends -- so they share the guard.
    """

    def _convert(self):
        forward = convert(
            model([{"name": "DIM"}],
                  columns=[{"name": "Name", "column_id": "DIM::EMP_NAME",
                            "properties": {"column_type": "ATTRIBUTE"}}]),
            [dim()],
        )
        doc = forward.model
        doc["relationships"] = [{
            "name": "dim_to_dim", "from": "DIM", "to": "DIM",
            "from_columns": ["ACCOUNTID"], "to_columns": ["ACCOUNTID"],
        }]
        return ossie_to_thoughtspot.convert(doc)

    def test_no_join_is_written(self):
        back = self._convert()
        assert all(not e.get("joins") for e in back.documents.model.body["model_tables"])

    def test_the_loss_is_reported(self):
        back = self._convert()
        [issue] = [i for i in back.issues.as_dicts()
                   if i["code"] == "TS-JOIN-SELF-UNWRITABLE"]
        assert issue["severity"] == "ERROR"
        assert "dim_to_dim" in issue["message"]

    def test_no_join_type_is_assumed_for_it(self):
        assert "TS-JOIN-TYPE-ASSUMED" not in codes_of(self._convert())


class TestTMLJoiningAnEntryToItselfIsNotGuessedAtEither:
    """The same widening on the forward leg.

    ThoughtSpot never exports this -- it refuses to import it -- so it
    reaches the converter only from hand-written TML. The condition names
    one table on both sides, so which end each column belongs to cannot be
    determined, and guessing would silently reverse the join. Reported and
    dropped, exactly as two aliases of one table are.
    """

    def _convert(self):
        return convert(
            model([{"name": "DIM", "joins": [{
                "with": "DIM",
                "on": "[DIM::ACCOUNTID] = [DIM::EMP_NAME]",
                "type": "INNER", "cardinality": "MANY_TO_ONE"}]}],
                  columns=[{"name": "Name", "column_id": "DIM::EMP_NAME",
                            "properties": {"column_type": "ATTRIBUTE"}}]),
            [dim()],
        )

    def test_no_relationship_is_emitted(self):
        assert relationships(self._convert()) == []

    def test_the_ambiguity_is_reported(self):
        result = self._convert()
        [issue] = [i for i in result.issues.as_dicts()
                   if i["code"] == "TS-JOIN-AMBIGUOUS-PREFIX"]
        assert "itself" in issue["message"]


class TestTwoRolesCitingOneTableJoinChangeShapeNotMeaning:
    """A known, reported shape change on the return leg.

    Two roles of one dimension may both cite the SAME Table join. Ossie
    relationship names are unique, so the second gets `_2` appended -- and
    that new name no longer matches its own stashed `referencing_join`
    pointer, which is the stale-stash condition. The relationship is
    therefore rebuilt in the inline form instead of the referencing one.

    Nothing is lost and nothing is silent: `TS-JOIN-REFERENCING-JOIN-STALE`
    reports it, the Table keeps its single `joins_with[]` entry (the input
    had one, cited twice), the inline join carries the same condition, and
    a second round trip is stable. Recorded here so the shape change is
    read as known rather than as an accident.
    """

    def _round_trip(self):
        src = model([
            {"name": "FACT", "joins": [
                {"with": "EMP_A", "referencing_join": FACT_JOIN},
                {"with": "EMP_B", "referencing_join": FACT_JOIN}]},
            {"name": "DIM", "alias": "EMP_A"},
            {"name": "DIM", "alias": "EMP_B"},
        ])
        forward = convert(src, [fact(physical_joins_with()), dim()])
        return forward, ossie_to_thoughtspot.convert(forward.model)

    def test_both_roles_survive_as_relationships(self):
        forward, _back = self._round_trip()
        assert [(r["from"], r["to"]) for r in relationships(forward)] == [
            ("FACT", "EMP_A"), ("FACT", "EMP_B"),
        ]

    def test_the_second_role_returns_inline_and_says_so(self):
        _forward, back = self._round_trip()
        entry = next(t for t in back.documents.model.body["model_tables"]
                     if t["name"] == "FACT")
        first, second = entry["joins"]
        assert first == {"with": "EMP_A", "referencing_join": FACT_JOIN}
        assert second["with"] == "EMP_B"
        assert second["on"] == "[FACT::ACCOUNTID] = [DIM::ACCOUNTID]"
        assert "TS-JOIN-REFERENCING-JOIN-STALE" in codes_of(back)

    def test_the_table_keeps_the_one_join_it_started_with(self):
        _forward, back = self._round_trip()
        names = [j.get("name") for d in back.documents.tables
                 for j in (d.body.get("joins_with") or [])]
        assert names == [FACT_JOIN]
