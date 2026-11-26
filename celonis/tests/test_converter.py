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

import jsonschema
import pytest
import yaml

from ossie_celonis import ConversionError, celonis_to_ossie, ossie_to_celonis
from ossie_celonis.cli import main


def identifier(name, kind="OBJECT", namespace="demo"):
    return {"name": name, "namespace": namespace, "entityType": kind}


def entity(name, kind="OBJECT", fields=None):
    return {
        "entityIdentifier": identifier(name, kind),
        "pigEntityType": kind,
        "authoringOrigin": {"packageKey": "demo-package", "assetKey": name},
        "displayName": name.title(),
        "agentContext": "Business context",
        "attributes": None,
        "description": "Demo entity",
        "primaryKeys": ["ID"],
        "fields": fields or [{"id": "ID", "dataType": "STRING", "required": True}],
    }


@pytest.fixture
def pig():
    order = entity(
        "Order",
        fields=[
            {"id": "ID", "dataType": "STRING", "required": True},
            {"id": "CustomerID", "dataType": "STRING"},
        ],
    )
    customer = entity("Customer")
    event = entity(
        "Created",
        "EVENT_SOURCE",
        [
            {"id": "ID", "dataType": "STRING"},
            {"id": "Time", "dataType": "TIMESTAMP"},
            {"id": "OrderID", "dataType": "STRING"},
        ],
    )
    event["timestampAttribute"] = "Time"
    joins = []
    for name, source, target, column in [
        ("OrderCustomer", order, customer, "CustomerID"),
        ("CreatedOrder", event, order, "OrderID"),
    ]:
        joins.append(
            {
                "entityIdentifier": identifier(name, "RELATIONSHIP"),
                "authoringOrigin": {"packageKey": "demo-package", "assetKey": name},
                "source": deepcopy(source["entityIdentifier"]),
                "target": deepcopy(target["entityIdentifier"]),
                "relationshipType": "INSTANCE_TO_INSTANCE",
                "cardinality": "MANY_TO_ONE",
                "foreignKeyMappings": [
                    {"sourceField": {"id": column}, "targetField": {"id": "ID"}}
                ],
            }
        )
    ontology = {
        "entityIdentifier": identifier("RelatedTo", "RELATIONSHIP"),
        "relationshipType": "TYPE_TO_TYPE",
        "source": deepcopy(order["entityIdentifier"]),
        "target": deepcopy(customer["entityIdentifier"]),
        "description": None,
    }
    bindings = [
        {
            "namespace": "demo",
            "name": e["entityIdentifier"]["name"] + "-binding",
            "entityRef": deepcopy(e["entityIdentifier"]),
            "sql": f"SELECT * FROM {e['displayName']}",
            "dialect": "STARROCKS",
            "extra": None,
        }
        for e in [order, customer, event]
    ]
    return {
        "objects": [order, customer],
        "eventSources": [event],
        "events": [],
        "relationships": [ontology, *joins],
        "dataBindings": bindings,
        "metrics": [{"pql": 'COUNT_TABLE("Order")'}],
        "perspectives": [],
        "futureEntities": [{"unknown": True}],
    }


def convert(pig):
    with pytest.warns(UserWarning, match="Preserved"):
        return celonis_to_ossie(pig)


def test_projection_and_exact_round_trip(pig):
    original = deepcopy(pig)
    model = convert(pig)
    schema = json.loads(
        (Path(__file__).parents[2] / "ossie/core-spec/ossie-schema.json").read_text()
    )
    jsonschema.validate(model, schema)
    assert model["name"] == "demo-package"
    assert len(model["datasets"]) == 3
    event = model["datasets"][2]
    assert event["fields"][1]["datatype"] == "DateTime"
    assert event["primary_key"] == ["ID"]
    assert model["relationships"][0]["from_columns"] == ["CustomerID"]
    assert "metrics" not in model  # PQL must never masquerade as ANSI SQL.
    assert ossie_to_celonis(model) == original
    assert pig == original


@pytest.mark.parametrize(
    "cardinality,reverse", [("ONE_TO_MANY", True), ("ONE_TO_ONE", False)]
)
def test_join_direction_and_composite_order(pig, cardinality, reverse):
    join = pig["relationships"][1]
    join["cardinality"] = cardinality
    pig["objects"][0]["fields"].append({"id": "CustomerTenantID", "dataType": "STRING"})
    pig["objects"][1]["fields"].append({"id": "TenantID", "dataType": "STRING"})
    join["foreignKeyMappings"].append(
        {"sourceField": {"id": "CustomerTenantID"}, "targetField": {"id": "TenantID"}}
    )
    model = convert(pig)
    mapped = model["relationships"][0]
    assert mapped["from"] == ("demo/Customer" if reverse else "demo/Order")
    assert mapped["from_columns"] == (
        ["ID", "TenantID"] if reverse else ["CustomerID", "CustomerTenantID"]
    )
    assert ossie_to_celonis(model) == pig


def test_junction_relationship_preserved(pig):
    pig["relationships"][1]["cardinality"] = "MANY_TO_MANY"
    pig["relationships"][1]["junctionTable"] = {"fields": [{"id": "LeftID"}]}
    model = convert(pig)
    assert len(model["relationships"]) == 1
    assert ossie_to_celonis(model) == pig


def test_multiple_bindings_and_legacy_keys(pig):
    pig["objects"][0]["primaryKeys"] = []
    pig["dataBindings"][0]["primaryKeyColumns"] = ["ID"]
    binding = deepcopy(pig["dataBindings"][0])
    binding.update(name="second-binding", sql="SELECT * FROM AnotherOrder")
    pig["dataBindings"].append(binding)
    model = convert(pig)
    assert "UNION ALL" in model["datasets"][0]["source"]
    assert model["datasets"][0]["primary_key"] == ["ID"]
    assert ossie_to_celonis(model) == pig


@pytest.mark.parametrize("with_dialects", [False, True])
def test_authoritative_sql_export(pig, with_dialects):
    model = convert(pig)
    expression = {"sql": '"ID" || \'-authoritative\''}
    if with_dialects:
        expression["dialects"] = [
            {"dialect": "OSSIE_SQL_2026", "expression": "wrong_expression"},
            {"dialect": "ANSI_SQL", "expression": "another_wrong_expression"},
        ]
    model["datasets"][0]["fields"][0]["expression"] = expression
    restored = ossie_to_celonis(model)
    sql = restored["dataBindings"][0]["sql"]
    assert '"ID" || \'-authoritative\' AS "ID"' in sql
    assert "wrong_expression" not in sql


@pytest.mark.parametrize("edit", ["source", "expression"])
def test_regenerated_binding_preserves_legacy_key(pig, edit):
    pig["objects"][0]["primaryKeys"] = []
    pig["dataBindings"][0]["primaryKeyColumns"] = ["ID"]
    model = convert(pig)
    dataset = model["datasets"][0]
    if edit == "source":
        dataset["source"] = "SELECT * FROM AnotherOrder"
    else:
        dataset["fields"][0]["expression"] = {"sql": 'CAST("ID" AS VARCHAR)'}
    restored = ossie_to_celonis(model)
    assert restored["objects"][0]["primaryKeys"] == []
    assert restored["dataBindings"][0]["primaryKeyColumns"] == ["ID"]
    assert convert(restored)["datasets"][0]["primary_key"] == ["ID"]


def test_edits_apply_and_keep_native_metadata(pig):
    model = convert(pig)
    model["datasets"][0]["description"] = "Edited description"
    model["datasets"][2]["fields"][1]["description"] = "Event time"
    restored = ossie_to_celonis(model)
    assert restored["objects"][0]["description"] == "Edited description"
    assert restored["objects"][0]["displayName"] == "Order"
    assert restored["eventSources"][0]["timestampAttribute"] == "Time"
    assert restored["eventSources"][0]["fields"][1]["description"] == "Event time"
    assert restored["dataBindings"][0]["sql"] == pig["dataBindings"][0]["sql"]


def test_new_ossie_model_and_sql_generation():
    model = {
        "version": "0.3.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "warehouse.sales.orders",
                "primary_key": ["id"],
                "unique_keys": [["id"]],
                "fields": [
                    {
                        "name": "id",
                        "datatype": "Integer",
                        "expression": {
                            "dialects": [
                                {"dialect": "ANSI_SQL", "expression": "order_id"}
                            ]
                        },
                    },
                    {
                        "name": "amount",
                        "datatype": "Decimal",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "net_amount * 1.2",
                                }
                            ]
                        },
                    },
                ],
            }
        ],
    }
    pig = ossie_to_celonis(model, package_key="target", namespace="custom")
    assert pig["objects"][0]["entityIdentifier"] == identifier(
        "orders", namespace="custom"
    )
    assert pig["objects"][0]["primaryKeys"] == ["id"]
    assert pig["objects"][0]["authoringOrigin"]["packageKey"] == "target"
    assert (
        pig["dataBindings"][0]["sql"]
        == 'SELECT order_id AS "id", net_amount * 1.2 AS "amount" FROM warehouse.sales.orders'
    )
    assert json.loads(pig["objects"][0]["attributes"]["apache_ossie"])[
        "unique_keys"
    ] == [["id"]]
    round_tripped = celonis_to_ossie(pig)
    assert round_tripped["datasets"][0]["fields"][1]["datatype"] == "Decimal"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda p: p.update(objects={}), "array"),
        (lambda p: p["objects"].append(deepcopy(p["objects"][0])), "collision"),
        (lambda p: p["objects"][0].update(primaryKeys=["missing"]), "undeclared"),
        (
            lambda p: p["relationships"][1]["source"].update(name="missing"),
            "Unresolved",
        ),
        (
            lambda p: p["relationships"][1]["foreignKeyMappings"][0][
                "sourceField"
            ].update(id="missing"),
            "Unknown relationship column",
        ),
    ],
)
def test_invalid_pig_rejected(pig, mutation, match):
    mutation(pig)
    with pytest.raises(ConversionError, match=match):
        celonis_to_ossie(pig)


def test_opaque_type_preserved(pig):
    pig["objects"][0]["fields"][0]["dataType"] = "CUSTOM_NATIVE_TYPE"
    model = convert(pig)
    assert model["datasets"][0]["fields"][0]["datatype"] == "Opaque"
    assert ossie_to_celonis(model) == pig


def test_null_authoring_origin_round_trip_and_package_override(pig):
    for item in pig["objects"] + pig["relationships"]:
        item["authoringOrigin"] = None
    model = convert(pig)
    assert ossie_to_celonis(model) == pig
    model["datasets"][0]["source"] = "new_source"
    exported = ossie_to_celonis(model, package_key="target")
    assert exported["objects"][0]["authoringOrigin"]["packageKey"] == "target"
    assert all(
        b["authoringOrigin"]["packageKey"] == "target"
        for b in exported["dataBindings"]
    )
    assert exported["relationships"][1]["authoringOrigin"]["packageKey"] == "target"


@pytest.mark.parametrize("side", ["sourceField", "targetField"])
def test_null_foreign_key_reference_exits_cleanly(pig, side, tmp_path, capsys):
    pig["relationships"][1]["foreignKeyMappings"][0][side] = None
    with pytest.raises(ConversionError, match=f"{side}.id"):
        celonis_to_ossie(pig)
    source, output = tmp_path / "input.json", tmp_path / "output.yaml"
    source.write_text(json.dumps(pig))
    output.write_text("unchanged")
    with pytest.raises(SystemExit) as exc:
        main(["celonis-to-ossie", "--core-only", "-i", str(source), "-o", str(output)])
    assert exc.value.code == 2
    assert output.read_text() == "unchanged"
    error = capsys.readouterr().err
    assert f"{side}.id" in error
    assert "Traceback" not in error


def test_structured_context_round_trips_at_all_scopes(pig):
    model = convert(pig)
    context = {"instructions": "use for orders", "business_rules": ["Keep IDs"]}
    model["ai_context"] = deepcopy(context)
    model["datasets"][0]["ai_context"] = deepcopy(context)
    model["relationships"][0]["ai_context"] = deepcopy(context)
    exported = ossie_to_celonis(model)
    assert exported["objects"][0]["agentContext"] == json.dumps(context)
    assert exported["relationships"][1]["agentContext"] == json.dumps(context)
    imported = convert(exported)
    assert imported["ai_context"] == context
    assert imported["datasets"][0]["ai_context"] == context
    assert imported["relationships"][0]["ai_context"] == context
    assert ossie_to_celonis(imported) == exported


@pytest.mark.parametrize("edited", [False, True])
def test_json_looking_context_stays_text(pig, edited):
    text = '{"instructions": "native text"}'
    if edited:
        model = convert(pig)
        model["datasets"][0]["ai_context"] = {"instructions": "original object"}
        model["relationships"][0]["ai_context"] = {"instructions": "original object"}
        pig = ossie_to_celonis(model)
    pig["objects"][0]["agentContext"] = text
    pig["relationships"][1]["agentContext"] = text
    imported = convert(pig)
    assert imported["datasets"][0]["ai_context"] == text
    assert imported["relationships"][0]["ai_context"] == text
    assert ossie_to_celonis(imported) == pig


@pytest.mark.parametrize("companions", [False, True])
def test_export_rejects_case_distinct_dataset_identities(companions):
    from ossie_celonis.ontology import build_ontology_documents

    model = {
        "version": "0.3.0.dev0",
        "name": "example",
        "datasets": [
            {
                "name": name,
                "source": name,
                "primary_key": ["ID"],
                "fields": [
                    {"name": "ID", "datatype": "String", "expression": {"sql": "ID"}}
                ],
            }
            for name in ("Orders", "orders")
        ],
    }
    kwargs = {}
    if companions:
        ontology, mapping = build_ontology_documents(model)
        ontology["ontology"][0]["description"] = "EDITED"
        kwargs.update(ontology=ontology, mapping=mapping)
    with pytest.raises(ConversionError, match="Dataset identity collision"):
        ossie_to_celonis(model, **kwargs)


@pytest.mark.parametrize("bucket", ["futureEntities", "relationships"])
def test_export_rejects_collision_with_preserved_identity(pig, bucket):
    original = (
        pig["objects"][0] if bucket == "futureEntities" else pig["relationships"][1]
    )
    duplicate = deepcopy(original)
    duplicate["entityIdentifier"]["name"] = (
        original["entityIdentifier"]["name"].upper()
    )
    if bucket == "relationships":
        duplicate["relationshipType"] = "TYPE_TO_TYPE"
    pig[bucket].append(duplicate)
    model = convert(pig)
    with pytest.raises(ConversionError, match="Entity identity collision"):
        ossie_to_celonis(model)


@pytest.mark.parametrize(
    "identifier",
    [
        None,
        {},
        {"name": "binding"},
        {"namespace": None, "name": "binding", "entityType": "DATA_BINDING"},
        {"namespace": "d", "name": " ", "entityType": "DATA_BINDING"},
        {"namespace": "d", "name": 123, "entityType": "DATA_BINDING"},
    ],
)
def test_incomplete_preserved_identifiers_round_trip(identifier):
    pig = {
        "objects": [
            {
                "entityIdentifier": {
                    "namespace": "d", "name": "O", "entityType": "OBJECT"
                },
                "fields": [{"id": "ID", "dataType": "STRING"}],
            }
        ],
        "dataBindings": [
            {
                "entityIdentifier": deepcopy(identifier),
                "entityRef": {"namespace": "d", "name": "O", "entityType": "OBJECT"},
                "sql": "SELECT * FROM o",
            }
        ],
        "futureEntities": [{"entityIdentifier": deepcopy(identifier), "extra": True}],
    }
    model = celonis_to_ossie(pig)
    assert ossie_to_celonis(model) == pig


def test_relationship_apache_ossie_clears_removed_or_string_context(pig):
    model = convert(pig)
    model["relationships"][0]["ai_context"] = {"instructions": "join rule"}
    exported = ossie_to_celonis(model)
    imported = convert(exported)
    del imported["relationships"][0]["ai_context"]
    updated = ossie_to_celonis(imported)
    stored = json.loads(updated["relationships"][1]["attributes"]["apache_ossie"])
    assert "ai_context" not in stored
    assert "ai_context" not in convert(updated)["relationships"][0]
    imported["relationships"][0]["ai_context"] = '{"instructions": "join rule"}'
    as_text = ossie_to_celonis(imported)
    assert convert(as_text)["relationships"][0]["ai_context"] == '{"instructions": "join rule"}'


@pytest.mark.parametrize(
    "property,value",
    [
        ("ai_context", "model notes"),
        ("ai_context", {"instructions": "model notes"}),
        ("ai_context", {}),
        ("ai_context", ""),
        ("description", "model description"),
        ("description", ""),
    ],
)
def test_model_header_preserves_empty_values_and_removals(pig, property, value):
    model = convert(pig)
    model[property] = deepcopy(value)
    exported = ossie_to_celonis(model)
    header = json.loads(exported["objects"][0]["attributes"]["apache_ossie_model"])
    assert header[property] == value
    imported = convert(exported)
    assert imported[property] == value
    del imported[property]
    updated = ossie_to_celonis(imported)
    header = json.loads(updated["objects"][0]["attributes"]["apache_ossie_model"])
    assert property not in header
    assert property not in convert(updated)


def test_non_ansi_metrics_and_malformed_extensions_fail(pig):
    model = convert(pig)
    model["metrics"] = [
        {
            "name": "count",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "COUNT(*)"}]
            },
        }
    ]
    with pytest.raises(ConversionError, match="PQL"):
        ossie_to_celonis(model)
    del model["metrics"]
    model["nonportable_extensions"][0]["data"] = "{"
    with pytest.raises(ConversionError, match="nonportable_extensions.*data"):
        ossie_to_celonis(model)


def test_cli_round_trip_and_failure_does_not_overwrite(pig, tmp_path):
    input_path, yaml_path, output_path = [
        tmp_path / p for p in ["pig.json", "model.yaml", "out.json"]
    ]
    input_path.write_text(json.dumps(pig))
    with pytest.warns(UserWarning):
        main(["celonis-to-ossie", "-i", str(input_path), "-o", str(yaml_path)])
    assert yaml.safe_load(yaml_path.read_text())["datasets"]
    main(["ossie-to-celonis", "-i", str(yaml_path), "-o", str(output_path)])
    assert json.loads(output_path.read_text()) == pig
    yaml_path.write_text("semantic_model: []")
    before = output_path.read_text()
    with pytest.raises(SystemExit) as exc:
        main(["ossie-to-celonis", "-i", str(yaml_path), "-o", str(output_path)])
    assert exc.value.code == 2
    assert output_path.read_text() == before


def test_tpcds_datasets_and_joins():
    path = Path(__file__).parents[2] / "ossie/examples/tpcds_semantic_model.yaml"
    model = yaml.safe_load(path.read_text())
    # This first pass projects row-level data and joins; aggregate PQL generation is separate.
    model.pop("metrics", None)
    for dataset in model["datasets"]:
        dataset.pop("metrics", None)
    pig = ossie_to_celonis(model)
    assert len(pig["objects"]) == len(model["datasets"])
    imported = celonis_to_ossie(pig)
    assert len(imported["relationships"]) == len(model.get("relationships", []))


def test_reference_identity_is_case_insensitive(pig):
    pig["relationships"][1]["source"]["name"] = "ORDER"
    pig["dataBindings"][0]["entityRef"]["namespace"] = "DEMO"
    model = convert(pig)
    assert model["datasets"][0]["source"] == "SELECT * FROM Order"
    assert model["relationships"][0]["from"] == "demo/Order"
    assert ossie_to_celonis(model) == pig


def test_binding_dialects_and_keys_must_agree(pig):
    binding = deepcopy(pig["dataBindings"][0])
    binding.update(name="second-binding", dialect="OTHER_DIALECT")
    pig["dataBindings"].append(binding)
    with pytest.raises(ConversionError, match="Mixed binding dialects"):
        celonis_to_ossie(pig)
    binding["dialect"] = "STARROCKS"
    pig["objects"][0]["primaryKeys"] = []
    binding["primaryKeyColumns"] = ["ID"]
    with pytest.raises(ConversionError, match="Inconsistent binding primary keys"):
        celonis_to_ossie(pig)


def test_metadata_edits_keep_original_relationship_and_key_contract(pig):
    join = pig["relationships"][1]
    join["cardinality"] = "ONE_TO_MANY"
    join["foreignKeyMappings"][0]["sourceField"]["description"] = (
        "Original key annotation"
    )
    pig["objects"][0]["primaryKeys"] = []
    pig["dataBindings"][0]["primaryKeyColumns"] = ["ID"]
    model = convert(pig)
    model["relationships"][0]["ai_context"] = "Updated context"
    model["datasets"][0]["description"] = "Updated description"
    restored = ossie_to_celonis(model)
    assert restored["relationships"][1]["cardinality"] == "ONE_TO_MANY"
    assert (
        restored["relationships"][1]["foreignKeyMappings"] == join["foreignKeyMappings"]
    )
    assert restored["objects"][0]["primaryKeys"] == []
    assert restored["dataBindings"][0]["primaryKeyColumns"] == ["ID"]


def test_foreign_extensions_preserved_as_native_attributes(pig):
    model = convert(pig)
    extra = {"vendor_name": "OTHER_VENDOR", "data": {"keep": True}}
    model["nonportable_extensions"].append(extra)
    model["datasets"][0]["nonportable_extensions"].append(extra)
    model["relationships"][0]["nonportable_extensions"].append(extra)
    restored = ossie_to_celonis(model)
    attrs = restored["objects"][0]["attributes"]
    assert json.loads(attrs["apache_ossie"])["nonportable_extensions"] == [extra]
    assert json.loads(attrs["apache_ossie_model"])["nonportable_extensions"] == [extra]
    assert json.loads(restored["relationships"][1]["attributes"]["apache_ossie"])[
        "nonportable_extensions"
    ] == [extra]
    imported = celonis_to_ossie(restored)
    assert ossie_to_celonis(imported) == restored


def test_duplicate_json_keys_rejected_before_writing(tmp_path):
    source, destination = tmp_path / "input.json", tmp_path / "out.json"
    source.write_text('{"objects":[],"objects":[{}]}')
    with pytest.raises(SystemExit) as exc:
        main(["celonis-to-ossie", "-i", str(source), "-o", str(destination)])
    assert exc.value.code == 2
    assert not destination.exists()
