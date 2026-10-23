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

"""Input that is valid YAML but the wrong shape fails cleanly.

The README promises a ConversionError for any input the converter cannot read, and the
CLI only reports those (and OSError) as clean errors -- anything else reaches the user as
a traceback. Rather than test the handful of places someone thought to check, this
sweeps the contract: every value in a real document is replaced, one at a time, with a
value of each other YAML kind, and each result must either convert or raise a
ConversionError. Any other exception is a bug.
"""

import copy
import datetime
import json
import warnings

import pytest
import yaml
from conftest import fixture

from ossie_solid import convert_ossie_to_solid, convert_solid_to_ossie
from ossie_solid._common import ConversionError

# One value of every kind `yaml.safe_load` produces, plus the containers in shapes that
# are easy to mistake for the expected one.
REPLACEMENTS = {
    "string": "x",
    "integer": 7,
    "float": 1.5,
    "boolean": True,
    "null": None,
    "date": datetime.date(2024, 1, 1),
    "datetime": datetime.datetime(2024, 1, 1, 12, 30),
    "list of strings": ["a"],
    "list of mappings": [{"a": 1}],
    "list of dates": [datetime.date(2024, 1, 1)],
    "empty list": [],
    "mapping": {"k": "v"},
    "empty mapping": {},
}


def _paths(node, prefix=()):
    """Every path into `node`, visiting only the first item of each list.

    Items of one list share a shape, so the first stands for the rest and keeps the
    sweep small enough to run on every push.
    """
    if prefix:
        yield prefix
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, prefix + (key,))
    elif isinstance(node, list) and node:
        yield from _paths(node[0], prefix + (0,))


# The same kinds, as JSON can hold them, for values inside a stash, plus the shapes a
# tagged date takes when it is malformed.
STASH_REPLACEMENTS = {
    kind: value for kind, value in REPLACEMENTS.items()
    if not isinstance(value, datetime.date) and kind != "list of dates"
} | {
    "tagged date with a non-string value": {"__ossie_solid__": "date", "v": 5},
    "tagged date that is not a date": {"__ossie_solid__": "date", "v": "soon"},
    "tag that is not a string": {"__ossie_solid__": [], "v": "2024-01-01"},
}


def _at(document, path):
    for key in path:
        document = document[key]
    return document


def _replaced(document, path, value):
    mutated = copy.deepcopy(document)
    target = mutated
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = copy.deepcopy(value)
    return mutated


def _mutations(document):
    """(label, mutated document) for every value replaced by every other kind."""
    for path in _paths(document):
        for kind, value in REPLACEMENTS.items():
            yield f"{'.'.join(map(str, path))} := {kind}", _replaced(document, path, value)


def _stash_mutations(document):
    """The same, inside each SOLID stash: its `data` is a JSON string, which the plain
    sweep only ever replaces whole."""
    for path in _paths(document):
        if path[-1] != "custom_extensions" or not isinstance(_at(document, path), list):
            continue
        for index, ext in enumerate(_at(document, path)):
            if not (isinstance(ext, dict) and ext.get("vendor_name") == "SOLID"):
                continue
            data = json.loads(ext["data"])
            for inner in _paths(data):
                for kind, value in STASH_REPLACEMENTS.items():
                    mutated = copy.deepcopy(document)
                    _at(mutated, path)[index]["data"] = json.dumps(
                        _replaced(data, inner, value))
                    yield (f"{'.'.join(map(str, path))}[{index}].data:"
                           f"{'.'.join(map(str, inner))} := {kind}"), mutated


def _sweep(convert, mutations):
    failures = []
    for label, mutated in mutations:
        text = yaml.safe_dump(mutated, sort_keys=False)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                convert(text)
        except ConversionError:
            pass
        except Exception as e:  # noqa: BLE001 - any other exception is the bug
            failures.append(f"{label}: {type(e).__name__}: {e}")
    return failures


@pytest.fixture(scope="module")
def solid_document():
    return yaml.safe_load(fixture("databricks_solid.yaml"))


@pytest.fixture(scope="module")
def ossie_document(solid_document):
    # A model this converter produced, so the sweep also reaches every stash.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return yaml.safe_load(
            convert_solid_to_ossie(yaml.safe_dump(solid_document, sort_keys=False)))


def test_any_malformed_solid_model_imports_or_raises_a_conversion_error(solid_document):
    failures = _sweep(convert_solid_to_ossie, _mutations(solid_document))
    assert not failures, "\n".join(failures)


def test_any_malformed_ossie_model_exports_or_raises_a_conversion_error(ossie_document):
    failures = _sweep(convert_ossie_to_solid, _mutations(ossie_document))
    assert not failures, "\n".join(failures)


def test_any_malformed_stash_exports_or_raises_a_conversion_error(ossie_document):
    """A hand-edited stash: valid JSON, with any value inside it of the wrong kind."""
    mutations = list(_stash_mutations(ossie_document))
    assert len(mutations) > 100, "the stash sweep found too few stashes to mutate"
    failures = _sweep(convert_ossie_to_solid, mutations)
    assert not failures, "\n".join(failures)


def test_the_sweep_reaches_every_part_of_both_documents(solid_document, ossie_document):
    """Guard against the sweep silently covering less than it claims."""
    solid_paths = {path[:4] for path in _paths(solid_document)}
    ossie_paths = {path[:3] for path in _paths(ossie_document)}
    for expected in [
        ("semantic_model", "tables", 0, "dimensions"),
        ("semantic_model", "tables", 0, "facts"),
        ("semantic_model", "relationships", 0, "join_keys"),
        ("semantic_model", "metrics", 0, "tables"),
    ]:
        assert expected in solid_paths, expected
    for expected in [
        ("datasets", 0, "fields"),
        ("datasets", 0, "custom_extensions"),
        ("relationships", 0, "from_columns"),
        ("metrics", 0, "expression"),
        ("custom_extensions", 0, "data"),
    ]:
        assert expected in ossie_paths, expected


# --- YAML dates -----------------------------------------------------------------------


UNQUOTED_DATES = """
semantic_model:
  name: orders
  business_context:
    business_questions: ['How many orders?']
  tables:
    - name: shop.sales.orders
      quality_rank: high
      dimensions:
        - name: order_date
          type: DATE
          sample_values: [2024-01-01, 2024-02-01]
      facts: []
  example_queries:
    - name: january
      sql: SELECT 1
      as_of: 2024-01-31
  benchmark_questions:
    - question: How many orders?
      is_enabled: true
      reviewed_at: 2024-01-31 09:30:00
"""


def test_unquoted_yaml_dates_survive_the_round_trip_as_dates():
    """Solid's own export quotes every value, but a hand-edited file may not. Dates are
    tagged in the stash so they come back as dates and re-dump unquoted."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ossie = convert_solid_to_ossie(UNQUOTED_DATES)
        solid = convert_ossie_to_solid(ossie)
    assert yaml.safe_load(solid) == yaml.safe_load(UNQUOTED_DATES)
    restored = yaml.safe_load(solid)["semantic_model"]
    assert restored["tables"][0]["dimensions"][0]["sample_values"][0] == datetime.date(
        2024, 1, 1)
    assert isinstance(restored["benchmark_questions"][0]["reviewed_at"],
                      datetime.datetime)
