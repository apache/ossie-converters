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

"""The round-trip guarantee, over models no one wrote by hand.

The fixtures and real exports cover the shapes someone thought of. This generates Solid
exports the way solid-server's template writes them -- every warehouse's type
vocabulary, mixed-case and keyword column names, composite keys, keys and joins on
columns a table does not list, single- and cross-table metrics, table-scoped metric
facts, and keys a later solid-server might add -- and checks the README's guarantees on
each: Solid -> Apache Ossie -> Solid gives the same model back, and every Apache Ossie
document import writes is schema-valid with each key and join column a declared field.
Each is also exported again without its SOLID stash, as a model built in Apache Ossie
would be, and checked against what Solid's own YAML import requires.

Seeded, so a failure names the model that produced it and reruns the same.
"""

import random

import pytest
import yaml
from conftest import convert_quietly, model_of, normalized_yaml

from ossie_solid import convert_ossie_to_solid, convert_solid_to_ossie
from ossie_solid.datatypes import source_system_for

MODELS = 300

_TYPES = {
    "snowflake": ["NUMBER", "TEXT", "BOOLEAN", "DATE", "TIMESTAMP_NTZ", "TIMESTAMP_TZ",
                  "FLOAT", "VARIANT", "ARRAY"],
    "databricks": ["STRING", "LONG", "INT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP",
                   "DECIMAL", "MAP"],
    "big_query": ["STRING", "INT64", "FLOAT64", "BOOL", "DATE", "DATETIME", "TIMESTAMP",
                  "NUMERIC"],
    "starburst": ["varchar", "bigint", "double", "boolean", "date", "timestamp(3)",
                  "decimal(18,2)"],
}
_NUMERIC = ("NUMBER", "DOUBLE", "FLOAT", "INT", "LONG", "DECIMAL", "NUMERIC", "BIGINT")
# Ordinary names next to SQL keywords (`order`, `date`, `user`, `value`), which have to
# survive quoting both ways.
_COLUMNS = ["id", "order_id", "customer_id", "amount", "status", "region", "created_at",
            "name", "order", "date", "value", "year", "type", "level", "user", "total",
            "price", "quantity", "country", "email", "segment", "is_active"]
_WORDS = ["Revenue", "orders", "customer", "net", "per", "account", "rate", "—", "é"]


def _text(r, words=6):
    return " ".join(r.choice(_WORDS) for _ in range(r.randint(1, words)))


def _model(seed):
    r = random.Random(seed)
    system = r.choice([*_TYPES, None])
    upper = system == "snowflake"

    def name(text):
        return text.upper() if upper else text

    tables = []
    for t in range(r.randint(1, 4)):
        columns = r.sample(_COLUMNS, r.randint(2, 7))
        table = {"name": name(f"db.sch.t{t}_{r.choice(['orders', 'users', 'events'])}")}
        if r.random() < .7:
            table["description"] = _text(r) + "\n"
        if r.random() < .3:
            table["manual_description"] = _text(r) + "\n"
        if r.random() < .3:
            table["synonyms"] = [_text(r, 2)]
        if r.random() < .6:
            key = r.sample(columns, r.randint(1, 2))
            if r.random() < .15:
                key = [f"{t}_unlisted_key"]   # a key on a column the table does not list
            table["primary_key"] = ", ".join(name(c) for c in key)
        table["quality_rank"] = r.choice(["", "high", "medium"])
        if r.random() < .2:
            table["indexes"] = [name(r.choice(columns))]
        if r.random() < .1:
            table["certified"] = True        # a key a later solid-server might add
        dimensions, facts = [], []
        for c in columns:
            column = {"name": name(c), "type": r.choice(_TYPES[system or "snowflake"])}
            if r.random() < .5:
                column["description"] = _text(r) + "\n"
            if r.random() < .2:
                column["synonyms"] = [_text(r, 2)]
            if r.random() < .3:
                column["sample_values"] = [str(r.randint(0, 99)) for _ in range(3)]
            numeric = column["type"].upper().startswith(_NUMERIC)
            (facts if numeric else dimensions).append(column)
        if r.random() < .2:
            facts.append({"name": name(f"metric_fact_{t}"),
                          "expression": f"SUM({name(columns[0])})"})
        if dimensions:
            table["dimensions"] = dimensions
        table["facts"] = facts
        tables.append(table)

    def listed(table):
        return [c["name"] for c in (table.get("dimensions") or []) + table["facts"]
                if "expression" not in c]

    relationships = []
    for _ in range(r.randint(0, len(tables))):
        left, right = r.choice(tables), r.choice(tables)
        left_key = r.choice(listed(left)) if r.random() < .9 else "unlisted_fk"
        relationships.append({"left_table": left["name"], "right_table": right["name"],
                              "join_keys": {"left": [left_key],
                                            "right": [r.choice(listed(right))]}})
    metrics = []
    for m in range(r.randint(0, 4)):
        bound = r.sample(tables, r.randint(1, min(2, len(tables))))
        first, last = r.choice(listed(bound[0])), r.choice(listed(bound[-1]))
        metric = {"name": name(f"metric_{m}")}
        if r.random() < .6:
            metric["description"] = _text(r) + "\n"
        metric["expression"] = r.choice([
            f"SUM({first})", f"COUNT(DISTINCT {first})",
            f"SUM({first}) / NULLIF(COUNT({last}), 0)",
            f"COUNT(CASE WHEN {first} IS NOT NULL THEN 1 END)", "COUNT(*)"])
        if r.random() < .3:
            metric["synonyms"] = [_text(r, 2)]
        metric["tables"] = [t["name"] for t in bound]
        metrics.append(metric)

    model = {"name": f"model_{seed}"}
    if system and r.random() < .7:
        model.update(version_number=r.randint(0, 5), source_system=system,
                     target_system="solid")
    business = {}
    if r.random() < .5:
        business["business_questions"] = [_text(r) + "?"]
    if r.random() < .5:
        business["model_description"] = _text(r) + "\n"
    model["business_context"] = business
    if r.random() < .6:
        model["model_llm_description"] = _text(r) + "\n"
    if r.random() < .3:
        model["model_llm_short_description"] = _text(r) + "\n"
    model["tables"] = tables
    if metrics:
        model["metrics"] = metrics
    if relationships:
        model["relationships"] = relationships
    if r.random() < .3:
        model["example_queries"] = [{"question": _text(r) + "?", "sql": "SELECT 1"}]
    return yaml.safe_dump({"semantic_model": model}, sort_keys=False, allow_unicode=True)


def _unresolved_keys(ossie):
    """Key and join columns that are not declared fields of their dataset, ignoring case,
    which the spec's validator rejects (apache/ossie#649)."""
    datasets = {d["name"]: {f["name"].casefold() for f in d.get("fields") or []}
                for d in ossie["datasets"]}
    bad = [f"{d['name']}.{k}" for d in ossie["datasets"]
           for k in d.get("primary_key") or [] if k.casefold() not in datasets[d["name"]]]
    for r in ossie.get("relationships") or []:
        for end, columns in ((r["from"], r["from_columns"]), (r["to"], r["to_columns"])):
            bad += [f"{end}.{c}" for c in columns if c.casefold() not in datasets[end]]
    return bad


@pytest.mark.parametrize("seed", range(MODELS))
def test_a_generated_solid_export_round_trips_and_imports_valid(seed, assert_valid_ossie):
    solid = _model(seed)
    ossie, _ = convert_quietly(convert_solid_to_ossie, solid)
    assert_valid_ossie(ossie)
    assert _unresolved_keys(model_of(ossie)) == []
    back, _ = convert_quietly(convert_ossie_to_solid, ossie)
    assert normalized_yaml(back) == normalized_yaml(solid)


def _without_stash(node):
    """`node` with every SOLID custom_extensions entry removed, recursively."""
    if isinstance(node, dict):
        return {k: _without_stash(v) for k, v in node.items()
                if not (k == "custom_extensions" and all(
                    e.get("vendor_name") == "SOLID" for e in v))}
    if isinstance(node, list):
        return [_without_stash(v) for v in node]
    return node


def _catalog(solid):
    """The warehouse columns a Solid export shows exist, per table (casefolded): every
    column a table lists, and every key and join column, listed or not."""
    model = yaml.safe_load(solid)["semantic_model"]
    catalog = {t["name"].casefold(): {c["name"].casefold()
                                      for c in (t.get("dimensions") or []) + t["facts"]
                                      if "expression" not in c}
               for t in model["tables"]}
    for t in model["tables"]:
        catalog[t["name"].casefold()] |= {
            k.strip().casefold() for k in (t.get("primary_key") or "").split(",")}
    for r in model.get("relationships") or []:
        for end in ("left", "right"):
            catalog[r[f"{end}_table"].casefold()] |= {
                c.casefold() for c in r["join_keys"][end]}
    return catalog


def _solid_import_rejects(model, catalog):
    """What solid-server's YAML import (data_server/.../yaml_import) would reject this
    Solid model for: a missing `source_system`, or a table, column, key column, join or
    metric table the warehouse catalog does not have. Its resolver matches names
    ignoring case, and rejects the whole model on any one of these."""
    problems = [] if model.get("source_system") else ["no source_system"]
    for t in model["tables"]:
        columns = catalog.get(t["name"].casefold())
        if columns is None:
            problems.append(f"table {t['name']}")
            continue
        problems += [f"column {t['name']}.{c['name']}"
                     for c in (t.get("dimensions") or []) + t["facts"]
                     if "expression" not in c and c["name"].casefold() not in columns]
        problems += [f"primary key {t['name']}.{k}"
                     for k in (t.get("primary_key") or "").split(", ")
                     if k and k.casefold() not in columns]
    for r in model.get("relationships") or []:
        left, right = r["join_keys"]["left"], r["join_keys"]["right"]
        if not left or len(left) != len(right):
            problems.append(f"join keys {r}")
        for end, keys in (("left", left), ("right", right)):
            columns = catalog.get(r[f"{end}_table"].casefold(), set())
            problems += [f"join column {r[f'{end}_table']}.{k}" for k in keys
                         if k.casefold() not in columns]
    for m in model.get("metrics") or []:
        if not m.get("expression"):
            problems.append(f"metric {m['name']}")
        problems += [f"metric {m['name']} table {t}" for t in m["tables"]
                     if t.casefold() not in catalog]
    return problems


@pytest.mark.parametrize("seed", range(MODELS))
def test_a_generated_model_without_its_stash_exports_what_solid_can_import(seed):
    """As if the model were built in Apache Ossie: no stash restores what Solid wrote,
    so this is the export a model from any other tool gets."""
    solid = _model(seed)
    ossie, _ = convert_quietly(convert_solid_to_ossie, solid)
    stripped = _without_stash(yaml.safe_load(ossie))
    dialect = stripped["datasets"][0]["fields"][0]["expression"]["dialects"][0]["dialect"]
    back, warnings_ = convert_quietly(convert_ossie_to_solid, yaml.safe_dump(stripped))
    model = yaml.safe_load(back)["semantic_model"]
    problems = _solid_import_rejects(model, _catalog(solid))
    if source_system_for(dialect) is None:
        # Portable SQL names no warehouse; the export says so instead of guessing.
        assert problems == ["no source_system"]
        assert any("names no warehouse, so 'source_system' was left out" in w
                   for w in warnings_)
    else:
        assert problems == []
        assert model["source_system"] == source_system_for(dialect)
