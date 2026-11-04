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

"""Interop with Apache Ossie models this converter did not produce.

Import (Solid -> Apache Ossie) is covered by round-trip equality, because both ends of
that trip are Solid's own format. Export (Apache Ossie -> Solid) has no such anchor: the
models it will really be handed come from *other vendors' converters*, and those look
nothing like the ones this converter emits -- no SOLID stash, no `dimension` blocks,
usually no `datatype`, metrics written against bare column names.

Two complementary passes cover that:

1. A **sweep** over the Ossie fixtures the other converters in this repository ship.
   It asserts only the robust contract -- every model converts, and the result is a
   well-formed Solid model that re-imports schema-valid. It deliberately pins no
   warning counts: those fixtures belong to other converters, and each converter's CI
   is path-filtered to its own directory, so a pin here would break on a main-branch
   push from a PR that never ran this suite.

2. Exact pins against `foreign_ossie.yaml`, a fixture *this* converter owns that
   reproduces the same constructs. That is where the interop gaps are recorded
   precisely, so a change in any of them shows up as a diff in this file.
"""

import json

import pytest
import yaml
from conftest import (
    CONVERTERS,
    convert_quietly,
    fixture,
    model_of,
    solid_model_of,
)

from ossie_solid import convert_ossie_to_solid, convert_solid_to_ossie
from ossie_solid._common import (
    PORTABLE_DIALECTS,
    READABLE_OSSIE_VERSIONS,
    SUPPORTED_DIALECTS,
    split_source,
)
from ossie_solid.expressions import is_query

# Directory names that hold another converter's test data; top-level directories that
# are not another converter (the spec submodule, this one); and build output.
_TEST_DIRS = {"tests", "test"}
_NOT_CONVERTERS = {"ossie", "solid"}
_SKIPPED_DIRS = {".venv", "node_modules", "target", "build"}


def _foreign_ossie_fixtures():
    """Every Apache Ossie document among the other converters' test data.

    Discovered rather than listed, so a fixture that is renamed or removed upstream
    drops out of the sweep instead of failing it. YAML and JSON both count, at any depth
    under a converter's test directory (`tests/fixtures/...`, `src/test/resources/...`).
    """
    found = []
    for path in sorted(CONVERTERS.glob("*/**/*")):
        parts = path.relative_to(CONVERTERS).parts
        if (path.suffix not in (".yaml", ".yml", ".json") or not path.is_file()
                or parts[0] in _NOT_CONVERTERS or _SKIPPED_DIRS & set(parts)
                or not _TEST_DIRS & set(parts)):
            continue
        try:
            document = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        # An Apache Ossie document declares a spec version and carries its model's
        # `datasets` at the root; Solid's and every other vendor's native format does
        # not.
        if (
            isinstance(document, dict)
            and document.get("version") in READABLE_OSSIE_VERSIONS
            and "datasets" in document
        ):
            found.append(path)
    return found


FOREIGN = _foreign_ossie_fixtures()


def _fixture_id(path):
    return f"{path.relative_to(CONVERTERS).parts[0]}/{path.name}"


def _is_non_sql(obj):
    """True if a field or metric is written only in an expression language that is not
    SQL (HOLISTICS_AQL, THOUGHTSPOT, ...), which export drops by design rather than
    handing Solid a formula it cannot run."""
    expression = obj.get("expression") or {}
    if expression.get("sql"):
        return False
    readable = set(SUPPORTED_DIALECTS) | set(PORTABLE_DIALECTS)
    return not {e.get("dialect") for e in expression.get("dialects") or []} & readable


def _only_non_sql(path):
    """True if some field or metric in the document is written only in a non-SQL
    expression language (see _is_non_sql)."""
    document = yaml.safe_load(path.read_text())
    objects = [f for d in document["datasets"] for f in d.get("fields") or []]
    objects += document.get("metrics") or []
    objects += [m for d in document["datasets"] for m in d.get("metrics") or []]
    return any(_is_non_sql(o) for o in objects)


# Every one converts: a field or metric only in a non-SQL language is dropped with a
# warning, and the rest of the model still reaches Solid.
CONVERTIBLE = FOREIGN
NON_SQL = [p for p in FOREIGN if _only_non_sql(p)]


@pytest.mark.skipif(not FOREIGN, reason="no other converters ship Ossie fixtures")
@pytest.mark.parametrize("path", CONVERTIBLE, ids=_fixture_id)
def test_another_vendors_model_converts_to_a_well_formed_solid_model(path):
    """The bar is a Solid model Solid could actually read, not a warning-free one.

    Losses are expected here and are reported as warnings; what must not happen is a
    hard failure or a structurally invalid Solid document.
    """
    solid, _ = convert_quietly(convert_ossie_to_solid, path.read_text())
    model = solid_model_of(solid)

    assert model["name"]
    assert model["tables"], "a Solid model needs at least one table"
    named = {table["name"] for table in model["tables"]}
    for table in model["tables"]:
        assert table["name"], "every Solid table needs its fully-qualified name"
        # A query-backed dataset (Cube writes some) has no Solid table to name.
        assert not is_query(table["name"], None), table["name"]
        # Solid always emits `facts`, as [] when a table has none.
        assert "facts" in table
        for column in (table.get("dimensions") or []) + table["facts"]:
            assert column["name"]
    for metric in model.get("metrics") or []:
        assert metric["name"] and metric["expression"]
        assert set(metric.get("tables") or []) <= named, metric
    for relationship in model.get("relationships") or []:
        assert {relationship["left_table"], relationship["right_table"]} <= named
        keys = relationship["join_keys"]
        assert len(keys["left"]) == len(keys["right"])


@pytest.mark.skipif(not NON_SQL, reason="no other converter ships a non-SQL expression")
@pytest.mark.parametrize("path", NON_SQL, ids=_fixture_id)
def test_a_formula_only_in_a_non_sql_language_is_dropped_with_a_warning(path):
    """As documented: a formula Solid cannot run is not passed into a SQL field. The
    field or metric is dropped with a warning; the rest of the model converts."""
    solid, warnings_ = convert_quietly(convert_ossie_to_solid, path.read_text())
    assert any("which is SQL Solid can run, so it was dropped" in w for w in warnings_)
    assert solid_model_of(solid)["tables"]


@pytest.mark.skipif(not FOREIGN, reason="no other converters ship Ossie fixtures")
@pytest.mark.parametrize("path", CONVERTIBLE, ids=_fixture_id)
def test_reimporting_another_vendors_model_is_schema_valid(path, assert_valid_ossie):
    """The full hop: their Apache Ossie -> Solid -> Apache Ossie, still valid."""
    solid, _ = convert_quietly(convert_ossie_to_solid, path.read_text())
    ossie, _ = convert_quietly(convert_solid_to_ossie, solid, dialect="ANSI_SQL")
    assert_valid_ossie(ossie)


NUMERIC_DATATYPES = {"Integer", "Decimal", "Float"}


def _split_expectations(path):
    """(field name, expected Solid list, actual Solid list) for each field whose
    markings settle its role, in every dataset of another vendor's model.

    The expectation is stated in terms of what the other converter wrote, so it holds
    whatever its fixtures contain: a field with a `dimension` block is a dimension, and
    an unmarked one with a numeric datatype is a fact, with any other datatype a
    dimension. An unmarked, untyped field follows its producer's convention instead;
    that case is pinned in test_ossie_to_solid.py.
    """
    document = yaml.safe_load(path.read_text())
    solid, _ = convert_quietly(convert_ossie_to_solid, path.read_text())
    found = []
    tables = {table["name"].lower(): table for table in solid_model_of(solid)["tables"]}
    for dataset in document["datasets"]:
        # By name, not position: a dataset Solid cannot hold is dropped, and two over
        # one table are merged.
        table = tables.get(".".join(split_source(dataset["source"])).lower())
        if table is None:
            continue
        # A field that is an alias for another column becomes that column, with the
        # field's name as a synonym.
        dimensions = {name for c in table.get("dimensions") or []
                      for name in [c["name"], *(c.get("synonyms") or [])]}
        for field in dataset.get("fields") or []:
            if _is_non_sql(field):
                continue  # dropped, with a warning, before any role applies
            if field.get("dimension") is not None:
                expected = "dimensions"
            elif field.get("datatype") in NUMERIC_DATATYPES:
                expected = "facts"
            elif field.get("datatype"):
                expected = "dimensions"
            else:
                continue
            actual = "dimensions" if field["name"] in dimensions else "facts"
            found.append((f"{dataset['name']}.{field['name']}", expected, actual))
    return found


@pytest.mark.skipif(not FOREIGN, reason="no other converters ship Ossie fixtures")
@pytest.mark.parametrize("path", CONVERTIBLE, ids=_fixture_id)
def test_another_vendors_fields_land_where_their_markings_put_them(path):
    """The fact/dimension split, not just a well-formed result."""
    wrong = [f"{name}: expected in {expected}, got {actual}"
             for name, expected, actual in _split_expectations(path)
             if expected != actual]
    assert not wrong, "\n".join(wrong)


@pytest.mark.skipif(not FOREIGN, reason="no other converters ship Ossie fixtures")
def test_the_split_check_actually_covers_fields():
    """Guard against the split check passing because no fixture marks anything."""
    checked = sum(len(_split_expectations(path)) for path in CONVERTIBLE)
    assert checked >= 50, f"only {checked} fields carried a block or a datatype"


@pytest.mark.skipif(not FOREIGN, reason="no other converters ship Ossie fixtures")
def test_the_sweep_actually_found_the_other_converters():
    """Guard against the discovery silently matching nothing and the sweep passing.

    A lower bound, not an exact count, so a converter added or removed upstream does
    not fail this suite.
    """
    vendors = {path.parts[-4] for path in FOREIGN}
    assert len(vendors) >= 3, f"only found fixtures for {vendors}"


# --- exact pins, against a fixture this converter owns -----------------------------


@pytest.fixture(scope="module")
def foreign():
    solid, warnings_ = convert_quietly(
        convert_ossie_to_solid, fixture("foreign_ossie.yaml")
    )
    return solid_model_of(solid), warnings_


def test_a_foreign_model_converts_to_the_expected_solid_shape(foreign):
    model, _ = foreign
    assert model["name"] == "foreign_retail_model"
    assert [t["name"] for t in model["tables"]] == [
        "tpcds.public.store_sales",
        "tpcds.public.date_dim",
    ]
    # No `dimension` block anywhere, so the split falls back to Solid's type rule:
    # a numeric type is a fact, everything else a dimension. With no datatype at all,
    # a column cannot be shown to be numeric and so lands in `dimensions`.
    store_sales = model["tables"][0]
    assert [c["name"] for c in store_sales["facts"]] == ["ss_net_paid"]
    # `ticket_number` is an alias for `ss_ticket_number`, which is already a column.
    assert [c["name"] for c in store_sales["dimensions"]] == [
        "ss_item_sk",
        "ss_ticket_number",
        "ss_sold_date_sk",
    ]
    assert store_sales["primary_key"] == "ss_item_sk, ss_ticket_number"


def test_the_documented_interop_gaps_are_exactly_these(foreign):
    """The record of what is lost bringing another vendor's model into Solid.

    Each line is a known gap, not a defect to be fixed here. Widening or narrowing any
    of them should be a visible diff in this test.
    """
    _, warnings_ = foreign
    counts = {
        # No `datatype` -> no Solid `type`. The single largest gap: Solid's columns are
        # typed from the warehouse catalog, and an offline converter has no catalog.
        "has no datatype": 4,
        # A field renamed relative to its column is that column, which another field
        # already exports, so Solid lists it once.
        "which another field already exports": 1,
        # Solid has no slot for any of these.
        "label 'Year' has no Solid equivalent": 1,
        "unique_keys have no Solid equivalent": 1,
        "datatype 'Decimal' has no Solid equivalent": 1,
        "ai_context.instructions, ai_context.synonyms have no Solid equivalent": 1,
        "custom_extensions for": 4,
        # Solid resolves a metric's columns through `tables`, and a metric written
        # against bare column names carries nothing to resolve them from.
        "names no table": 2,
    }
    for text, expected in counts.items():
        assert sum(text in w for w in warnings_) == expected, text
    assert len(warnings_) == sum(counts.values()) == 15


def test_a_renamed_field_names_the_column_it_reads(foreign):
    """`ticket_number` reads `ss_ticket_number`. A Solid column is a catalog column, so
    the output names the column the warehouse has, never the alias, and lists it once
    since another field already is that column."""
    model, warnings_ = foreign
    names = [c["name"] for c in model["tables"][0]["dimensions"]]
    assert "ticket_number" not in names
    assert names.count("ss_ticket_number") == 1
    assert any("'store_sales.ticket_number' reads the column ss_ticket_number, which "
               "another field already exports" in w for w in warnings_)


def test_a_bare_column_metric_reaches_solid_with_no_tables(foreign):
    """Also pinned as a gap: Solid needs `tables` to resolve a formula's columns.

    Only a dataset-qualified reference identifies its owner, and another vendor has no
    reason to qualify -- Apache Ossie's own TPC-DS example does, which is why this does
    not show up against it.
    """
    model, _ = foreign
    assert {m["name"]: m["tables"] for m in model["metrics"]} == {
        "revenue": [],
        "order_count": [],
    }


def test_a_foreign_model_survives_the_round_trip_back_to_ossie(assert_valid_ossie):
    solid, _ = convert_quietly(convert_ossie_to_solid, fixture("foreign_ossie.yaml"))
    ossie, _ = convert_quietly(convert_solid_to_ossie, solid, dialect="ANSI_SQL")
    assert_valid_ossie(ossie)

    original = model_of(fixture("foreign_ossie.yaml"))
    restored = model_of(ossie)
    assert restored["name"] == original["name"]
    assert [d["source"] for d in restored["datasets"]] == [
        d["source"] for d in original["datasets"]
    ]
    # The relationship survives with its direction intact: `store_sales` joins
    # `date_dim` on the latter's primary key, so it is the many side.
    edge = restored["relationships"][0]
    assert (edge["from"], edge["to"]) == ("store_sales", "date_dim")


def test_the_dialect_recorded_on_a_reimported_foreign_model_is_explicit():
    """A foreign model has no stash, so the re-import records what it resolved to."""
    solid, _ = convert_quietly(convert_ossie_to_solid, fixture("foreign_ossie.yaml"))
    ossie, _ = convert_quietly(convert_solid_to_ossie, solid, dialect="ANSI_SQL")
    stash = next(
        json.loads(ext["data"])
        for ext in model_of(ossie)["custom_extensions"]
        if ext["vendor_name"] == "SOLID"
    )
    assert stash["dialect"] == "ANSI_SQL"
