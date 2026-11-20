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

"""Relationship handling: composite/oriented join parsing and collision-free
naming (semantido -> Ossie), and foreign keys regenerated from relationships
(Ossie -> semantido)."""

from ossie import OssieDocument
from semantido.generators.semantic_layer import Relationship, RelationshipType

from ossie_semantido.converter_issues import ConverterIssueType
from ossie_semantido.ossie_to_semantido import ossie_to_semantido_source
from ossie_semantido.semantido_to_ossie import _parse_join, _relationship_to_ossie


def _expr(expression: str) -> dict:
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]}


def _rel(join, from_table="orders", to_table="party"):
    return Relationship(
        from_table=from_table,
        to_table=to_table,
        join_condition=join,
        relationship_type=RelationshipType.MANY_TO_ONE,
        description="",
    )


# --- semantido -> Ossie: join parsing --------------------------------------


def test_parse_single_join_either_orientation():
    assert _parse_join("orders.party_id = party.id", "orders") == (["party_id"], ["id"])
    assert _parse_join("party.id = orders.party_id", "orders") == (["party_id"], ["id"])


def test_parse_composite_join_keeps_arity_and_alignment():
    from_cols, to_cols = _parse_join(
        "o.product_id = p.id AND o.variant_id = p.variant_id", "o"
    )
    assert from_cols == ["product_id", "variant_id"]
    assert to_cols == ["id", "variant_id"]


def test_parse_rejects_non_column_equality():
    assert _parse_join("LOWER(a.x) = b.y", "a") is None
    assert _parse_join("a.x = b.y AND 1 = 1", "a") is None


def test_relationship_name_includes_columns():
    issues = []
    rel = _relationship_to_ossie(_rel("orders.buyer_id = party.id"), issues)
    assert rel.from_columns == ["buyer_id"]
    assert rel.to_columns == ["id"]
    assert "buyer_id" in rel.name
    assert not issues


def test_two_fks_between_same_pair_get_distinct_names():
    issues = []
    r1 = _relationship_to_ossie(_rel("orders.buyer_id = party.id"), issues)
    r2 = _relationship_to_ossie(_rel("orders.seller_id = party.id"), issues)
    assert r1.name != r2.name
    assert not issues


def test_composite_relationship_has_matching_arity():
    issues = []
    rel = _relationship_to_ossie(
        _rel(
            "line.product_id = variant.id AND line.variant_id = variant.variant_id",
            from_table="line",
            to_table="variant",
        ),
        issues,
    )
    assert len(rel.from_columns) == len(rel.to_columns) == 2
    assert not issues


# --- Ossie -> semantido: foreign keys from relationships -------------------


def test_reverse_emits_single_fk_from_relationship_without_extension():
    document = OssieDocument.model_validate(
        {
            "name": "m",
            "datasets": [
                {
                    "name": "orders",
                    "source": "orders",
                    "primary_key": ["id"],
                    "fields": [
                        {"name": "id", "expression": _expr("id"), "datatype": "Integer"},
                        {
                            "name": "party_id",
                            "expression": _expr("party_id"),
                            "datatype": "Integer",
                        },
                    ],
                },
                {
                    "name": "party",
                    "source": "party",
                    "primary_key": ["id"],
                    "fields": [
                        {"name": "id", "expression": _expr("id"), "datatype": "Integer"}
                    ],
                },
            ],
            "relationships": [
                {
                    "name": "orders__party__party_id",
                    "from": "orders",
                    "to": "party",
                    "from_columns": ["party_id"],
                    "to_columns": ["id"],
                }
            ],
        }
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert "party_id = Column(Integer, ForeignKey('party.id'))" in result.output


def test_reverse_emits_composite_foreign_key_constraint():
    document = OssieDocument.model_validate(
        {
            "name": "m",
            "datasets": [
                {
                    "name": "line",
                    "source": "line",
                    "primary_key": ["id"],
                    "fields": [
                        {"name": "id", "expression": _expr("id"), "datatype": "Integer"},
                        {
                            "name": "product_id",
                            "expression": _expr("product_id"),
                            "datatype": "Integer",
                        },
                        {
                            "name": "variant_id",
                            "expression": _expr("variant_id"),
                            "datatype": "Integer",
                        },
                    ],
                },
                {
                    "name": "variant",
                    "source": "variant",
                    "primary_key": ["id", "variant_id"],
                    "fields": [
                        {"name": "id", "expression": _expr("id"), "datatype": "Integer"},
                        {
                            "name": "variant_id",
                            "expression": _expr("variant_id"),
                            "datatype": "Integer",
                        },
                    ],
                },
            ],
            "relationships": [
                {
                    "name": "line__variant__product_id_variant_id",
                    "from": "line",
                    "to": "variant",
                    "from_columns": ["product_id", "variant_id"],
                    "to_columns": ["id", "variant_id"],
                }
            ],
        }
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert (
        "ForeignKeyConstraint(['product_id', 'variant_id'], "
        "['variant.id', 'variant.variant_id'])" in result.output
    )


def test_reverse_reports_relationship_with_unknown_source_dataset():
    document = OssieDocument.model_validate(
        {
            "name": "m",
            "datasets": [
                {
                    "name": "orders",
                    "source": "orders",
                    "primary_key": ["id"],
                    "fields": [
                        {"name": "id", "expression": _expr("id"), "datatype": "Integer"}
                    ],
                }
            ],
            "relationships": [
                {
                    "name": "ghost__orders__x",
                    "from": "ghost",
                    "to": "orders",
                    "from_columns": ["x"],
                    "to_columns": ["id"],
                }
            ],
        }
    )
    result = ossie_to_semantido_source(document)
    dropped = {
        i.element_name
        for i in result.issues
        if i.issue_type is ConverterIssueType.RELATIONSHIP_DROPPED
    }
    assert dropped == {"ghost__orders__x"}
