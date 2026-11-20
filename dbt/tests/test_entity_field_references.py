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

"""Entity expressions remain source SQL; keys and joins reference declared fields."""

import subprocess
import sys
from pathlib import Path

import pytest
from metricflow_semantic_interfaces.test_utils import (
    semantic_model_with_guaranteed_meta,
)
from metricflow_semantic_interfaces.type_enums import EntityType
from ossie_dbt.msi_to_ossie import MSIToOssieConverter
from ossie_dbt.ossie_to_msi import OssieToMSIConverter

from ossie import OssieDocument
from tests.helpers import _entity, _manifest


def _assert_core_valid(document: OssieDocument, tmp_path: Path) -> None:
    """Run the pinned core validator, including its semantic reference checks."""
    model_path = tmp_path / "model.yaml"
    model_path.write_text(document.to_ossie_yaml(), encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, str(root / "ossie/validation/validate.py"), str(model_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("entity_type", [EntityType.PRIMARY, EntityType.UNIQUE])
@pytest.mark.parametrize(
    "expr",
    [None, "customer", "customer_id", "COALESCE(customer_id, 0)", '"Customer ID"'],
)
def test_entity_key_references_field_and_round_trips(
    entity_type: EntityType, expr: str | None, tmp_path: Path
) -> None:
    model = semantic_model_with_guaranteed_meta(
        name="customers", entities=[_entity("customer", entity_type, expr)]
    )
    result = MSIToOssieConverter().convert(_manifest(semantic_models=[model]))
    assert result.issues == []
    document = result.output
    dataset = document.datasets[0]
    assert dataset.fields is not None
    assert dataset.fields[0].name == "customer"
    assert dataset.fields[0].expression.dialects[0].expression == (expr or "customer")
    if entity_type is EntityType.PRIMARY:
        assert dataset.primary_key == ["customer"]
    else:
        assert dataset.unique_keys == [["customer"]]
    _assert_core_valid(document, tmp_path)

    restored = OssieToMSIConverter().convert(document)
    assert restored.issues == []
    entities = restored.output.semantic_models[0].entities
    assert len(entities) == 1
    assert entities[0].name == "customer"
    assert entities[0].type is entity_type
    assert (entities[0].expr or entities[0].name) == (expr or "customer")


@pytest.mark.parametrize(
    "primary_expr, foreign_expr",
    [
        (None, None),
        ("customer_id", "customer_fk"),
        ("COALESCE(customer_id, 0)", "COALESCE(customer_fk, 0)"),
    ],
)
def test_relationship_references_fields_and_preserves_entity_expressions(
    primary_expr: str | None, foreign_expr: str | None, tmp_path: Path
) -> None:
    models = [
        semantic_model_with_guaranteed_meta(
            name="customers",
            entities=[_entity("customer", EntityType.PRIMARY, primary_expr)],
        ),
        semantic_model_with_guaranteed_meta(
            name="orders",
            entities=[
                _entity("order", EntityType.PRIMARY),
                _entity("customer", EntityType.FOREIGN, foreign_expr),
            ],
        ),
    ]
    result = MSIToOssieConverter().convert(_manifest(semantic_models=models))
    assert result.issues == []
    document = result.output
    assert document.relationships is not None
    assert len(document.relationships) == 1
    relationship = document.relationships[0]
    assert relationship.from_dataset == "orders"
    assert relationship.to == "customers"
    assert relationship.from_columns == ["customer"]
    assert relationship.to_columns == ["customer"]
    _assert_core_valid(document, tmp_path)

    restored = OssieToMSIConverter().convert(document)
    assert restored.issues == []
    restored_models = {model.name: model for model in restored.output.semantic_models}
    for model_name, entity_type, expr in [
        ("customers", EntityType.PRIMARY, primary_expr),
        ("orders", EntityType.FOREIGN, foreign_expr),
    ]:
        entity = next(
            entity
            for entity in restored_models[model_name].entities
            if entity.name == "customer"
        )
        assert entity.type is entity_type
        assert (entity.expr or entity.name) == (expr or "customer")


def test_fields_over_the_same_source_column_keep_distinct_key_roles(
    tmp_path: Path,
) -> None:
    model = semantic_model_with_guaranteed_meta(
        name="customers",
        entities=[
            _entity("customer", EntityType.PRIMARY, "customer_id"),
            _entity("customer_lookup", EntityType.UNIQUE, "customer_id"),
        ],
    )
    document = MSIToOssieConverter().convert(_manifest(semantic_models=[model])).output
    assert document.datasets[0].primary_key == ["customer"]
    assert document.datasets[0].unique_keys == [["customer_lookup"]]
    _assert_core_valid(document, tmp_path)
    restored = OssieToMSIConverter().convert(document).output.semantic_models[0]
    assert {(e.name, e.type, e.expr) for e in restored.entities} == {
        ("customer", EntityType.PRIMARY, "customer_id"),
        ("customer_lookup", EntityType.UNIQUE, "customer_id"),
    }
