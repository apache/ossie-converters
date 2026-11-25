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

"""Convert between Apache Ossie and Starburst Context Studio YAML."""

import json
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

import jsonschema
import yaml
from sqlglot import exp

from ._sql import (
    ConversionError,
    identifier,
    ossie_expression,
    parse_expression,
    parse_reference,
    parse_source,
    physical_expression,
    reference,
    render,
    select_expression,
    source_sql,
)

VENDOR = "STARBURST"
RELATIONS = ("tables", "views", "materializedViews")
SEMANTICS = (
    "terms",
    "dimensions",
    "metrics",
    "constraints",
    "rules",
    "sampleQueries",
    "businessContexts",
)
ANNOTATION = ("summary", "externalLinks", "relationships")
SQL_ANNOTATION = ("name", "description", "synonyms", *ANNOTATION)
NATIVE_HEADER = {"apiVersion": "v1", "kind": "SemanticModel"}
SEMANTIC_NAME = re.compile(r"[a-z][a-z0-9_]*")


def convert_ossie_to_starburst(text, *, domain=None, skip_unsupported_metrics=False):
    """Convert a flat Ossie YAML or JSON document to Context Studio YAML."""
    model = _load(text)
    _validate_ossie(model)
    model_extra = _extension(model)
    catalogs = _Catalogs(_entries(model_extra, "catalogs"))
    semantics = _mapping(model_extra.get("semantics", {}), "STARBURST semantics")
    datasets = {}
    physical_sources = set()
    for dataset in model["datasets"]:
        name = dataset["name"]
        relation = parse_source(dataset["source"])
        if name.casefold() in datasets or relation in physical_sources:
            raise ConversionError(
                f"{name}: duplicate dataset name or physical source {dataset['source']!r}"
            )
        physical_sources.add(relation)
        datasets[name.casefold()] = (
            dataset,
            relation,
            _export_dataset(dataset, relation, catalogs, semantics),
        )

    _export_metrics(model, datasets, semantics, skip_unsupported_metrics)

    joins = [_export_join(join, datasets) for join in model.get("relationships", [])]
    joins += _entries(model_extra, "joins")
    _check_joins(joins)
    _only_keys(semantics, SEMANTICS, "STARBURST semantics")
    for kind in SEMANTICS:
        _check_semantic_names(_entries(semantics, kind), kind)

    if "description" in model or "ai_context" in model:
        _warn(
            "model description and AI context have no Context Studio model-level equivalent"
        )
    output = {**NATIVE_HEADER, "catalogs": catalogs.entries}
    if joins:
        output["joins"] = joins
    if semantics or domain is not None or "domain" in model_extra:
        selected_domain = (
            domain if domain is not None else model_extra.get("domain", model["name"])
        )
        output["semantics"] = {
            "domain": _string(selected_domain, "semantics.domain"),
            **semantics,
        }
    return _dump(output)


def convert_starburst_to_ossie(text, *, model_name=None):
    """Convert Context Studio YAML or JSON to flat Ossie YAML."""
    native = _load(text)
    _only_keys(
        native, (*NATIVE_HEADER, "catalogs", "joins", "semantics"), "SemanticModel"
    )
    if any(native.get(key) != value for key, value in NATIVE_HEADER.items()):
        raise ConversionError(
            "invalid apiVersion or kind for Starburst Context Studio YAML"
        )
    raw_semantics = native.get("semantics")
    semantics = _mapping(
        raw_semantics if raw_semantics is not None else {}, "semantics"
    )
    _only_keys(semantics, ("domain", *SEMANTICS), "semantics")
    domain = (
        _string(semantics.get("domain"), "semantics.domain")
        if raw_semantics is not None
        else None
    )

    model = {
        "version": _schema()["properties"]["version"]["const"],
        "name": _string(
            model_name if model_name is not None else domain or "context_studio",
            "model name",
        ),
        "datasets": [],
    }
    datasets = {}
    used_names = set()

    def dataset_for(relation, kind="tables", annotation=None):
        if relation in datasets:
            if annotation is not None:
                raise ConversionError(f"duplicate relation {reference(relation)}")
            return datasets[relation]
        name = _unique_name(relation, used_names)
        dataset = {"name": name, "source": source_sql(relation), "fields": []}
        model["datasets"].append(dataset)
        datasets[relation] = dataset
        if annotation is not None:
            _only_keys(annotation, (*SQL_ANNOTATION, "columns"), reference(relation))
            _import_annotation(
                annotation, dataset, kind=kind if kind != "tables" else None
            )
            for column in _entries(annotation, "columns"):
                _only_keys(column, SQL_ANNOTATION, "column")
                _import_column(dataset, _name(column), column)
        else:
            _stash(dataset, inferred=True)
        return dataset

    extra = {}
    if domain is not None:
        extra["domain"] = domain
    catalog_annotations = _import_catalogs(native, dataset_for)
    if catalog_annotations:
        extra["catalogs"] = catalog_annotations

    joins = _entries(native, "joins")
    _check_joins(joins)
    relationships = []
    for join in joins:
        imported = _import_join(join, dataset_for)
        if imported is None:
            extra.setdefault("joins", []).append(join)
        else:
            relationships.append(imported)
    if relationships:
        model["relationships"] = relationships

    for kind in SEMANTICS:
        items = _entries(semantics, kind)
        _check_semantic_names(items, kind)
        for entry in items:
            if kind in ("metrics", "dimensions") and _import_semantic(
                kind, entry, dataset_for
            ):
                continue
            extra.setdefault("semantics", {}).setdefault(kind, []).append(entry)

    if not model["datasets"]:
        raise ConversionError(
            "Ossie needs at least one dataset; no relation found in the Context Studio model"
        )
    _stash(model, **extra)
    _validate_ossie(model)
    return _dump(model)


def _export_dataset(dataset, relation, catalogs, semantics):
    extra = _extension(dataset)
    kind = extra.get("kind", "tables")
    if kind not in RELATIONS:
        raise ConversionError(f"{dataset['name']}: invalid relation kind {kind!r}")
    if "primary_key" in dataset or "unique_keys" in dataset:
        _warn(
            f"{dataset['name']}: Context Studio has no primary or unique key declarations"
        )
    annotation = {"name": relation[-1], **_export_annotation(dataset)}
    fields = {}
    for field in dataset.get("fields", []):
        name = field["name"]
        if name.casefold() in fields:
            raise ConversionError(f"{dataset['name']}: duplicate field name {name!r}")
        node = physical_expression(select_expression(field["expression"]), relation)
        fields[name.casefold()] = node
        extra_field = _extension(field)
        if (
            _physical_column(node) == name
            and extra_field.get("kind") != "dimension"
            and "label" not in field
        ):
            column_annotation = _export_annotation(field)
            if column_annotation or not extra_field.get("inferred"):
                annotation.setdefault("columns", []).append(
                    {"name": name, **column_annotation}
                )
        else:
            entry = {
                "name": name,
                "table": reference(relation),
                "sql": render(node),
                **_export_annotation(field, label=True),
            }
            semantics.setdefault("dimensions", []).append(entry)
        if "datatype" in field or "dimension" in field:
            _warn(
                f"{dataset['name']}.{name}: Context Studio has no field datatype or time-role override"
            )
    if len(annotation) > 1 or kind != "tables" or not extra.get("inferred"):
        catalogs.add_relation(relation, kind, annotation)
    return fields


def _export_metrics(model, datasets, semantics, skip_unsupported):
    metrics = [
        (metric, key)
        for key, (dataset, _, _) in datasets.items()
        for metric in dataset.get("metrics", [])
    ]
    metrics.extend((metric, None) for metric in model.get("metrics", []))
    for metric, dataset_key in metrics:
        try:
            entry = _export_metric(metric, datasets, dataset_key)
        except ConversionError as error:
            message = f"{metric['name']}: {error}"
            if not skip_unsupported:
                raise ConversionError(message) from error
            _warn(f"skipping metric {message}")
        else:
            semantics.setdefault("metrics", []).append(entry)


def _export_metric(metric, datasets, dataset_key):
    name = metric["name"]
    expression = select_expression(metric["expression"])
    scope = None
    if dataset_key is None:
        columns = list(expression.find_all(exp.Column))
        if any(not column.table for column in columns):
            raise ConversionError(
                "model metric fields must include a dataset qualifier"
            )
        scopes = {column.table.casefold() for column in columns}
        if not scopes and len(datasets) == 1:
            scopes = set(datasets)
        if len(scopes) != 1 or not scopes <= datasets.keys():
            raise ConversionError("model metric needs exactly one identifiable dataset")
        scope = dataset_key = next(iter(scopes))
    _, relation, fields = datasets[dataset_key]

    def replace(column):
        if column.db or column.catalog:
            raise ConversionError(f"unexpected qualified reference {column.sql()}")
        qualifier = column.table.casefold()
        if scope is None and qualifier == "source":
            if "source" in fields:
                raise ConversionError("field named source shadows the source qualifier")
            return exp.Column(this=column.this.copy())
        if qualifier and qualifier != scope:
            raise ConversionError(f"column {column.sql()} is outside its dataset")
        field = fields.get(column.name.casefold())
        if field is None:
            raise ConversionError(
                f"unknown field {column.name!r}; use source.column for a physical column"
            )
        return (
            field.copy()
            if isinstance(field, exp.Column)
            else exp.Paren(this=field.copy())
        )

    node = expression.transform(
        lambda item: replace(item) if isinstance(item, exp.Column) else item
    )
    if "datatype" in metric:
        _warn(f"{name}: Context Studio has no metric datatype override")
    return {
        "name": name,
        "table": reference(relation),
        "sql": render(node),
        **_export_annotation(metric, label=True),
    }


def _export_join(join, datasets):
    name = join["name"]
    sources = {}
    for side in ("from", "to"):
        dataset = datasets.get(join[side].casefold())
        if dataset is None:
            raise ConversionError(f"{name}: unknown dataset {join[side]!r}")
        _, relation, fields = dataset
        sources[side] = []
        for field in join[f"{side}_columns"]:
            column = _physical_column(fields.get(field.casefold()))
            if column is None:
                raise ConversionError(
                    f"{name}: join field {field!r} must refer to a physical column"
                )
            sources[side].append(reference((*relation, column)))
    if len(sources["from"]) != len(sources["to"]):
        raise ConversionError(f"{name}: join sides have different column counts")
    cardinality = _extension(join).get("cardinality", "MANY_TO_ONE")
    if cardinality == "ONE_TO_MANY":
        sources = {"from": sources["to"], "to": sources["from"]}
    return {
        "name": name,
        **sources,
        "cardinality": cardinality,
        **_export_annotation(join, label=True),
    }


def _import_catalogs(native, dataset_for):
    annotations = []
    for catalog in _entries(native, "catalogs"):
        name = _name(catalog)
        _only_keys(catalog, (*SQL_ANNOTATION, "schemas"), "catalog")
        retained_catalog = _select(catalog, *SQL_ANNOTATION)
        for schema in _entries(catalog, "schemas"):
            _only_keys(schema, (*SQL_ANNOTATION, *RELATIONS), "schema")
            retained_schema = _select(schema, *SQL_ANNOTATION)
            for kind in RELATIONS:
                for relation in _entries(schema, kind):
                    dataset_for((name, _name(schema), _name(relation)), kind, relation)
            if len(retained_schema) > 1 or not any(
                schema.get(kind) for kind in RELATIONS
            ):
                retained_catalog.setdefault("schemas", []).append(retained_schema)
        if len(retained_catalog) > 1 or not catalog.get("schemas"):
            annotations.append(retained_catalog)
    return annotations


def _import_column(dataset, name, annotation=None):
    fields = dataset["fields"]
    if any(field["name"] == name for field in fields):
        if annotation is not None:
            raise ConversionError(f"{dataset['source']}: duplicate column {name!r}")
        return
    field = {"name": name, "expression": {"sql": render(identifier(name))}}
    _import_annotation(
        annotation or {}, field, inferred=True if annotation is None else None
    )
    fields.append(field)


def _import_join(join, dataset_for):
    _only_keys(join, (*SQL_ANNOTATION, "label", "from", "to", "cardinality"), "join")
    name = _name(join)
    cardinality = join.get("cardinality")
    sides = [_join_columns(join, side) for side in ("from", "to")]
    for columns in sides:
        dataset = dataset_for(columns[0][:-1])
        for column in columns:
            _import_column(dataset, column[-1])
    if cardinality not in ("MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE"):
        _warn(
            f"{name}: {cardinality or 'unspecified'} cardinality preserved in the STARBURST extension"
        )
        return None
    if cardinality == "ONE_TO_MANY":
        sides.reverse()
    result = {"name": name}
    for side, columns in zip(("from", "to"), sides):
        dataset = dataset_for(columns[0][:-1])
        result[side] = dataset["name"]
        result[f"{side}_columns"] = [column[-1] for column in columns]
    _import_annotation(join, result, description=False, cardinality=cardinality)
    return result


def _import_semantic(kind, entry, dataset_for):
    _only_keys(entry, (*SQL_ANNOTATION, "label", "table", "sql"), kind)
    name = _name(entry)
    if entry.get("table") is None:
        _warn(f"{name}: unbound {kind} entry preserved in the STARBURST extension")
        return False
    relation = parse_reference(_string(entry["table"], f"{name}.table"), 3)
    dataset = dataset_for(relation)
    if any(
        item["name"].casefold() == name.casefold()
        for key in ("fields", "metrics")
        for item in dataset.get(key, [])
    ):
        _warn(
            f"{name}: name collision on {dataset['source']}; preserved in the STARBURST extension"
        )
        return False
    try:
        sql = parse_expression(_string(entry.get("sql"), f"{name}.sql"))
        if kind == "metrics":
            sql = _import_metric_expression(sql, relation, dataset["fields"])
        else:
            sql = physical_expression(sql, relation)
        expression = ossie_expression(sql)
    except ConversionError as error:
        _warn(f"{name}: {error}; preserved in the STARBURST extension")
        return False
    result = {"name": name, "expression": expression}
    _import_annotation(
        entry, result, kind="dimension" if kind == "dimensions" else None
    )
    dataset.setdefault("fields" if kind == "dimensions" else "metrics", []).append(
        result
    )
    return True


def _import_metric_expression(sql, relation, fields):
    if not any(field["name"].casefold() == "source" for field in fields):
        return physical_expression(sql, relation, qualify=True)

    sql = physical_expression(sql, relation)
    declared = {
        field["name"].casefold()
        for field in fields
        if _extension(field).get("kind") != "dimension"
    }
    for column in sql.find_all(exp.Column):
        if column.name.casefold() not in declared:
            raise ConversionError(
                f"field named source shadows the qualifier for undeclared column {column.sql()}"
            )
    return sql


def _import_annotation(entry, result, *, description=True, **extra):
    if description and entry.get("description") is not None:
        result["description"] = entry["description"]
    if entry.get("synonyms"):
        result["ai_context"] = {"synonyms": entry["synonyms"]}
    keys = ANNOTATION if description else (*ANNOTATION, "description")
    if extra.get("kind") == "dimension" and entry.get("label") is not None:
        result["label"] = entry["label"]
    else:
        keys = (*keys, "label")
    if annotation := _select(entry, *keys):
        extra["annotation"] = annotation
    _stash(result, **{key: value for key, value in extra.items() if value is not None})


def _export_annotation(entry, *, label=False):
    keys = (*ANNOTATION, "description", "label") if label else ANNOTATION
    output = _select(
        _mapping(_extension(entry).get("annotation", {}), "STARBURST annotation"), *keys
    )
    output.update(_select(entry, "description", *(("label",) if label else ())))
    ai_context = entry.get("ai_context", {})
    if isinstance(ai_context, dict) and ai_context.get("synonyms"):
        output["synonyms"] = ai_context["synonyms"]
    if ai_context and (
        not isinstance(ai_context, dict) or ai_context.keys() - {"synonyms"}
    ):
        _warn(
            f"{entry['name']}: only AI context synonyms have a Context Studio annotation equivalent"
        )
    return output


class _Catalogs:
    def __init__(self, entries):
        self.entries = entries
        for catalog in entries:
            _only_keys(catalog, (*SQL_ANNOTATION, "schemas"), "STARBURST catalog")
            if "schemas" in catalog:
                catalog["schemas"] = _entries(catalog, "schemas")
                for schema in catalog["schemas"]:
                    _only_keys(schema, SQL_ANNOTATION, "STARBURST schema")

    def add_relation(self, relation, kind, annotation):
        catalog = self._get_or_create(self.entries, relation[0])
        schema = self._get_or_create(catalog.setdefault("schemas", []), relation[1])
        schema.setdefault(kind, []).append(annotation)

    @staticmethod
    def _get_or_create(entries, name):
        for entry in entries:
            if _name(entry) == name:
                return entry
        entry = {"name": name}
        entries.append(entry)
        return entry


def _join_columns(join, side):
    name = _name(join)
    columns = join.get(side)
    if not isinstance(columns, list) or not columns:
        raise ConversionError(f"{name}.{side} needs one or more column references")
    parts = [
        parse_reference(_string(column, f"{name}.{side} column"), 4)
        for column in columns
    ]
    if any(column[:-1] != parts[0][:-1] for column in parts):
        raise ConversionError(f"{name}.{side} must reference columns of one relation")
    return parts


def _check_joins(joins):
    _check_semantic_names(joins, "joins")
    pairs = set()
    for join in joins:
        left, right = (_join_columns(join, side) for side in ("from", "to"))
        if len(left) != len(right):
            raise ConversionError(
                f"{_name(join)}: join sides have different column counts"
            )
        key = frozenset(zip(left, right))
        if key in pairs or frozenset(zip(right, left)) in pairs:
            raise ConversionError(f"{_name(join)}: duplicate join column pairs")
        pairs.add(key)
        cardinality = join.get("cardinality")
        if cardinality not in (
            None,
            "MANY_TO_ONE",
            "ONE_TO_MANY",
            "ONE_TO_ONE",
            "MANY_TO_MANY",
        ):
            raise ConversionError(f"{_name(join)}: invalid cardinality {cardinality!r}")


def _check_semantic_names(entries, kind):
    seen = set()
    for entry in entries:
        name = _name(entry)
        if not SEMANTIC_NAME.fullmatch(name):
            raise ConversionError(
                f"{kind}: invalid Context Studio name {name!r}; expected [a-z][a-z0-9_]*"
            )
        if name in seen:
            raise ConversionError(f"{kind}: duplicate Context Studio name {name!r}")
        seen.add(name)


def _physical_column(expression):
    if isinstance(expression, exp.Column) and not expression.table:
        return expression.name if expression.this.quoted else expression.name.lower()
    return None


def _unique_name(relation, used_names):
    for name in (relation[-1], "_".join(relation[-2:]), "_".join(relation)):
        if name.casefold() not in used_names:
            used_names.add(name.casefold())
            return name
    stem = "_".join(relation)
    suffix = 2
    while f"{stem}_{suffix}".casefold() in used_names:
        suffix += 1
    name = f"{stem}_{suffix}"
    used_names.add(name.casefold())
    return name


def _extension(entry):
    for extension in entry.get("nonportable_extensions", []):
        if extension["vendor_name"] == VENDOR:
            return extension["data"]
    return {}


def _stash(entry, **extra):
    if extra:
        entry["nonportable_extensions"] = [{"vendor_name": VENDOR, "data": extra}]


def _select(entry, *keys):
    return {key: entry[key] for key in keys if key in entry}


def _name(entry):
    return _string(entry.get("name"), "name")


def _string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ConversionError(f"{path} must be a non-empty string")
    return value


def _mapping(value, path):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConversionError(f"{path} must be a mapping with string keys")
    return value


def _only_keys(value, keys, path):
    if unknown := value.keys() - set(keys):
        raise ConversionError(
            f"{path}: unknown properties {', '.join(sorted(unknown))}"
        )


def _entries(entry, key):
    items = entry.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConversionError(f"{key} must be a list")
    return [_mapping(item, f"{key}[{index}]") for index, item in enumerate(items)]


class _Loader(yaml.SafeLoader):
    yaml_implicit_resolvers: ClassVar[dict] = {
        key: [
            (tag, pattern)
            for tag, pattern in values
            if tag.rsplit(":", 1)[-1] not in ("bool", "timestamp")
        ]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            name = self.construct_object(key, deep=deep)
            if not isinstance(name, str) or name in keys:
                raise ConversionError(
                    f"line {key.start_mark.line + 1}: invalid or duplicate mapping key {name!r}"
                )
            keys.add(name)
        return super().construct_mapping(node, deep)


_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _load(text):
    try:
        document = _mapping(yaml.load(text, Loader=_Loader), "document")
        return json.loads(json.dumps(document, allow_nan=False))
    except (yaml.YAMLError, ValueError, TypeError) as error:
        raise ConversionError(str(error)) from error


def _dump(document):
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


@lru_cache(maxsize=1)
def _schema():
    path = Path(__file__).with_name("ossie-schema.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[3] / "ossie" / "core-spec" / path.name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConversionError(
            "Ossie schema missing; initialize the ossie submodule"
        ) from error


def _validate_ossie(document):
    errors = jsonschema.Draft202012Validator(_schema()).iter_errors(document)
    if error := next(errors, None):
        raise ConversionError(f"Ossie {error.json_path}: {error.message}")


def _warn(message):
    warnings.warn(message, stacklevel=3)
