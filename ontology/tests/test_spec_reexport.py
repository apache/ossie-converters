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
"""Tests for `ossie_ontology.spec`, the deprecated re-export of the old DTO
names under their `ossie.models` replacements."""

from __future__ import annotations

import warnings

import ossie.models
import pytest

import ossie_ontology.spec as spec

# Every name the old module defined that has a shared replacement.
_RENAMED = [
    ("OssieSpec", "OssieOntologyModel"),
    ("ConceptComponent", "OssieConceptComponent"),
    ("Relationship", "OssieOntologyRelationship"),
    ("Role", "OssieRole"),
    ("OntologyMapping", "OssieOntologyMapping"),
    ("ConceptMapping", "OssieConceptMapping"),
    ("ObjectMapping", "OssieObjectMapping"),
    ("ReferentMapping", "OssieReferentMapping"),
    ("LinkMapping", "OssieLinkMapping"),
    ("SemanticModel", "OssieSemanticModel"),
    ("Dataset", "OssieDataset"),
    ("DatasetField", "OssieField"),
    ("Metric", "OssieMetric"),
    ("Dimension", "OssieDimension"),
    ("JoinPath", "OssieRelationship"),
    ("Expression", "OssieExpression"),
    ("DialectExpression", "OssieDialectExpression"),
    ("CustomExtension", "OssieCustomExtension"),
    ("AiContext", "OssieAIContext"),
    ("DataType", "OssieDataType"),
    ("CoreVersion", "OssieVersion"),
]


@pytest.mark.parametrize(("old_name", "new_name"), _RENAMED)
def test_old_name_is_the_shared_model_and_warns_with_its_replacement(old_name, new_name):
    with pytest.warns(DeprecationWarning) as record:
        value = getattr(spec, old_name)

    assert value is getattr(ossie.models, new_name)
    assert str(record[0].message) == (
        f"ossie_ontology.spec.{old_name} is deprecated; use ossie.models.{new_name} instead."
    )


def test_from_import_warns_at_the_importing_line():
    with pytest.warns(DeprecationWarning, match="ossie.models.OssieOntologyModel") as record:
        from ossie_ontology.spec import OssieSpec

    assert OssieSpec is ossie.models.OssieOntologyModel
    assert record[0].filename == __file__


def test_star_import_exports_every_old_name():
    namespace: dict[str, object] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        exec("from ossie_ontology.spec import *", namespace)

    assert {name for name, _ in _RENAMED} <= namespace.keys()


def test_unknown_name_raises_attribute_error():
    with pytest.raises(AttributeError, match="OssieObject"):
        spec.OssieObject


def test_dir_lists_the_old_names():
    assert {name for name, _ in _RENAMED} <= set(dir(spec))
