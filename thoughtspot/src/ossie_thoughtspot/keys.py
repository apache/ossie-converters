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

"""primary_key / unique_keys derivation.

TML declares no keys, so every key we emit is manufactured from the
join graph. Upstream PR #330 checks that a relationship's to_columns covers a
declared key, and databricks turns a declared key into a
`rely.at_most_one_match` join hint — so a fabricated key becomes another
vendor's wrong numbers, not just a cosmetic error in ours.

Orientation is re-checked downstream, so do not rely on ours surviving.
`databricks` (`ossie_to_metric_view.py:446-478`) swaps `from`/`to`
and their column arrays — via `_warn()` (`ossie_to_metric_view.py:53`), not
silently — when the *from* side covers a key and the *to* side does not,
carrying any `custom_extensions` payload onto the reversed relationship.
"""
from dataclasses import dataclass
from typing import Sequence

from .issues import IssueLog, Severity

_TO_ONE = frozenset({"MANY_TO_ONE", "ONE_TO_ONE"})


@dataclass(frozen=True)
class Relationship:
    """The subset of a relationship that key derivation needs.

    `frozen=True` implies hashability, but `to_columns` may hold a `list`
    (unhashable), so `hash(instance)` is not reliably safe here — do not put
    a `Relationship` in a set or use it as a dict key without checking the
    concrete `to_columns` type first.
    """

    name: str
    to_dataset: str
    to_columns: tuple[str, ...] | list[str]
    cardinality: str
    has_residual_predicates: bool


def _fold(column: str) -> str:
    """The form two spellings of one regular identifier share.

    `expression_language.md` makes an unquoted identifier case-insensitive, so
    `customer_id` and `CUSTOMER_ID` name one field. Folding decides only what
    counts as the SAME column; the spelling a key emits is always one the
    document used.

    `.lower()`, not `.casefold()`. Casefolding is for caseless matching of
    natural language and is more aggressive than SQL case-insensitivity: it
    maps the German sharp s onto a two-character form and changes the length
    of the dotted capital I, so two genuinely distinct identifiers would be
    read as one key.

    Upstream does not fold at all -- `validate.py` compares key columns as
    exact strings -- so folding here can MERGE two spellings into one key that
    upstream will then find uncovered. `derive_keys` reports that rather than
    leaving it to be discovered at validation time.
    """
    return column.lower()


def _key_columns(to_columns: Sequence[str]) -> list[str]:
    """The columns of `to_columns`, each named once, in the order first seen.

    A key is the set of fields it names. `to_columns` is not a set: it
    corresponds POSITIONALLY to `from_columns`, so one column of the `to`
    dataset may legitimately be compared with two of the `from` dataset, and
    apache/ossie#705 leaves it alone for exactly that reason. Copied into a
    key verbatim, that repeat declares a column the dataset does not have.
    """
    seen: set[str] = set()
    columns: list[str] = []
    for column in to_columns:
        folded = _fold(column)
        if folded not in seen:
            seen.add(folded)
            columns.append(column)
    return columns


def _qualifies(rel: Relationship) -> bool:
    """Key evidence requires a to-one join whose condition is wholly
    equality, and columns actually present to name as the key."""
    return (
        rel.cardinality in _TO_ONE
        and not rel.has_residual_predicates
        and bool(rel.to_columns)
    )


def derive_keys(
    dataset_name: str, relationships: list[Relationship], log: IssueLog
) -> tuple[list[str] | None, list[list[str]]]:
    """Return (primary_key, unique_keys) for one dataset.

    primary_key is emitted only when the qualifying relationships agree on a
    single column set — where they disagree, choosing one is a guess, so the
    candidates go to unique_keys and no primary key is declared.
    """
    inbound = [r for r in relationships if r.to_dataset == dataset_name]
    qualifying = [r for r in inbound if _qualifies(r)]

    # Candidates are compared as SETS of folded names, not as lists as
    # written: the same columns in a different order, or in a different case,
    # are one key written twice. Read as two, a dataset is judged to disagree
    # with itself and loses the primary key it actually has.
    seen: list[list[str]] = []
    folded_seen: list[frozenset[str]] = []
    for rel in qualifying:
        cols = _key_columns(rel.to_columns)
        folded = frozenset(_fold(c) for c in cols)
        if folded not in folded_seen:
            folded_seen.append(folded)
            seen.append(cols)

    # A qualifying relationship whose spelling is not the one emitted.
    #
    # Only reachable once `_fold` has merged two spellings of one key: the
    # emitted key carries the first spelling seen, and upstream compares
    # exactly, so every other spelling is uncovered by its rule and earns a
    # to_columns coverage warning. The deduplication is still right -- two
    # unique keys that are one key in two cases assert a uniqueness the data
    # does not have, and only silenced upstream by accident -- but staying
    # quiet about the consequence was not.
    emitted_exact = [set(key) for key in seen]
    for rel in qualifying:
        columns = set(_key_columns(rel.to_columns))
        if any(key <= columns for key in emitted_exact):
            continue
        log.add(
            code="TS_KEY_COVERAGE",
            severity=Severity.WARNING,
            message=(
                f"Relationship {rel.name!r} targets {dataset_name!r} on "
                f"{sorted(columns)}, which names the same key as the one "
                f"declared but spells it differently. Ossie validation compares "
                f"key columns exactly, so it will report a to_columns coverage "
                f"warning for this relationship."
            ),
            object_ref=f"relationship:{rel.name}",
            remedy=(
                "Spell the column the same way in every relationship that "
                "targets this dataset, and in the dataset's own fields."
            ),
        )

    # Explain every disqualified relationship's non-key status.
    #
    # An empty to_columns is a hard schema failure, not a coverage warning:
    # upstream's schema requires to_columns to be a non-empty list
    # (minItems: 1), so such a relationship cannot be emitted at all. It is
    # reported unconditionally, at ERROR severity, with its own remedy.
    #
    # For every other disqualification (residual predicates, wrong
    # cardinality), the base "not a declared key" statement is our own,
    # unconditional explanation of why we did not derive a key from this
    # relationship. Upstream's *separate* to_columns coverage check
    # (`validate_references` in `validation/validate.py` -- named rather than
    # cited by line, because the line number this comment used to give had
    # drifted onto an unrelated helper) only warns when a declared key exists
    # for this dataset AND this relationship's columns fail to cover any of it
    # — `declared_keys and not any(set(key) <= to_column_set for
    # key in declared_keys)`. So predicting that warning is only added when
    # that same condition genuinely holds here: a key was derived (`seen`)
    # and none of the derived keys is a subset of this relationship's
    # to_columns. The canonical SCD-2 residual (as-of) join, whose
    # to_columns exactly covers the derived key, passes upstream's check
    # clean — predicting a warning for it would be wrong.
    for rel in inbound:
        if _qualifies(rel):
            continue

        if not rel.to_columns:
            log.add(
                code="TS_KEY_COVERAGE",
                severity=Severity.ERROR,
                message=(
                    f"Relationship {rel.name!r} targets {dataset_name!r} with an empty "
                    f"to_columns. Ossie's schema requires to_columns to be a non-empty "
                    f"list (minItems: 1), so this relationship cannot be emitted as-is."
                ),
                object_ref=f"relationship:{rel.name}",
                remedy=(
                    "Not expected. Populate to_columns with the join columns on the "
                    "'to' dataset, or drop the relationship — an empty to_columns fails "
                    "Ossie schema validation outright; it is not a coverage warning."
                ),
            )
            continue

        if rel.has_residual_predicates:
            reason = "its condition carries residual (non-equality) predicates"
        else:
            reason = f"its cardinality is {rel.cardinality}"

        message = (
            f"Relationship {rel.name!r} targets {dataset_name!r} on columns that are "
            f"not a declared key, because {reason}."
        )
        # EXACT, deliberately -- this predicts what upstream will say, and
        # upstream does not fold: `validate.py` builds `set(to_columns)` and
        # tests `set(key) <= …` on the strings as written. Folding here was
        # backwards; it made the converter fail to foresee a warning upstream
        # does raise, on precisely the case-divergent documents the folding
        # above creates.
        to_column_set = set(rel.to_columns)
        if seen and not any(set(key) <= to_column_set for key in seen):
            message += (
                " Ossie validation will report a to_columns coverage warning for it."
            )

        log.add(
            code="TS_KEY_COVERAGE",
            severity=Severity.WARNING,
            message=message,
            object_ref=f"relationship:{rel.name}",
            remedy=(
                "The relationship is genuinely not a key join; declaring a key to "
                "silence a coverage warning would assert uniqueness that does not hold."
            ),
        )

    # `list(...)`, not `seen[0]`: returning the same list OBJECT in both slots
    # made PyYAML emit a YAML anchor/alias pair -- `primary_key: &id001` with
    # `unique_keys: [*id001]` -- into every generated document, and into both
    # committed fixtures, where the tests pinned it rather than caught it.
    # Aliases are valid YAML but a common hardening default disables them, and a
    # non-PyYAML reader that does not resolve them reads null.
    primary_key = list(seen[0]) if len(seen) == 1 else None
    return primary_key, seen
