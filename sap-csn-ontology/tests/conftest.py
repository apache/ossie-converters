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

"""Shared fixtures for the SAP CSN converter tests."""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import pytest

# Make the converter modules in ../src importable from the tests.
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from ossie_sap_csn_ontology import convert, load_csn, load_curation, render  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

#: The repository root. The spec schemas come from the `ossie` git submodule
#: pinned there (see CONTRIBUTING.md), not from a sibling checkout.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OSSIE_SUBMODULE = REPO_ROOT / "ossie"


@pytest.fixture(scope="session")
def slice_conversion():
    """The Business Partner slice, converted with its curation overlay."""
    return convert(
        load_csn(FIXTURES / "sap-bp-slice.csn.json"),
        load_curation(FIXTURES / "sap-bp-slice.curation.yaml"),
    )


@pytest.fixture(scope="session")
def generated(slice_conversion) -> dict[str, Any]:
    """The ontology document generated from the CSN fixture."""
    return render(slice_conversion)


@pytest.fixture(scope="session")
def bare_conversion():
    """The same CSN converted with no curation overlay at all."""
    return convert(load_csn(FIXTURES / "sap-bp-slice.csn.json"))
