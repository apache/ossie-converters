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

import re

from ossie_thoughtspot import _yaml
from ossie_thoughtspot.issues import IssueLog, Severity
from ossie_thoughtspot.keys import Relationship, derive_keys


def rel(name, to_columns, cardinality="MANY_TO_ONE", residual=False):
    return Relationship(
        name=name,
        to_dataset="customers",
        to_columns=to_columns,
        cardinality=cardinality,
        has_residual_predicates=residual,
    )


def test_single_qualifying_relationship_yields_a_primary_key():
    log = IssueLog()
    pk, uniques = derive_keys("customers", [rel("r1", ["customer_id"])], log)
    assert pk == ["customer_id"]
    assert uniques == [["customer_id"]]
    assert log.as_dicts() == []


def test_disagreeing_qualifying_relationships_yield_unique_keys_and_no_primary_key():
    # Choosing one would be a guess; both are real join targets.
    log = IssueLog()
    pk, uniques = derive_keys(
        "customers", [rel("by_id", ["customer_id"]), rel("by_email", ["email"])], log
    )
    assert pk is None
    assert sorted(uniques) == [["customer_id"], ["email"]]


def test_residual_predicate_relationship_is_not_key_evidence():
    # The equality columns alone are not unique — the narrowing makes it to-one.
    log = IssueLog()
    pk, uniques = derive_keys("customers", [rel("asof", ["ccy"], residual=True)], log)
    assert pk is None
    assert uniques == []


def test_many_to_many_is_not_key_evidence():
    log = IssueLog()
    pk, uniques = derive_keys("customers", [rel("bridge", ["c_id"], cardinality="MANY_TO_MANY")], log)
    assert pk is None
    assert uniques == []


def test_a_disqualified_sibling_raises_an_issue_naming_it():
    # "ccy" does not cover the derived key ("customer_id"), so
    # upstream's to_columns coverage check -- `validate_references`'s
    # `set(key) <= set(to_columns)` test -- genuinely
    # will warn here — the claim is correct and must be present.
    log = IssueLog()
    pk, uniques = derive_keys(
        "customers", [rel("by_id", ["customer_id"]), rel("asof", ["ccy"], residual=True)], log
    )
    assert pk == ["customer_id"]
    issues = log.as_dicts()
    assert len(issues) == 1
    assert issues[0]["severity"] == Severity.WARNING.value
    assert "asof" in issues[0]["message"]
    assert "coverage warning" in issues[0]["message"]


def test_residual_join_whose_columns_cover_the_key_has_no_upstream_warning_claim():
    # The canonical SCD-2 shape — a residual (as-of) join whose
    # to_columns exactly covers the derived key. Upstream's coverage check
    # (`validate_references`) passes clean here, so the message must not
    # predict a warning that will not fire.
    log = IssueLog()
    pk, uniques = derive_keys(
        "customers",
        [rel("by_id", ["customer_id"]), rel("scd2_asof", ["customer_id"], residual=True)],
        log,
    )
    assert pk == ["customer_id"]
    issues = log.as_dicts()
    assert len(issues) == 1
    assert issues[0]["severity"] == Severity.WARNING.value
    assert "scd2_asof" in issues[0]["message"]
    assert "coverage warning" not in issues[0]["message"]


def test_column_order_within_a_composite_key_is_preserved():
    log = IssueLog()
    pk, _ = derive_keys("customers", [rel("r", ["region", "customer_id"])], log)
    assert pk == ["region", "customer_id"]


def test_empty_to_columns_yields_no_key_and_raises_an_error():
    # An empty to_columns is a hard schema failure (minItems: 1) — such a
    # relationship cannot be emitted at all, so there is no upstream check
    # left to run and no coverage warning to predict. This is ERROR, not
    # WARNING, and the remedy must not claim it is "Expected".
    log = IssueLog()
    pk, uniques = derive_keys("customers", [rel("blank", [])], log)
    assert pk is None
    assert uniques == []
    issues = log.as_dicts()
    assert len(issues) == 1
    assert "blank" in issues[0]["message"]
    assert issues[0]["severity"] == Severity.ERROR.value
    assert "coverage warning" not in issues[0]["message"]
    assert not issues[0]["remedy"].lower().startswith("expected")


def test_agreeing_qualifying_relationships_collapse_to_one_unique_key():
    # A dimension joined from several fact tables on the same foreign key is
    # a common shape — it must not be mistaken for disagreement.
    log = IssueLog()
    pk, uniques = derive_keys(
        "customers", [rel("from_orders", ["customer_id"]), rel("from_invoices", ["customer_id"])], log
    )
    assert pk == ["customer_id"]
    assert uniques == [["customer_id"]]
    assert log.as_dicts() == []


class TestDerivedKeysDoNotAliasEachOther:
    """`primary_key` and `unique_keys[0]` must not be the SAME list object.

    When they were, PyYAML emitted `primary_key: &id001` / `- *id001`. The
    existing guard reads the committed FIXTURE's text, so reverting
    `keys.py`'s `list(...)` left the whole suite green -- an artifact test
    cannot see a change to the code that produces the artifact. This one dumps
    freshly derived keys and asserts on that text.
    """

    @staticmethod
    def _derive_one_key():
        return derive_keys("customers", [rel("r1", ["customer_id"])], IssueLog())

    def test_primary_key_is_not_the_same_object_as_a_unique_key(self):
        primary_key, unique_keys = self._derive_one_key()
        assert primary_key is not None and unique_keys
        assert all(primary_key is not entry for entry in unique_keys), (
            "primary_key is the same list object as a unique_keys entry; PyYAML "
            "will emit a YAML anchor/alias pair for it"
        )

    def test_freshly_dumped_keys_carry_no_anchor_or_alias(self):
        primary_key, unique_keys = self._derive_one_key()
        text = _yaml.dump({"primary_key": primary_key, "unique_keys": unique_keys})
        assert not re.search(r"(?:^|\s)[&*]id\d+\b", text), (
            f"generated YAML contains an anchor/alias:\n{text}"
        )


class TestAKeyIsTheSetOfColumnsItNames:
    """A key is the set of fields its entries name, not the list as written.

    `derive_keys` builds each candidate from a relationship's `to_columns`
    verbatim and compares candidates by exact list equality. Neither holds up:

    * `to_columns` corresponds POSITIONALLY to `from_columns`, so one column
      of the `to` dataset may legitimately be compared with two of the `from`
      dataset -- apache/ossie#705 says so explicitly, and leaves `to_columns`
      alone for that reason. Copied into a key, that repeat claims a column
      the dataset does not have.
    * Two relationships naming the same columns in a different order, or in a
      different case, are one key written twice. Read as two, the dataset is
      judged to disagree with itself and loses its primary key.

    Regular identifiers are case-insensitive (`expression_language.md`), which
    is what makes the case axis part of the same rule rather than a second one.
    """

    def test_a_column_named_twice_in_one_relationship_is_one_column(self):
        log = IssueLog()
        pk, uniques = derive_keys("customers", [rel("r", ["customer_id", "customer_id"])], log)
        assert pk == ["customer_id"]
        assert uniques == [["customer_id"]]

    def test_the_same_two_columns_in_a_different_order_are_one_key(self):
        # Two fact tables on one composite-key dimension, the AND written the
        # other way round. The order is the author's choice, so reading it as
        # a second key costs the dimension its primary key.
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers",
            [rel("from_orders", ["store_id", "date_id"]),
             rel("from_returns", ["date_id", "store_id"])],
            log,
        )
        assert pk == ["store_id", "date_id"]
        assert uniques == [["store_id", "date_id"]]

    def test_spellings_differing_only_in_case_are_one_key(self):
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers",
            [rel("from_orders", ["customer_id"]), rel("from_returns", ["CUSTOMER_ID"])],
            log,
        )
        assert pk == ["customer_id"]
        assert uniques == [["customer_id"]]

    def test_a_disqualified_sibling_spelling_the_key_differently_is_predicted_uncovered(self):
        # The coverage claim is only reached for a DISQUALIFIED relationship,
        # so the two here cannot both qualify or the branch never runs -- an
        # earlier version of this test made that mistake and passed under a
        # mutation that reverted the fold.
        #
        # This converter reads `CUSTOMER_ID` and `customer_id` as one field,
        # and emits one key. Upstream does NOT: `validate.py` compares key
        # columns as exact strings. So the prediction has to be made on the
        # spellings as written, or it foresees the opposite of what upstream
        # reports. An earlier version of this file folded both sides and
        # asserted the reverse of the line below.
        log = IssueLog()
        pk, _ = derive_keys(
            "customers",
            [rel("by_id", ["customer_id"]),
             rel("scd2_asof", ["CUSTOMER_ID"], residual=True)],
            log,
        )
        assert pk == ["customer_id"]
        issues = log.as_dicts()
        assert len(issues) == 1
        assert "scd2_asof" in issues[0]["message"]
        assert "coverage warning" in issues[0]["message"]

    def test_the_first_spelling_seen_is_the_one_emitted(self):
        # Folding decides what counts as the same key; it must not rewrite the
        # column names, which have to match the fields the dataset declares.
        log = IssueLog()
        pk, _ = derive_keys(
            "customers",
            [rel("from_orders", ["CustomerId"]), rel("from_returns", ["customerid"])],
            log,
        )
        assert pk == ["CustomerId"]

    def test_genuinely_different_keys_still_disagree(self):
        # The guard on over-folding: these name different fields, so the
        # dataset really does have two candidates and no primary key.
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers", [rel("by_id", ["customer_id"]), rel("by_email", ["email"])], log
        )
        assert pk is None
        assert sorted(uniques) == [["customer_id"], ["email"]]

    def test_a_subset_is_not_the_same_key(self):
        # [a] and [a, b] fold to different sets, so they stay two candidates.
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers", [rel("one", ["a"]), rel("two", ["a", "b"])], log
        )
        assert pk is None
        assert len(uniques) == 2


class TestASpellingUpstreamWillNotMatchIsReported:
    """Folding decides what is ONE key. Upstream does not fold at all.

    `validate.py` does `set(to_columns)` then `any(set(key) <= …)` -- exact
    strings, no case handling. So the moment two qualifying relationships
    spell one key column differently, whichever spelling is emitted leaves
    the others uncovered BY UPSTREAM'S RULE, and upstream raises a
    to_columns coverage warning for them.

    That warning is correct and the deduplication is still right: declaring
    two unique keys that are one key in two cases asserts something about
    the data that is not true, and it only silenced upstream by accident.
    What was wrong was this converter saying nothing. The prediction folded
    both sides, so it could never foresee a warning upstream raises on an
    exact comparison -- a comment in the source claimed the opposite.

    Nothing fires in the ordinary case, where every relationship spells the
    key the same way.
    """

    def _derive(self, *column_sets):
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers",
            [rel(f"r{i}", cols) for i, cols in enumerate(column_sets)],
            log,
        )
        return pk, uniques, [i for i in log.as_dicts() if i["code"] == "TS_KEY_COVERAGE"]

    def test_one_key_is_still_emitted(self):
        pk, uniques, _ = self._derive(["CUSTOMER_ID"], ["customer_id"])
        assert pk == ["CUSTOMER_ID"]
        assert uniques == [["CUSTOMER_ID"]]

    def test_the_divergent_relationship_is_reported(self):
        _pk, _uniques, issues = self._derive(["CUSTOMER_ID"], ["customer_id"])
        assert len(issues) == 1
        assert issues[0]["object_ref"] == "relationship:r1"
        assert "customer_id" in issues[0]["message"]

    def test_the_relationship_whose_spelling_was_emitted_is_not_reported(self):
        _pk, _uniques, issues = self._derive(["CUSTOMER_ID"], ["customer_id"])
        assert "relationship:r0" not in [i["object_ref"] for i in issues]

    def test_agreeing_spellings_report_nothing(self):
        _pk, _uniques, issues = self._derive(["customer_id"], ["customer_id"])
        assert issues == []

    def test_a_single_relationship_reports_nothing(self):
        _pk, _uniques, issues = self._derive(["customer_id"])
        assert issues == []


def test_folding_is_ascii_case_not_unicode_casefold():
    # `.casefold()` is for caseless MATCHING of natural language: it maps the
    # German sharp s to a two-character form and changes the length of the
    # dotted capital I. SQL identifier case-insensitivity is neither of those,
    # so two genuinely distinct identifiers were being read as one key.
    log = IssueLog()
    _pk, uniques = derive_keys(
        "customers", [rel("a", ["strasse"]), rel("b", ["straße"])], log
    )
    assert sorted(uniques) == [["strasse"], ["straße"]]


class TestThePredictionMatchesUpstreamsRuleExactly:
    """This prediction's only job is to say what upstream will say.

    Both halves of upstream's test were unpinned, and a mutation of either
    left the suite green. `validate_references` is:

        if declared_keys and not any(set(key) <= to_column_set …)

    -- so it is silent when the dataset declares NO key, and it asks whether
    the key is a SUBSET of to_columns, not whether the two are equal.
    """

    def test_a_keyless_dataset_is_predicted_clean(self):
        # Nothing qualifies, so no key is declared, so upstream has nothing
        # to compare against and raises nothing. Predicting a warning here
        # would be a warning about every residual join in every model that
        # happens to have no key at all.
        log = IssueLog()
        pk, uniques = derive_keys(
            "customers", [rel("asof", ["ccy"], residual=True)], log
        )
        assert (pk, uniques) == (None, [])
        [issue] = log.as_dicts()
        assert "coverage warning" not in issue["message"]

    def test_a_key_that_is_a_subset_of_to_columns_is_predicted_covered(self):
        # `scd2` joins on the key PLUS an extra column. Upstream's `<=` says
        # that covers the key; `==` would say it does not.
        log = IssueLog()
        pk, _ = derive_keys(
            "customers",
            [rel("by_id", ["customer_id"]),
             rel("scd2", ["customer_id", "valid_from"], residual=True)],
            log,
        )
        assert pk == ["customer_id"]
        [issue] = log.as_dicts()
        assert "scd2" in issue["message"]
        assert "coverage warning" not in issue["message"]
