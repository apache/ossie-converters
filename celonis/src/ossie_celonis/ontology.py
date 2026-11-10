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

"""Business concepts and their standalone mappings onto the core data model."""

import json
import re
import warnings
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from .converter import (
    ConversionError,
    _core,
    _extension,
    _identity,
    _identity_key,
    _metadata,
    _name,
    _quote,
    _validate,
    celonis_to_ossie,
)

VALUE_TYPES = {
    t: t
    for t in ("String", "Integer", "Decimal", "Float", "Boolean", "Date", "DateTime")
}
BUILTINS = {"Any", *VALUE_TYPES}
_COLUMN = re.compile(r'^"((?:[^"]|"")+)"\."((?:[^"]|"")+)"$')


def _unique(text, taken):
    name = re.sub(r"[^A-Za-z0-9_]", "_", text)
    if not name or name[0].isdigit():
        name = "C_" + name
    candidate, suffix = name, 1
    while candidate.casefold() in taken:
        suffix += 1
        candidate = f"{name}_{suffix}"
    taken.add(candidate.casefold())
    return candidate


def _data_projection(model):
    return {
        "datasets": [
            {
                k: deepcopy(v)
                for k, v in dataset.items()
                if k in ("name", "primary_key", "unique_keys")
            }
            | {
                "fields": [
                    {k: v for k, v in field.items() if k in ("name", "datatype")}
                    for field in dataset.get("fields", [])
                ]
            }
            for dataset in model["datasets"]
        ],
        "relationships": [
            {
                k: v
                for k, v in join.items()
                if k in ("name", "from", "to", "from_columns", "to_columns")
            }
            for join in model.get("relationships", [])
        ],
    }


def _column(dataset, field):
    return f"{_quote(dataset)}.{_quote(field)}"


@lru_cache(maxsize=1)
def _validators():
    # Pinned schemas ship with the wheel; validation never retrieves URLs.
    schemas = {
        name: json.loads(
            files("ossie_celonis").joinpath(f"schemas/{name}.json").read_text()
        )
        for name in ("core", "ontology", "mapping")
    }
    registry = Registry()
    for name, schema in schemas.items():
        resource = Resource.from_contents(schema)
        path = (
            "core-spec/ossie-schema.json" if name == "core" else f"ontology/{name}.json"
        )
        registry = registry.with_resources(
            [
                (schema["$id"], resource),
                (
                    f"https://raw.githubusercontent.com/apache/ossie/main/{path}",
                    resource,
                ),
            ]
        )
    return {
        name: Draft202012Validator(schema, registry=registry)
        for name, schema in schemas.items()
    }


def validate_ontology_documents(model, ontology, mapping):
    """Validate schemas, document identities, concepts, roles and mapped fields."""
    _validate(model)
    try:
        # Reject non-JSON values before walking recursive structures.
        json.dumps([ontology, mapping], allow_nan=False)
        for name, document in (("ontology", ontology), ("mapping", mapping)):
            _validators()[name].validate(document)
    except (ValidationError, ValueError, TypeError, RecursionError) as exc:
        raise ConversionError(f"Invalid ontology/mapping document: {exc}") from exc
    if mapping["ontology_ref"]["name"] != ontology["name"]:
        raise ConversionError("Mapping ontology_ref.name does not match the ontology")
    if mapping["semantic_model_ref"]["name"] != model["name"]:
        raise ConversionError(
            "Mapping semantic_model_ref.name does not match the core model"
        )
    concepts = {}
    for component in ontology["ontology"]:
        name = component["concept"]
        if not name.strip() or name in concepts or name in BUILTINS:
            raise ConversionError(f"Duplicate or reserved ontology concept: {name}")
        concepts[name] = component
    declared = {*concepts, *BUILTINS}
    for name, component in concepts.items():
        relations = component.get("relationships", [])
        names = [r["name"] for r in relations]
        if len(names) != len(set(names)) or any(not n.strip() for n in names):
            raise ConversionError(f"Duplicate/empty ontology relationship in {name}")
        for parent in component.get("extends", []):
            if parent not in declared:
                raise ConversionError(f"Unknown ontology supertype: {parent}")
        pending, inherited, seen = [name], set(), set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            ancestor = concepts.get(current, {})
            inherited.update(r["name"] for r in ancestor.get("relationships", []))
            parents = ancestor.get("extends", [])
            if name in parents:
                raise ConversionError(f"Ontology inheritance cycle involving {name}")
            pending.extend(parents)
        for key in component.get("identify_by", []):
            if key not in inherited:
                raise ConversionError(f"Unknown identifying relationship: {name}.{key}")
        for relation in relations:
            for role in relation.get("roles", []):
                if role["concept"] not in declared:
                    raise ConversionError(
                        f"Unknown ontology role concept: {role['concept']}"
                    )
    datasets = {
        d["name"]: {f["name"] for f in d.get("fields", [])} for d in model["datasets"]
    }

    def check_object(obj, concept):
        concept = obj.get("concept", concept)
        if concept not in declared:
            raise ConversionError(f"Unknown mapped concept: {concept}")
        if "expression" in obj:
            match = _COLUMN.fullmatch(obj["expression"])
            if not match:
                raise ConversionError(
                    "This converter supports only quoted dataset.field mapping expressions"
                )
            dataset, field = (s.replace('""', '"') for s in match.groups())
            if dataset not in datasets or field not in datasets[dataset]:
                raise ConversionError(
                    f"Mapping references an unknown dataset/field: {dataset}.{field}"
                )
        for referent in obj.get("referent_mappings", []):
            relation = next(
                (
                    r
                    for r in concepts.get(concept, {}).get("relationships", [])
                    if r["name"] == referent["relationship"]
                ),
                None,
            )
            if relation is None or len(relation.get("roles", [])) != 1:
                raise ConversionError(
                    f"Unknown or non-binary referent relationship: {concept}.{referent['relationship']}"
                )
            check_object(referent, relation["roles"][0]["concept"])

    mapped = set()
    for cm in mapping["concept_mappings"]:
        concept = cm["concept"]
        if concept not in concepts or concept in mapped:
            raise ConversionError(f"Unknown/duplicate concept mapping: {concept}")
        mapped.add(concept)
        for obj in cm.get("object_mappings", []):
            check_object(obj, concept)

        def check_links(nodes, depth=1):
            for link in nodes:
                check_object(link["object_mapping"], concept)
                if "relationship" in link:
                    relation = next(
                        (
                            r
                            for r in concepts[concept].get("relationships", [])
                            if r["name"] == link["relationship"]
                        ),
                        None,
                    )
                    if relation is None or len(relation.get("roles", [])) + 1 != depth:
                        raise ConversionError(
                            f"Unknown relationship or mapping arity: {concept}.{link['relationship']}"
                        )
                    expected = (
                        relation["roles"][-1]["concept"] if depth > 1 else concept
                    )
                    if link["object_mapping"].get("concept", concept) != expected:
                        raise ConversionError(
                            f"Mapping role does not match {concept}.{link['relationship']}"
                        )
                check_links(link.get("children", []), depth + 1)

        check_links(cm.get("link_mappings", []))


def build_ontology_documents(model, *, semantic_model_iri=None, ontology_iri=None):
    """Project dataset entities, field properties, data joins and native type edges."""
    _validate(model)
    metadata = _metadata(model, "model")
    remainder = metadata["remainder"] if metadata else {}
    native_entities = {}
    for bucket, entities in remainder.items():
        for entity in entities:
            if "entityIdentifier" in entity and bucket not in (
                "relationships",
                "dataBindings",
            ):
                key = _identity_key(_identity(entity["entityIdentifier"]))
                if key in native_entities:
                    raise ConversionError(
                        f"Duplicate native ontology entity identity: {key}"
                    )
                native_entities[key] = (bucket, entity)
    concepts, records, by_identity, by_dataset = {}, [], {}, {}
    taken = {n.casefold() for n in BUILTINS}

    def add_concept(identity, bucket, entity, dataset=None):
        key = _identity_key(identity)
        if key in by_identity:
            return by_identity[key]
        name = _unique("_".join(identity[:2]), taken)
        component = {"concept": name, "type": "EntityType", "extends": ["Any"]}
        description = (
            dataset.get("description") if dataset else entity.get("description")
        )
        if isinstance(description, str):
            component["description"] = description
        concepts[name] = component
        by_identity[key] = name
        records.append(
            {
                "concept": name,
                "bucket": bucket,
                "identity": list(identity),
                **({"dataset": dataset["name"]} if dataset else {}),
            }
        )
        return name

    for dataset in model["datasets"]:
        native = _metadata(dataset, "dataset")
        identity = (
            _identity(native["entity"]["entityIdentifier"])
            if native
            else ("ossie", dataset["name"], "OBJECT")
        )
        name = add_concept(
            identity,
            native["bucket"] if native else "objects",
            native["entity"] if native else {},
            dataset,
        )
        by_dataset[dataset["name"]] = name
    # Systems have conceptual identity, but no fabricated row population.
    for key, (bucket, entity) in native_entities.items():
        if bucket == "informationSystems":
            add_concept(_identity(entity["entityIdentifier"]), bucket, entity)
    type_edges = [
        r
        for r in remainder.get("relationships", [])
        if r.get("relationshipType") == "TYPE_TO_TYPE"
    ]
    unmapped_edges = [
        r
        for r in remainder.get("relationships", [])
        if r.get("relationshipType") == "INSTANCE_TO_INSTANCE"
    ]
    if metadata:
        identities = [_identity_key(tuple(i)) for i in metadata["relationship_order"]]
        if len(identities) != len(set(identities)):
            raise ConversionError("Duplicate native relationship identity")
    for edge in type_edges + unmapped_edges:
        for side in ("source", "target"):
            identity = _identity(edge.get(side))
            key = _identity_key(identity)
            if key not in by_identity:
                if key not in native_entities:
                    raise ConversionError(
                        f"Unresolved ontology relationship {side}: {_name(identity)}"
                    )
                bucket, entity = native_entities[key]
                add_concept(identity, bucket, entity)
    fields, identifiers, rel_names = {}, {}, {c: set() for c in concepts}
    field_records = []
    for dataset in model["datasets"]:
        concept = by_dataset[dataset["name"]]
        component = concepts[concept]
        attrs = {}
        keys = dataset.get("primary_key") or next(
            iter(dataset.get("unique_keys", [])), []
        )
        for field in dataset.get("fields", []):
            value_type = VALUE_TYPES.get(field.get("datatype"))
            if not value_type:
                warnings.warn(
                    f"No ontology value type for {dataset['name']}.{field['name']}; retained in core",
                    stacklevel=2,
                )
                continue
            name = _unique("field_" + field["name"], rel_names[concept])
            relation = {
                "name": name,
                "roles": [{"concept": value_type}],
                "multiplicity": "OneToOne" if keys == [field["name"]] else "ManyToOne",
                "verbalizes": [f"{{{concept}}} has {field['name']} {{{value_type}}}"],
            }
            if isinstance(field.get("description"), str):
                relation["description"] = field["description"]
            component.setdefault("relationships", []).append(relation)
            attrs[field["name"]] = (name, value_type)
            field_records.append(
                {
                    "concept": concept,
                    "name": name,
                    "dataset": dataset["name"],
                    "field": field["name"],
                }
            )
        fields[dataset["name"]] = attrs
        if keys and all(k in attrs for k in keys):
            component["identify_by"] = [attrs[k][0] for k in keys]
            identifiers[dataset["name"]] = keys
        else:
            warnings.warn(
                f"{dataset['name']} has no representable key; ontology concept has no row mapping",
                stacklevel=2,
            )

    def objects(dataset_name, columns=None):
        keys = identifiers.get(dataset_name)
        if not keys:
            return None
        columns = columns or {k: _column(dataset_name, k) for k in keys}
        if not all(k in columns for k in keys):
            return None
        return {
            "referent_mappings": [
                {"relationship": fields[dataset_name][k][0], "expression": columns[k]}
                for k in keys
            ]
        }

    cms = {}
    for dataset in model["datasets"]:
        obj = objects(dataset["name"])
        if obj is None:
            continue
        concept = by_dataset[dataset["name"]]
        children = [
            {
                "object_mapping": {
                    "concept": value_type,
                    "expression": _column(dataset["name"], field),
                },
                "relationship": name,
            }
            for field, (name, value_type) in fields[dataset["name"]].items()
            if field not in identifiers[dataset["name"]]
        ]
        cms[concept] = {
            "concept": concept,
            "object_mappings": [obj],
            "link_mappings": [{"object_mapping": deepcopy(obj), "children": children}],
        }
    join_records = []
    for join in model.get("relationships", []):
        source, target = by_dataset[join["from"]], by_dataset[join["to"]]
        name = _unique("link_" + join["name"], rel_names[source])
        native = _metadata(join, "relationship")
        one_to_one = (
            native
            and native["entity"].get("cardinality") == "ONE_TO_ONE"
            and _core(join) == native["projection"]
        )
        relation = {
            "name": name,
            "roles": [{"concept": target, "name": "target"}],
            "multiplicity": "OneToOne" if one_to_one else "ManyToOne",
            "verbalizes": [f"{{{source}}} relates to {{{target}:target}}"],
        }
        concepts[source].setdefault("relationships", []).append(relation)
        join_records.append({"concept": source, "name": name, "join": join["name"]})
        target_obj = objects(
            join["to"],
            {
                b: _column(join["from"], a)
                for a, b in zip(join["from_columns"], join["to_columns"])
            },
        )
        if source in cms and target_obj:
            cms[source]["link_mappings"][0]["children"].append(
                {
                    "object_mapping": {"concept": target, **target_obj},
                    "relationship": name,
                }
            )
        else:
            warnings.warn(
                f"{join['name']} has no key-based ontology link mapping; concept relationship retained",
                stacklevel=2,
            )
    for edge in unmapped_edges:
        source = by_identity[_identity_key(_identity(edge["source"]))]
        target = by_identity[_identity_key(_identity(edge["target"]))]
        cardinality = edge.get("cardinality")
        if cardinality == "ONE_TO_MANY":
            source, target = target, source
        name = _unique(
            "link_" + _name(_identity(edge["entityIdentifier"])), rel_names[source]
        )
        relation = {
            "name": name,
            "roles": [{"concept": target, "name": "target"}],
            "verbalizes": [f"{{{source}}} relates to {{{target}:target}}"],
        }
        if cardinality in ("MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE"):
            relation["multiplicity"] = (
                "OneToOne" if cardinality == "ONE_TO_ONE" else "ManyToOne"
            )
        if isinstance(edge.get("description"), str):
            relation["description"] = edge["description"]
        concepts[source].setdefault("relationships", []).append(relation)
        warnings.warn(
            f"{name} has no portable key/junction mapping; concept relationship retained",
            stacklevel=2,
        )
    type_records = []
    for edge in type_edges:
        source = by_identity[_identity_key(_identity(edge["source"]))]
        target = by_identity[_identity_key(_identity(edge["target"]))]
        name = _unique(
            "type_" + _name(_identity(edge["entityIdentifier"])), rel_names[source]
        )
        relation = {
            "name": name,
            "roles": [{"concept": target, "name": "target"}],
            "verbalizes": [f"{{{source}}} relates to {{{target}:target}}"],
        }
        if isinstance(edge.get("description"), str):
            relation["description"] = edge["description"]
        # Preserve native type-edge cardinality without assigning row multiplicity.
        concepts[source].setdefault("relationships", []).append(relation)
        type_records.append(
            {
                "concept": source,
                "name": name,
                "identity": list(_identity(edge["entityIdentifier"])),
            }
        )
    ontology = {
        "version": model["version"],
        "name": model["name"] + "_ontology",
        "ontology": list(concepts.values()),
    }
    mapping = {
        "version": model["version"],
        "name": model["name"] + "_mapping",
        "ontology_ref": {"name": ontology["name"]},
        "semantic_model_ref": {"name": model["name"]},
        "concept_mappings": list(cms.values()),
    }
    if semantic_model_iri:
        mapping["semantic_model_ref"]["iri"] = semantic_model_iri
    if ontology_iri:
        mapping["ontology_ref"]["iri"] = ontology_iri
    mapping["custom_extensions"] = [
        _extension(
            "ontology",
            projection=ontology,
            concept_mappings=mapping["concept_mappings"],
            concepts=records,
            fields=field_records,
            joins=join_records,
            type_relationships=type_records,
            data_projection=_data_projection(model),
        )
    ]
    validate_ontology_documents(model, ontology, mapping)
    return ontology, mapping


def celonis_to_ossie_bundle(
    pig, *, name=None, semantic_model_iri=None, ontology_iri=None
):
    """Return three standard documents, with native fidelity metadata in extensions."""
    model = celonis_to_ossie(pig, name=name)
    ontology, mapping = build_ontology_documents(
        model,
        semantic_model_iri=semantic_model_iri,
        ontology_iri=ontology_iri,
    )
    return {"semantic_model": model, "ontology": ontology, "mapping": mapping}


def apply_ontology_documents(
    pig, model, ontology, mapping, *, namespace="ossie", package_key=None
):
    """Apply concept/property descriptions and binary type-edge edits to Celonis JSON.

    Row structure and joins are authored in the core model. The mapping extension
    ties ontology names to native identities without guessing from display names.
    """
    validate_ontology_documents(model, ontology, mapping)
    metadata = _metadata(mapping, "ontology")
    if metadata is None:
        raise ConversionError(
            "Ontology export requires CELONIS mapping metadata; use build_ontology_documents for a new core model"
        )
    if _data_projection(model) != metadata["data_projection"]:
        raise ConversionError(
            "Core fields, keys or joins changed; regenerate the ontology/mapping or use --core-only"
        )
    if mapping["concept_mappings"] != metadata["concept_mappings"]:
        raise ConversionError(
            "Editing mapping expressions is not supported; edit the core model and regenerate its ontology/mapping"
        )
    before = {c["concept"]: c for c in metadata["projection"]["ontology"]}
    after = {c["concept"]: c for c in ontology["ontology"]}
    if set(before) != set(after):
        raise ConversionError(
            "Add, remove or rename dataset concepts in the core model, then regenerate the ontology/mapping"
        )
    if {
        k: v
        for k, v in ontology.items()
        if k not in ("ontology", "name", "description", "ai_context")
    } != {
        k: v
        for k, v in metadata["projection"].items()
        if k not in ("ontology", "name", "description", "ai_context")
    } or any(
        k in ontology for k in ("description", "ai_context")
    ):
        raise ConversionError(
            "Ontology-level rules, context and annotations cannot be translated to Celonis JSON"
        )
    records = {r["concept"]: r for r in metadata["concepts"]}
    entities = {}
    for bucket, values in pig.items():
        if bucket in ("relationships", "dataBindings"):
            continue
        for entity in values:
            if "entityIdentifier" in entity:
                entities[_identity_key(_identity(entity["entityIdentifier"]))] = entity
    datasets = {d["name"]: d for d in model["datasets"]}
    current_identity = {}
    for concept, record in records.items():
        if "dataset" in record:
            dataset = datasets.get(record["dataset"])
            if dataset is None:
                raise ConversionError(
                    "Ontology mapping is stale after a dataset deletion; regenerate the companions"
                )
            native = _metadata(dataset, "dataset")
            identity = (
                _identity(native["entity"]["entityIdentifier"])
                if native
                else (namespace, dataset["name"], "OBJECT")
            )
        else:
            identity = tuple(record["identity"])
        current_identity[concept] = identity
        entity = entities.get(_identity_key(identity))
        if entity is None:
            raise ConversionError(f"Missing Celonis entity for ontology concept: {concept}")
        stripped = lambda c: {
            k: v for k, v in c.items() if k not in ("description", "relationships")
        }
        if stripped(before[concept]) != stripped(after[concept]):
            raise ConversionError(
                f"Unsupported identity, type, inheritance or rule edit in {concept}; edit core fields/keys instead"
            )
        if before[concept].get("description") != after[concept].get("description"):
            old = before[concept].get("description")
            new = after[concept].get("description")
            if entity.get("description") not in (old, new):
                raise ConversionError(
                    f"Conflicting core and ontology descriptions for {concept}"
                )
            entity["description"] = new

    relations_before = {
        (c, r["name"]): r for c, v in before.items() for r in v.get("relationships", [])
    }
    relations_after = {
        (c, r["name"]): r for c, v in after.items() for r in v.get("relationships", [])
    }
    type_records = {
        (r["concept"], r["name"]): r for r in metadata["type_relationships"]
    }
    field_records = {(r["concept"], r["name"]): r for r in metadata["fields"]}
    for key, relation in relations_before.items():
        if key in type_records:
            continue
        edited = relations_after.get(key)
        if edited is None:
            raise ConversionError(
                f"Delete field/data-join relationships in the core model: {key}"
            )
        without_description = lambda r: {
            k: v for k, v in r.items() if k != "description"
        }
        if without_description(edited) != without_description(relation):
            raise ConversionError(
                f"Edit field/data-join structure in the core model: {key}"
            )
        if edited.get("description") != relation.get("description"):
            record = field_records.get(key)
            if record is None:
                raise ConversionError(
                    f"Data-join description edits are not supported: {key}"
                )
            identity = current_identity[record["concept"]]
            entity = entities[_identity_key(identity)]
            field = next(
                (f for f in entity.get("fields", []) if f["id"] == record["field"]),
                None,
            )
            if field is None:
                raise ConversionError(
                    f"Stale ontology field mapping: {record['field']}"
                )
            new = edited.get("description")
            if field.get("description") not in (relation.get("description"), new):
                raise ConversionError(
                    f"Conflicting core and ontology field descriptions: {key}"
                )
            field["description"] = new
    native_relations = {
        _identity_key(_identity(r["entityIdentifier"])): r for r in pig["relationships"]
    }
    removed = set()
    for key, record in type_records.items():
        identity_key = _identity_key(tuple(record["identity"]))
        if key not in relations_after:
            removed.add(identity_key)
            continue
        relation = relations_after[key]
        original = relations_before[key]
        if relation == original:
            continue
        if {k: v for k, v in relation.items() if k not in ("description", "roles")} != {
            k: v for k, v in original.items() if k not in ("description", "roles")
        }:
            raise ConversionError(
                f"Only description and target edits are supported for native type relationships: {key}"
            )
        roles = relation.get("roles", [])
        if (
            len(roles) != 1
            or roles[0]["concept"] not in records
            or after[roles[0]["concept"]]["type"] != "EntityType"
        ):
            raise ConversionError(
                f"A Celonis type relationship needs one EntityType target: {key}"
            )
        edge = native_relations[identity_key]
        if roles != original.get("roles"):
            target_identity = current_identity[roles[0]["concept"]]
            target = entities[_identity_key(target_identity)]
            edge["target"] = deepcopy(target["entityIdentifier"])
        edge["description"] = relation.get("description")
        if package_key:
            edge["authoringOrigin"] = {
                **(edge.get("authoringOrigin") or {}),
                "packageKey": package_key,
            }
    pig["relationships"] = [
        r
        for r in pig["relationships"]
        if _identity_key(_identity(r["entityIdentifier"])) not in removed
    ]
    for key in sorted(relations_after.keys() - relations_before.keys()):
        source, name = key
        relation = relations_after[key]
        roles = relation.get("roles", [])
        if (
            len(roles) != 1
            or roles[0]["concept"] not in records
            or after[roles[0]["concept"]]["type"] != "EntityType"
        ):
            raise ConversionError(
                f"A new Celonis type relationship needs one EntityType target: {key}"
            )
        if set(relation) - {"name", "roles", "description", "verbalizes"}:
            raise ConversionError(
                "New type relationships cannot translate multiplicity, rules or derivations"
            )
        identity = (namespace, name, "RELATIONSHIP")
        if _identity_key(identity) in native_relations:
            raise ConversionError(
                f"New ontology relationship identity collision: {name}"
            )
        entity_identity = {
            "namespace": namespace,
            "name": name,
            "entityType": "RELATIONSHIP",
        }
        edge = {
            "entityIdentifier": entity_identity,
            "pigEntityType": "RELATIONSHIP",
            "authoringOrigin": {
                "packageKey": package_key or model["name"],
                "assetKey": name,
            },
            "relationshipType": "TYPE_TO_TYPE",
            "source": deepcopy(
                entities[_identity_key(current_identity[source])]["entityIdentifier"]
            ),
            "target": deepcopy(
                entities[_identity_key(current_identity[roles[0]["concept"]])][
                    "entityIdentifier"
                ]
            ),
            "description": relation.get("description"),
            "attributes": {"apache_ossie": json.dumps(relation, ensure_ascii=False)},
        }
        pig["relationships"].append(edge)
        native_relations[_identity_key(identity)] = edge
    return pig
