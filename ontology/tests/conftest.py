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

"""Shared fixtures for the ontology converter test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from ossie_ontology.model import OssieOntology
from ossie_ontology.parser import OssieParser

# Test inputs are vendored under tests/fixtures/ so the suite runs without the
# apache/ossie examples. specs/flights.yaml and specs/flights.semantic_model.yaml
# are snapshots of apache/ossie's examples/flights*.yaml and are deliberately not
# kept in sync with them: the suite tests against this version even if the
# canonical examples drift.
#
# Sorted by what a file is for, because the suites read them differently:
#   specs/       full ontologies the snapshot suites render end to end
#   behaviours/  one spec per behaviour, asserted directly rather than snapshotted
#   validation/  formulas that must parse, or must fail with a stated message
#
# relationalai/'s suite reads specs/ from here too, so a change to a spec there
# also moves its PyRel snapshots.
_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURES_DIR = _TESTS_DIR / "fixtures"
_SPECS_DIR = _FIXTURES_DIR / "specs"
_BEHAVIOURS_DIR = _FIXTURES_DIR / "behaviours"

# The snapshot files under tests/snapshots/ carry the ASF license header inline,
# so that a source-release audit (Apache RAT) finds a header on every file in the
# tree without needing an exclude list. pytest-snapshot compares the whole file
# byte-for-byte, so the header has to be part of the asserted value; the snapshot
# fixture below applies it, which also means `pytest --snapshot-update` reproduces
# it and it cannot drift.
#
# Both snapshot formats tolerate it: '#' starts a comment in YAML, and the
# structure snapshot is free-form text.
LICENSE_HEADER = """\
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

"""


@pytest.fixture
def snapshot(snapshot):
    """pytest-snapshot's fixture, with the ASF license header applied for free.

    Overrides the plugin fixture so tests assert only their own payload and the
    header stays in one place. Applies on both paths: comparison prepends it to
    the expected value, and --snapshot-update writes it into the file.

    Note: only assert_match is wrapped, since that is all this suite uses. A test
    reaching for assert_match_dir would need the same treatment.
    """
    assert_match = snapshot.assert_match

    def assert_match_with_license(value: str | bytes, snapshot_name: str | Path) -> None:
        __tracebackhide__ = True
        if isinstance(value, bytes):
            assert_match(LICENSE_HEADER.encode() + value, snapshot_name)
        else:
            assert_match(LICENSE_HEADER + value, snapshot_name)

    snapshot.assert_match = assert_match_with_license
    return snapshot


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return _FIXTURES_DIR


@pytest.fixture(scope="session")
def specs_dir() -> Path:
    """Full ontologies: flights, retail, tpch, AutoOntologyTPCH."""
    return _SPECS_DIR


@pytest.fixture(scope="session")
def behaviours_dir() -> Path:
    """One spec per behaviour, each asserted on directly."""
    return _BEHAVIOURS_DIR


@pytest.fixture(scope="session")
def validation_dir() -> Path:
    """Formulas that must parse, or must fail with a stated message."""
    return _FIXTURES_DIR / "validation"


@pytest.fixture(scope="session")
def flights_path(fixtures_dir: Path) -> Path:
    """The reference corpus: read by the spec snapshots, the
    parser round-trip tests, and test_reasoner for its composite reference scheme
    (Runway is identified by designator *and* airport).

    Its note lives here rather than in the file, because the file is a snapshot
    of apache/ossie's examples/flights.yaml -- retail.yaml and tpch.yaml are
    test-only and carry theirs inline.
    """
    return _SPECS_DIR / "flights.yaml"


@pytest.fixture
def flights_model(flights_path: Path) -> OssieOntology:
    return OssieParser().parse(flights_path)

# The corpus the snapshot suites run over. relationalai/tests/conftest.py keeps
# the same list for its PyRel snapshots. `flights` also keeps the named fixtures
# above, since several tests assert on it directly.
SPEC_NAMES = ["flights", "retail", "tpch", "AutoOntologyTPCH"]


@pytest.fixture
def spec_model(request) -> OssieOntology:
    """The parsed ontology for the spec named by an indirect parametrize.

    Built with the default factories, so formulas arrive parsed and validated:
    the grammar lives in `ossie_ontology.expr` and depends only on `ply`.
    """
    return OssieParser().parse(_SPECS_DIR / f"{request.param}.yaml")
