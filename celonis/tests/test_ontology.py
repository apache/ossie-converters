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

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from ossie_celonis import (
    ConversionError,
    build_ontology_documents,
    celonis_to_ossie_bundle,
    ossie_to_celonis,
    validate_ontology_documents,
)
from ossie_celonis.cli import main
from test_converter import entity, identifier, pig  # noqa: F401 (shared fixture)


@pytest.fixture
def context_pig(pig):
    system = entity("ERP", "INFORMATION_SYSTEM")
    system.pop("fields")
    system.pop("primaryKeys")
    pig["informationSystems"] = [system]
    edge = pig["relationships"][0]
    edge.update(
        target=deepcopy(system["entityIdentifier"]),
        cardinality="ONE_TO_ONE",
        description="Orders originate in ERP",
    )
    return pig


def bundle(pig):
    with pytest.warns(UserWarning, match="Preserved"):
        return celonis_to_ossie_bundle(pig)


def export(b, **kwargs):
    return ossie_to_celonis(
        b["semantic_model"], ontology=b["ontology"], mapping=b["mapping"], **kwargs
    )


def concepts(b):
    return {c["concept"]: c for c in b["ontology"]["ontology"]}


def mapping_metadata(b):
    return json.loads(b["mapping"]["custom_extensions"][0]["data"])


def type_relation(b):
    record = mapping_metadata(b)["type_relationships"][0]
    return next(
        r
        for r in concepts(b)[record["concept"]]["relationships"]
        if r["name"] == record["name"]
    )


def test_complete_context_projection_and_native_fidelity(context_pig):
    original = deepcopy(context_pig)
    b = bundle(context_pig)
    before = deepcopy(b)
    assert set(concepts(b)) == {
        "demo_Order",
        "demo_Customer",
        "demo_Created",
        "demo_ERP",
    }
    assert all(c["type"] == "EntityType" for c in concepts(b).values())
    order = concepts(b)["demo_Order"]
    assert order["identify_by"] == ["field_ID"]
    assert (
        next(r for r in order["relationships"] if r["name"] == "field_ID")[
            "multiplicity"
        ]
        == "OneToOne"
    )
    assert "multiplicity" not in type_relation(
        b
    )  # Type-edge cardinality is preserved only in the native extension.
    assert type_relation(b)["roles"] == [{"concept": "demo_ERP", "name": "target"}]
    assert len(b["mapping"]["concept_mappings"]) == 3  # No invented ERP rows.
    cm = b["mapping"]["concept_mappings"][0]
    assert cm["object_mappings"][0]["referent_mappings"] == [
        {"relationship": "field_ID", "expression": '"demo/Order"."ID"'}
    ]
    customer_link = next(
        n
        for n in cm["link_mappings"][0]["children"]
        if n["object_mapping"]["concept"] == "demo_Customer"
    )
    assert (
        customer_link["object_mapping"]["referent_mappings"][0]["expression"]
        == '"demo/Order"."CustomerID"'
    )
    assert export(b) == original
    assert context_pig == original and b == before


def test_composite_identity_and_foreign_key_mapping(context_pig):
    order, customer = context_pig["objects"]
    order["fields"].append({"id": "Tenant", "dataType": "INTEGER"})
    customer["fields"].append({"id": "Tenant", "dataType": "INTEGER"})
    customer["primaryKeys"] = ["Tenant", "ID"]
    join = context_pig["relationships"][1]
    join["foreignKeyMappings"].append(
        {"sourceField": {"id": "Tenant"}, "targetField": {"id": "Tenant"}}
    )
    b = bundle(context_pig)
    assert concepts(b)["demo_Customer"]["identify_by"] == ["field_Tenant", "field_ID"]
    cm = b["mapping"]["concept_mappings"][0]
    link = next(
        n
        for n in cm["link_mappings"][0]["children"]
        if n["object_mapping"]["concept"] == "demo_Customer"
    )
    assert link["object_mapping"]["referent_mappings"] == [
        {"relationship": "field_Tenant", "expression": '"demo/Order"."Tenant"'},
        {"relationship": "field_ID", "expression": '"demo/Order"."CustomerID"'},
    ]
    assert export(b) == context_pig


def test_concepts_without_keys_and_unsupported_values(context_pig):
    context_pig["objects"][0]["primaryKeys"] = []
    context_pig["objects"][0]["fields"].append(
        {"id": "NativeValue", "dataType": "UNKNOWN"}
    )
    with pytest.warns(UserWarning) as caught:
        b = celonis_to_ossie_bundle(context_pig)
    assert any("no representable key" in str(w.message) for w in caught)
    assert any("No ontology value type" in str(w.message) for w in caught)
    assert "identify_by" not in concepts(b)["demo_Order"]
    assert "demo_Order" not in {m["concept"] for m in b["mapping"]["concept_mappings"]}
    assert export(b) == context_pig


def test_names_are_safe_and_collision_free(context_pig):
    other = entity("Order")
    other["entityIdentifier"]["namespace"] = "demo!"
    context_pig["objects"].append(other)
    # Distinct names that sanitize to the same identifier.
    context_pig["informationSystems"].extend(
        [entity("a-b", "INFORMATION_SYSTEM"), entity("a_b", "INFORMATION_SYSTEM")]
    )
    with pytest.warns(UserWarning):
        b = celonis_to_ossie_bundle(context_pig)
    names = list(concepts(b))
    assert len(names) == len(set(n.casefold() for n in names)) == 7
    assert export(b) == context_pig


def test_type_endpoint_resolution_and_case_insensitivity(context_pig):
    context_pig["relationships"][0]["target"]["name"] = "erp"
    b = bundle(context_pig)
    assert type_relation(b)["roles"][0]["concept"] == "demo_ERP"
    assert export(b) == context_pig
    context_pig["relationships"][0]["target"]["name"] = "missing"
    with pytest.warns(UserWarning), pytest.raises(
        ConversionError, match="Unresolved ontology"
    ):
        celonis_to_ossie_bundle(context_pig)


def test_concept_and_property_description_edits(context_pig):
    b = bundle(context_pig)
    concepts(b)["demo_Order"]["description"] = "Updated order meaning"
    concepts(b)["demo_ERP"]["description"] = "System of record"
    field = next(
        r
        for r in concepts(b)["demo_Order"]["relationships"]
        if r["name"] == "field_CustomerID"
    )
    field["description"] = "Customer identifier"
    restored = export(b)
    assert restored["objects"][0]["description"] == "Updated order meaning"
    assert restored["objects"][0]["fields"][1]["description"] == "Customer identifier"
    assert restored["informationSystems"][0]["description"] == "System of record"
    regenerated = bundle(restored)
    assert concepts(regenerated)["demo_Order"]["description"] == "Updated order meaning"


def test_conflicting_descriptions_fail(context_pig):
    b = bundle(context_pig)
    concepts(b)["demo_Order"]["description"] = "Ontology edit"
    b["semantic_model"]["datasets"][0]["description"] = "Different core edit"
    with pytest.raises(ConversionError, match="Conflicting core"):
        export(b)


def test_type_edge_edit_delete_and_add(context_pig):
    b = bundle(context_pig)
    type_relation(b)["description"] = "Updated origin"
    type_relation(b)["roles"][0]["concept"] = "demo_Customer"
    restored = export(b)
    edge = restored["relationships"][0]
    assert edge["description"] == "Updated origin"
    assert edge["target"]["name"] == "Customer"
    assert edge["cardinality"] == "ONE_TO_ONE"
    b = bundle(context_pig)
    old_name = type_relation(b)["name"]
    order = concepts(b)["demo_Order"]
    order["relationships"] = [
        r for r in order["relationships"] if r["name"] != old_name
    ]
    order["relationships"].append(
        {
            "name": "handledBy",
            "roles": [{"concept": "demo_ERP"}],
            "description": "ERP handles orders",
            "verbalizes": ["{demo_Order} is handled by {demo_ERP}"],
        }
    )
    restored = export(b, namespace="new")
    assert not any(
        r["entityIdentifier"]["name"] == "RelatedTo" for r in restored["relationships"]
    )
    edge = next(
        r
        for r in restored["relationships"]
        if r["entityIdentifier"]["name"] == "handledBy"
    )
    assert edge["relationshipType"] == "TYPE_TO_TYPE"
    assert edge["entityIdentifier"]["namespace"] == "new"
    assert edge["target"]["entityType"] == "INFORMATION_SYSTEM"
    assert edge["description"] == "ERP handles orders"
    assert "foreignKeyMappings" not in edge
    again = bundle(restored)
    assert export(again) == restored


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda b: b["mapping"]["ontology_ref"].update(name="wrong"), "ontology_ref"),
        (
            lambda b: b["mapping"]["semantic_model_ref"].update(name="wrong"),
            "semantic_model_ref",
        ),
        (
            lambda b: b["ontology"]["ontology"].append(
                deepcopy(b["ontology"]["ontology"][0])
            ),
            "Duplicate",
        ),
        (
            lambda b: type_relation(b)["roles"][0].update(concept="unknown"),
            "Unknown ontology role",
        ),
        (
            lambda b: concepts(b)["demo_Order"].update(identify_by=["missing"]),
            "Unknown identifying",
        ),
        (
            lambda b: concepts(b)["demo_Order"].update(extends=["demo_Order"]),
            "inheritance cycle",
        ),
        (
            lambda b: b["mapping"]["concept_mappings"][0]["object_mappings"][0][
                "referent_mappings"
            ][0].update(expression='"demo/Order"."missing"'),
            "unknown dataset/field",
        ),
        (
            lambda b: b["mapping"]["concept_mappings"][0]["object_mappings"][0][
                "referent_mappings"
            ][0].update(expression="COUNT(*)"),
            "only quoted",
        ),
        (
            lambda b: concepts(b)["demo_Order"].update(requires=["something"]),
            "Unsupported identity",
        ),
        (
            lambda b: b["semantic_model"]["datasets"][0].update(
                primary_key=["CustomerID"]
            ),
            "Core fields, keys or joins changed",
        ),
        (
            lambda b: b["mapping"]["concept_mappings"][0]["object_mappings"][0][
                "referent_mappings"
            ][0].update(expression='"demo/Order"."CustomerID"'),
            "Editing mapping",
        ),
    ],
)
def test_invalid_or_untranslatable_companions_fail(context_pig, mutation, match):
    b = bundle(context_pig)
    mutation(b)
    with pytest.raises(ConversionError, match=match):
        export(b)


def test_new_core_model_can_be_enriched():
    model = {
        "version": "0.2.0.dev0",
        "name": "example",
        "datasets": [
            {
                "name": "orders",
                "source": "orders",
                "unique_keys": [["ID"]],
                "fields": [
                    {
                        "name": "ID",
                        "datatype": "Integer",
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "ID"}],
                        },
                    }
                ],
            }
        ],
    }
    ontology, mapping = build_ontology_documents(model)
    assert ontology["ontology"][0]["identify_by"] == ["field_ID"]
    ontology["ontology"][0]["description"] = "New order concept"
    native = ossie_to_celonis(
        model, ontology=ontology, mapping=mapping, namespace="custom"
    )
    assert native["objects"][0]["description"] == "New order concept"
    assert native["objects"][0]["entityIdentifier"]["namespace"] == "custom"


def test_cli_companions_custom_paths_and_core_only(context_pig, tmp_path):
    source, core = tmp_path / "pig.json", tmp_path / "model.yaml"
    subdir = tmp_path / "ontology dir"
    subdir.mkdir()
    ontology, mapping = subdir / "concepts.json", subdir / "mapping.yaml"
    source.write_text(json.dumps(context_pig))
    with pytest.warns(UserWarning):
        main(
            [
                "celonis-to-ossie",
                "-i",
                str(source),
                "-o",
                str(core),
                "--ontology",
                str(ontology),
                "--mapping",
                str(mapping),
            ]
        )
    assert (
        yaml.safe_load(mapping.read_text())["semantic_model_ref"]["iri"]
        == "../model.yaml"
    )
    result = tmp_path / "result.json"
    main(
        [
            "ossie-to-celonis",
            "-i",
            str(core),
            "-o",
            str(result),
            "--ontology",
            str(ontology),
            "--mapping",
            str(mapping),
        ]
    )
    assert json.loads(result.read_text()) == context_pig
    main(["ossie-to-celonis", "-i", str(core), "-o", str(result), "--core-only"])
    assert json.loads(result.read_text()) == context_pig
    only_core = tmp_path / "only.json"
    with pytest.warns(UserWarning):
        main(
            ["celonis-to-ossie", "-i", str(source), "-o", str(only_core), "--core-only"]
        )
    assert not (tmp_path / "only.ontology.json").exists()


def test_cli_missing_or_duplicate_companions_do_not_overwrite(context_pig, tmp_path):
    source, core, destination = [
        tmp_path / name for name in ("pig.json", "model.yaml", "out.json")
    ]
    source.write_text(json.dumps(context_pig))
    with pytest.warns(UserWarning):
        main(["celonis-to-ossie", "-i", str(source), "-o", str(core)])
    ontology = tmp_path / "model.ontology.yaml"
    mapping = tmp_path / "model.mapping.yaml"
    destination.write_text("keep")
    saved = ontology.read_text()
    ontology.write_text("name: one\nname: two\n")
    with pytest.raises(SystemExit):
        main(["ossie-to-celonis", "-i", str(core), "-o", str(destination)])
    assert destination.read_text() == "keep"
    ontology.write_text(saved)
    mapping.unlink()
    with pytest.raises(SystemExit):
        main(["ossie-to-celonis", "-i", str(core), "-o", str(destination)])
    assert destination.read_text() == "keep"


def test_staged_outputs_and_path_collisions(context_pig, tmp_path):
    source, core = tmp_path / "pig.json", tmp_path / "model.yaml"
    source.write_text(json.dumps(context_pig))
    core.write_text("keep")
    with pytest.warns(UserWarning), pytest.raises(SystemExit):
        main(
            [
                "celonis-to-ossie",
                "-i",
                str(source),
                "-o",
                str(core),
                "--mapping",
                str(tmp_path / "missing" / "map.yaml"),
            ]
        )
    assert core.read_text() == "keep"
    assert not (tmp_path / "model.ontology.yaml").exists()
    with pytest.warns(UserWarning), pytest.raises(SystemExit):
        main(
            [
                "celonis-to-ossie",
                "-i",
                str(source),
                "-o",
                str(core),
                "--ontology",
                str(core),
            ]
        )
    assert core.read_text() == "keep"


def test_packaged_schemas_match_pinned_spec():
    root = Path(__file__).parents[2]
    for name, upstream in (
        ("core", "core-spec/ossie-schema.json"),
        ("ontology", "ontology/ontology.json"),
        ("mapping", "ontology/mapping.json"),
    ):
        packaged = root / "celonis/src/ossie_celonis/schemas" / f"{name}.json"
        assert json.loads(packaged.read_text()) == json.loads(
            (root / "ossie" / upstream).read_text()
        )


def test_junction_relationship_has_concept_edge_without_invented_mapping(context_pig):
    join = context_pig["relationships"][1]
    join.update(cardinality="MANY_TO_MANY", junctionTable={"fields": [{"id": "left"}]})
    with pytest.warns(UserWarning) as caught:
        b = celonis_to_ossie_bundle(context_pig)
    assert any("junction mapping" in str(w.message) for w in caught)
    relation = next(
        r
        for r in concepts(b)["demo_Order"]["relationships"]
        if r["name"] == "link_demo_OrderCustomer"
    )
    assert relation["roles"][0]["concept"] == "demo_Customer"
    assert "multiplicity" not in relation
    cm = next(
        cm for cm in b["mapping"]["concept_mappings"] if cm["concept"] == "demo_Order"
    )
    assert not any(
        child.get("relationship") == relation["name"]
        for child in cm["link_mappings"][0]["children"]
    )
    assert export(b) == context_pig
