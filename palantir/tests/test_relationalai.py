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

"""The full spoke-to-spoke path: Palantir -> Ossie -> RelationalAI.

It lives here rather than in ontology/ because this package depends on
apache-ossie-ontology, not the other way round. The module is skipped unless the
optional `relationalai` extra is installed:

    uv sync --extra relationalai

Nothing talks to an engine: conftest pins `RAI_CONFIG_FILE_PATH` to an offline
config, and `declared_table` takes column types from the Ossie model rather than
from a warehouse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ossie_palantir import PalantirParser, PalantirToOssieConverter

pyrel = pytest.importorskip(
    "relationalai.semantics",
    reason="needs the optional relationalai extra: uv sync --extra relationalai",
)

from relationalai.semantics.metamodel.pyrel_codegen import to_pyrel  # noqa: E402
from relationalai.semantics.metamodel.typer import Typer  # noqa: E402

from ossie_ontology.converter.ossie_to_relationalai import (  # noqa: E402
    OntologyModel,
    OssieToRelationalAIConverter,
    declared_table,
)

# An extracted Palantir export on disk, the same shape the real exports have:
# an ontology JSON plus a data_sets/ folder.
PALANTIR_SPEC = "retail_mini"


def test_palantir_export_converts_all_the_way_to_pyrel(snapshot, fixtures_dir: Path):
    """Palantir JSON in, executable PyRel out, through the Ossie hub.

    Neither converter alone proves this works: `palantir_to_ossie` stops at the
    Ossie model and `ossie_to_relationalai` starts from one. This is the only
    test that runs a vendor format through the hub and out the other side, which
    is the point of the hub-and-spoke arrangement.
    """
    ontology = PalantirToOssieConverter().convert(
        PalantirParser().parse(fixtures_dir / PALANTIR_SPEC)
    )

    pyrel.Model.all_models.clear()
    om = OssieToRelationalAIConverter.convert(ontology, table_provider=declared_table)

    assert isinstance(om, OntologyModel)
    # Both Palantir object types survived the round trip as concepts.
    assert om.lookup_concept("Store") is not None
    assert om.lookup_concept("Sale") is not None

    source = to_pyrel(om.base_model().to_metamodel())
    snapshot.assert_match(source, "palantir_chain.py")

    namespace: dict = {}
    pyrel.Model.all_models.clear()
    exec(compile(source, "generated_palantir_chain.py", "exec"), namespace)
    Typer().infer_model(namespace["m"].to_metamodel())
