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

"""File-based Celonis JSON ↔ Ossie core conversion."""

import json
import re
import warnings
from copy import deepcopy
from urllib.parse import quote

from ossie import OssieDocument

VENDOR = "CELONIS"
FORMAT_VERSION = 1
TYPES = {
    "STRING": "String",
    "INTEGER": "Integer",
    "LONG": "Integer",
    "DECIMAL": "Decimal",
    "DOUBLE": "Float",
    "FLOAT": "Float",
    "BOOLEAN": "Boolean",
    "DATE": "Date",
    "TIME": "Time",
    "TIMESTAMP": "DateTime",
}
REVERSE_TYPES = {v: k for k, v in TYPES.items()}
REVERSE_TYPES.update({"Integer": "INTEGER", "Float": "DOUBLE"})


class ConversionError(ValueError):
    """An input cannot be represented by this first-pass converter."""


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"{path} must be a non-empty string")
    return value


def _identity(entity):
    if not isinstance(entity, dict):
        raise ConversionError("An entity reference must be an object")
    return tuple(
        _text(entity.get(k), f"entityIdentifier.{k}")
        for k in ("namespace", "name", "entityType")
    )


def _identity_key(identity):
    return tuple(component.casefold() for component in identity)


def _name(identity):
    # Escaped components and a slash prevent collisions between namespaces.
    return "/".join(quote(x, safe="") for x in identity[:2])


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _binding_key(binding):
    identity = binding.get("entityIdentifier") or binding
    return (
        *_identity(binding["entityRef"]),
        identity.get("namespace"),
        identity.get("name"),
    )


def _expression(text):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}


def _extension(kind, **data):
    return {
        "vendor_name": VENDOR,
        "data": json.dumps(
            {"format_version": FORMAT_VERSION, "kind": kind, **data},
            ensure_ascii=False,
            allow_nan=False,
        ),
    }


def _metadata(node, kind):
    found = []
    for extension in node.get("custom_extensions", []):
        if extension["vendor_name"] != VENDOR:
            continue
        try:
            data = json.loads(extension["data"])
        except (ValueError, TypeError) as exc:
            raise ConversionError("Invalid CELONIS extension JSON") from exc
        if isinstance(data, dict) and data.get("kind") == kind:
            if data.get("format_version") != FORMAT_VERSION:
                raise ConversionError("Unsupported CELONIS extension format_version")
            found.append(data)
    if len(found) > 1:
        raise ConversionError(f"Duplicate CELONIS {kind} extensions")
    return found[0] if found else None


def _core(node):
    return {k: deepcopy(v) for k, v in node.items() if k != "custom_extensions"}


def _extra_extensions(node, kind):
    native = _metadata(node, kind)
    return [
        deepcopy(e)
        for e in node.get("custom_extensions", [])
        if not (
            native and e["vendor_name"] == VENDOR and json.loads(e["data"]) == native
        )
    ]


def _row_projection(dataset):
    return [(f["name"], f["expression"]) for f in dataset.get("fields", [])]


def _validate(model):
    # The shared parser rejects legacy wrappers, unknown fields, nulls and invalid dialects.
    try:
        OssieDocument.from_ossie_json(json.dumps(model, allow_nan=False))
    except ValueError as exc:
        raise ConversionError(f"Invalid Ossie document: {exc}") from exc
    datasets = {}
    for dataset in model["datasets"]:
        name = dataset["name"]
        if name in datasets:
            raise ConversionError(f"Duplicate dataset name: {name}")
        fields = [f["name"] for f in dataset.get("fields", [])]
        if len(fields) != len(set(fields)):
            raise ConversionError(f"Duplicate field name in {name}")
        for key in [dataset.get("primary_key", [])] + dataset.get("unique_keys", []):
            if not set(key) <= set(fields):
                raise ConversionError(f"Key refers to an undeclared field in {name}")
        datasets[name] = set(fields)
    names = set()
    for relationship in model.get("relationships", []):
        if relationship["name"] in names:
            raise ConversionError(
                f"Duplicate relationship name: {relationship['name']}"
            )
        names.add(relationship["name"])
        if len(relationship["from_columns"]) != len(relationship["to_columns"]):
            raise ConversionError("Relationship key arrays must have equal lengths")
        for side, columns in [("from", "from_columns"), ("to", "to_columns")]:
            if relationship[side] not in datasets:
                raise ConversionError(
                    f"Unknown relationship dataset: {relationship[side]}"
                )
            if not set(relationship[columns]) <= datasets[relationship[side]]:
                raise ConversionError(
                    f"Unknown relationship column in {relationship['name']}"
                )


def _array(document, key):
    value = document.get(key, [])
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise ConversionError(f"{key} must be an array of objects")
    return value


def _source(bindings, identity):
    sql = [_text(b.get("sql"), "dataBinding.sql") for b in bindings]
    if len(sql) == 1:
        return sql[0]
    if sql:
        dialects = {b.get("dialect", "STARROCKS") for b in bindings}
        if len(dialects) != 1:
            raise ConversionError(f"Mixed binding dialects for {_name(identity)}")
        return " UNION ALL ".join(f"({s})" for s in sql)
    warnings.warn(
        f"{_name(identity)} has no binding; using its logical name as source",
        stacklevel=3,
    )
    return _name(identity)


def celonis_to_ossie(pig, *, name=None):
    """Project objects, event sources and supported data joins; preserve all other content."""
    if not isinstance(pig, dict) or not any(
        k in pig for k in ("objects", "eventSources", "events")
    ):
        raise ConversionError("Expected a Celonis JSON object with entity arrays")
    # Validate every container, including unconverted ones, before preserving them.
    for key in pig:
        _array(pig, key)
    bindings = {}
    for binding in _array(pig, "dataBindings"):
        identity = _identity(binding.get("entityRef"))
        bindings.setdefault(_identity_key(identity), []).append(binding)
    model = {
        "version": "0.2.0.dev0",
        "name": name or "celonis_model",
        "datasets": [],
        "relationships": [],
    }
    dataset_names = {}
    packages = set()
    for bucket, entity_type in [
        ("objects", "OBJECT"),
        ("eventSources", "EVENT_SOURCE"),
        ("events", "EVENT"),
    ]:
        for entity in _array(pig, bucket):
            identity = _identity(entity.get("entityIdentifier"))
            if identity[2] != entity_type:
                raise ConversionError(
                    f"Unexpected entity type in {bucket}: {identity[2]}"
                )
            dataset_name = _name(identity)
            if dataset_name.casefold() in {
                n.casefold() for n in dataset_names.values()
            }:
                raise ConversionError(f"Dataset identity collision: {dataset_name}")
            dataset_names[_identity_key(identity)] = dataset_name
            packages.add(entity.get("authoringOrigin", {}).get("packageKey"))
            native_bindings = bindings.get(_identity_key(identity), [])
            dataset = {
                "name": dataset_name,
                "source": _source(native_bindings, identity),
                "fields": [],
            }
            for field in _array(entity, "fields"):
                field_name = _text(field.get("id"), "field.id")
                converted = {
                    "name": field_name,
                    "expression": _expression(_quote(field_name)),
                    "datatype": TYPES.get(field.get("dataType"), "Opaque"),
                    "dimension": {},
                }
                if isinstance(field.get("description"), str):
                    converted["description"] = field["description"]
                dataset["fields"].append(converted)
            keys = entity.get("primaryKeys") or []
            if not keys and native_bindings:
                key_lists = [b.get("primaryKeyColumns") or [] for b in native_bindings]
                if any(k != key_lists[0] for k in key_lists):
                    raise ConversionError(
                        f"Inconsistent binding primary keys for {dataset_name}"
                    )
                keys = key_lists[0]
            if keys:
                dataset["primary_key"] = deepcopy(keys)
            if isinstance(entity.get("description"), str):
                dataset["description"] = entity["description"]
            if isinstance(entity.get("agentContext"), str):
                dataset["ai_context"] = entity["agentContext"]
            dataset["custom_extensions"] = [
                _extension(
                    "dataset",
                    bucket=bucket,
                    entity=entity,
                    bindings=native_bindings,
                    projection=_core(dataset),
                )
            ]
            model["datasets"].append(dataset)
    if name is None and len(packages) == 1 and None not in packages:
        model["name"] = next(iter(packages))
    preserved_relationships = []
    relationship_order = []
    for relationship in _array(pig, "relationships"):
        identity = _identity(relationship.get("entityIdentifier"))
        relationship_order.append(identity)
        cardinality = relationship.get("cardinality")
        mappings = relationship.get("foreignKeyMappings") or []
        if (
            relationship.get("relationshipType") != "INSTANCE_TO_INSTANCE"
            or cardinality not in ("MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE")
            or relationship.get("junctionTable")
            or not mappings
        ):
            preserved_relationships.append(relationship)
            continue
        source = _identity_key(_identity(relationship.get("source")))
        target = _identity_key(_identity(relationship.get("target")))
        if source not in dataset_names or target not in dataset_names:
            raise ConversionError(f"Unresolved data relationship: {_name(identity)}")
        converted = {
            "name": _name(identity),
            "from": dataset_names[source],
            "to": dataset_names[target],
            "from_columns": [
                _text(m.get("sourceField", {}).get("id"), "sourceField.id")
                for m in mappings
            ],
            "to_columns": [
                _text(m.get("targetField", {}).get("id"), "targetField.id")
                for m in mappings
            ],
        }
        if cardinality == "ONE_TO_MANY":
            converted["from"], converted["to"] = converted["to"], converted["from"]
            converted["from_columns"], converted["to_columns"] = (
                converted["to_columns"],
                converted["from_columns"],
            )
        if isinstance(relationship.get("agentContext"), str):
            converted["ai_context"] = relationship["agentContext"]
        converted["custom_extensions"] = [
            _extension("relationship", entity=relationship, projection=_core(converted))
        ]
        model["relationships"].append(converted)
    mapped = {"objects", "events", "eventSources", "relationships", "dataBindings"}
    remainder = {k: v for k, v in pig.items() if k not in mapped}
    remainder["relationships"] = preserved_relationships
    remainder["dataBindings"] = [
        b
        for b in pig.get("dataBindings", [])
        if _identity_key(_identity(b["entityRef"])) not in dataset_names
    ]
    model["custom_extensions"] = [
        _extension(
            "model",
            remainder=remainder,
            original_keys=list(pig),
            relationship_order=relationship_order,
            binding_order=[_binding_key(b) for b in pig.get("dataBindings", [])],
        )
    ]
    if preserved_relationships or pig.get("metrics"):
        warnings.warn(
            f"Preserved {len(preserved_relationships)} non-core relationships and "
            f"{len(pig.get('metrics', []))} PQL metrics in CELONIS extensions",
            stacklevel=2,
        )
    _validate(model)
    return model


def _sql(expression):
    for dialect in ("OSSIE_SQL_2026", "ANSI_SQL"):
        for entry in expression["dialects"]:
            if entry["dialect"] == dialect:
                return _text(entry["expression"], "field expression")
    raise ConversionError(
        "A field needs ANSI_SQL or OSSIE_SQL_2026 for Celonis SQL generation"
    )


def _binding_sql(dataset):
    source = dataset["source"]
    if re.match(r"\s*(SELECT\b|WITH\b|\()", source, re.I):
        source = f"({source}) AS ossie_source"
    fields = dataset.get("fields", [])
    projection = (
        ", ".join(f"{_sql(f['expression'])} AS {_quote(f['name'])}" for f in fields)
        or "*"
    )
    return f"SELECT {projection} FROM {source}"


def _agent_context(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def ossie_to_celonis(
    model, *, package_key=None, namespace="ossie", ontology=None, mapping=None
):
    """Write Celonis JSON for offline file conversion."""
    if (ontology is None) != (mapping is None):
        raise ConversionError("Supply both ontology and mapping documents")
    _validate(model)
    if model.get("metrics") or any(d.get("metrics") for d in model["datasets"]):
        raise ConversionError(
            "SQL metrics cannot be translated to PQL in this first version; "
            "native PQL metrics round-trip through CELONIS extensions"
        )
    metadata = _metadata(model, "model")
    pig = deepcopy(metadata["remainder"]) if metadata else {}
    for bucket in (
        "objects",
        "events",
        "eventSources",
        "relationships",
        "dataBindings",
    ):
        pig.setdefault(bucket, [])
    identities = {}
    for dataset in model["datasets"]:
        native = _metadata(dataset, "dataset")
        if native and _core(dataset) == native["projection"]:
            entity, bindings = deepcopy(native["entity"]), deepcopy(native["bindings"])
        else:
            if native and dataset["name"] != native["projection"]["name"]:
                raise ConversionError(
                    "Renaming a native dataset requires updating preserved Celonis references"
                )
            entity = (
                deepcopy(native["entity"])
                if native
                else {
                    "entityIdentifier": {
                        "namespace": _text(namespace, "namespace"),
                        "name": dataset["name"],
                        "entityType": "OBJECT",
                    },
                    "authoringOrigin": {
                        "packageKey": package_key or model["name"],
                        "assetKey": dataset["name"],
                    },
                    "displayName": dataset["name"],
                }
            )
            entity["pigEntityType"] = entity["entityIdentifier"]["entityType"]
            entity["description"] = dataset.get("description")
            entity["agentContext"] = _agent_context(dataset.get("ai_context"))
            old_fields = {f["id"]: f for f in entity.get("fields", [])}
            entity["fields"] = []
            for field in dataset.get("fields", []):
                converted = deepcopy(old_fields.get(field["name"], {}))
                datatype = field.get("datatype")
                if datatype == "DateTimeTz" or (
                    datatype == "Opaque" and "dataType" not in converted
                ):
                    raise ConversionError(
                        f"Cannot infer a native Celonis type for {field['name']}: {datatype}"
                    )
                converted.update(
                    id=field["name"],
                    description=field.get("description"),
                    required=converted.get("required", False),
                )
                if (
                    datatype != "Opaque"
                    and TYPES.get(converted.get("dataType")) != datatype
                ):
                    converted["dataType"] = REVERSE_TYPES.get(datatype, "STRING")
                entity["fields"].append(converted)
            keys_changed = not native or dataset.get("primary_key") != native[
                "projection"
            ].get("primary_key")
            if keys_changed:
                entity["primaryKeys"] = dataset.get("primary_key", [])
            if (
                native
                and dataset["source"] == native["projection"]["source"]
                and _row_projection(dataset) == _row_projection(native["projection"])
            ):
                bindings = deepcopy(native["bindings"])
            else:
                bindings = [
                    {
                        "entityIdentifier": {
                            "namespace": entity["entityIdentifier"]["namespace"],
                            "name": entity["entityIdentifier"]["name"] + "-binding",
                            "entityType": "DATA_BINDING",
                        },
                        "authoringOrigin": deepcopy(entity["authoringOrigin"]),
                        "entityRef": deepcopy(entity["entityIdentifier"]),
                        "bindingType": "READ_ONLY",
                        "dialect": "STARROCKS",
                        "sql": _binding_sql(dataset),
                    }
                ]
            if keys_changed:
                for binding in bindings:
                    binding.pop("primaryKeyColumns", None)
            # Do not embed our own native JSON extension again on every edit cycle.
            semantics = _core(dataset)
            extras = _extra_extensions(dataset, "dataset")
            if extras:
                semantics["custom_extensions"] = extras
            entity.setdefault("attributes", None)
            entity["attributes"] = {
                **(entity["attributes"] or {}),
                "apache_ossie": json.dumps(semantics, ensure_ascii=False),
            }
        extras = _extra_extensions(dataset, "dataset")
        if extras and native and _core(dataset) == native["projection"]:
            entity["attributes"] = {
                **(entity.get("attributes") or {}),
                "apache_ossie": json.dumps(
                    {**_core(dataset), "custom_extensions": extras}, ensure_ascii=False
                ),
            }
        if package_key:
            entity["authoringOrigin"]["packageKey"] = package_key
            for binding in bindings:
                binding["authoringOrigin"] = {
                    **(binding.get("authoringOrigin") or {}),
                    "packageKey": package_key,
                }
        identity = entity["entityIdentifier"]
        identities[dataset["name"]] = identity
        bucket = native["bucket"] if native else "objects"
        pig[bucket].append(entity)
        pig["dataBindings"].extend(bindings)
    for relationship in model.get("relationships", []):
        native = _metadata(relationship, "relationship")
        if native and _core(relationship) == native["projection"]:
            entity = deepcopy(native["entity"])
        else:
            if native and relationship["name"] != native["projection"]["name"]:
                raise ConversionError(
                    "Renaming a native relationship requires updating preserved Celonis references"
                )
            entity = (
                deepcopy(native["entity"])
                if native
                else {
                    "entityIdentifier": {
                        "namespace": namespace,
                        "name": relationship["name"],
                        "entityType": "RELATIONSHIP",
                    },
                    "authoringOrigin": {
                        "packageKey": package_key or model["name"],
                        "assetKey": relationship["name"],
                    },
                }
            )
            join_changed = not native or any(
                relationship[k] != native["projection"][k]
                for k in ("from", "to", "from_columns", "to_columns")
            )
            entity["agentContext"] = _agent_context(relationship.get("ai_context"))
            if join_changed:
                entity.update(
                    pigEntityType="RELATIONSHIP",
                    relationshipType="INSTANCE_TO_INSTANCE",
                    cardinality="MANY_TO_ONE",
                    source=identities[relationship["from"]],
                    target=identities[relationship["to"]],
                    foreignKeyMappings=[
                        {"sourceField": {"id": a}, "targetField": {"id": b}}
                        for a, b in zip(
                            relationship["from_columns"], relationship["to_columns"]
                        )
                    ],
                )
        extras = _extra_extensions(relationship, "relationship")
        if extras:
            entity["attributes"] = {
                **(entity.get("attributes") or {}),
                "apache_ossie": json.dumps(
                    {**_core(relationship), "custom_extensions": extras}
                ),
            }
        if package_key:
            entity["authoringOrigin"]["packageKey"] = package_key
        pig["relationships"].append(entity)
    if metadata:
        order = {
            tuple(identity): i
            for i, identity in enumerate(metadata["relationship_order"])
        }
        pig["relationships"].sort(
            key=lambda r: order.get(_identity(r["entityIdentifier"]), len(order))
        )
        binding_order = {
            tuple(key): i for i, key in enumerate(metadata["binding_order"])
        }
        pig["dataBindings"].sort(
            key=lambda b: binding_order.get(_binding_key(b), len(binding_order))
        )
        # Preserve absent versus present-empty buckets in an unedited round trip.
        for bucket in list(pig):
            if bucket not in metadata["original_keys"] and not pig[bucket]:
                del pig[bucket]
    model_extras = _extra_extensions(model, "model")
    if (
        not metadata
        or model_extras
        or model.get("description")
        or model.get("ai_context")
    ):
        header = {
            k: deepcopy(v)
            for k, v in model.items()
            if k not in ("datasets", "relationships", "metrics", "custom_extensions")
        }
        if model_extras:
            header["custom_extensions"] = model_extras
        first = next(
            e
            for bucket in ("objects", "eventSources", "events")
            for e in pig.get(bucket, [])
        )
        first["attributes"] = {
            **(first.get("attributes") or {}),
            "apache_ossie_model": json.dumps(header),
        }
    if ontology is not None:
        from .ontology import apply_ontology_documents

        pig = apply_ontology_documents(
            pig, model, ontology, mapping, namespace=namespace, package_key=package_key
        )
    return pig
