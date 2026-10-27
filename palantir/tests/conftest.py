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

"""Shared fixtures for the Palantir converter test suite.

Mirrors the harness in ontology/tests/conftest.py for the one test here that
runs the RelationalAI converter (tests/test_relationalai.py): snapshots carry
the ASF license header, `relationalai` is pinned to an offline config, and no
test may open a network connection.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent

# Snapshot files carry the ASF license header inline so Apache RAT finds one on
# every file. pytest-snapshot compares the whole file, so the header has to be
# part of the asserted value; the fixture below applies it on both comparison
# and --snapshot-update.
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
    """pytest-snapshot's fixture, with the ASF license header applied."""
    assert_match = snapshot.assert_match

    def assert_match_with_license(value: str | bytes, snapshot_name: str | Path) -> None:
        __tracebackhide__ = True
        if isinstance(value, bytes):
            assert_match(LICENSE_HEADER.encode() + value, snapshot_name)
        else:
            assert_match(LICENSE_HEADER + value, snapshot_name)

    snapshot.assert_match = assert_match_with_license
    return snapshot


# `relationalai` resolves its config the first time a Model is constructed and
# would otherwise pick up a contributor's real, credentialed one. Pin it to the
# offline fixture before any test can build a Model.
os.environ["RAI_CONFIG_FILE_PATH"] = str(_TESTS_DIR / "config" / "raiconfig.yaml")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Palantir exports, in Palantir's own layout."""
    return _TESTS_DIR / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse every socket connection: the suite runs offline."""
    import socket

    def deny(*args, **kwargs):
        raise RuntimeError(
            "This suite runs offline: a test tried to open a network connection."
        )

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
