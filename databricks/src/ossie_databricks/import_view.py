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

"""Reconstruct an Ossie model from the underlying sources of a metric view."""

from collections.abc import Mapping
from typing import Any

from sqlglot import exp

from ossie_databricks import _ossie, metric_view
from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    columns,
    has_window,
    measure_names,
    parse,
    translate,
)
from ossie_databricks.result import ConversionError, Result, WarningCode

_EXTENSION_KEY = "metric_view_import"


def convert_metric_view_to_ossie_model(
    metric_view_yaml: str | Mapping[str, Any], name: str
) -> Result[str]:
    """Read the source tables, joins, dimensions and measures into an Ossie model."""
    view = metric_view.load(metric_view_yaml, require_parameter_defaults=False)
    root = {"name": name, "source": view["source"]}
    if view.get("comment"):
        root["description"] = view["comment"]
    importer = _Import(root)
    importer.add_joins(view.get("joins") or [], (), importer.extras)
    for field in view.get("dimensions", view.get("fields")) or []:
        importer.add_item(field, measure=False)
    for measure in view.get("measures") or []:
        importer.add_item(measure, measure=True)
    for key, value in view.items():
        if key not in {"version", "source", "comment", "joins", "dimensions", "fields", "measures"}:
            importer.extras[key] = value
            importer.warn(key, "preserved in the Databricks extension")
    model = {
        "version": _ossie.spec_version(),
        "name": name,
        "datasets": list(importer.datasets.values()),
    }
    if importer.relationships:
        model["relationships"] = importer.relationships
    if importer.metrics:
        model["metrics"] = importer.metrics
    if importer.extras.get("opaque_joins"):
        importer.extras["paths"] = {
            ".".join(path): dataset["name"] for path, dataset in importer.datasets.items() if path
        }
    if importer.extras:
        model["custom_extensions"] = [
            {
                "vendor_name": DATABRICKS,
                "data": _ossie.dump({_EXTENSION_KEY: {"root": name, "extras": importer.extras}}),
            }
        ]
    _ossie.check(model, "the imported model")
    importer.result.value = _ossie.dump(model)
    return importer.result


class _Import:
    def __init__(self, root):
        self.datasets = {(): root}
        self.relationships = []
        self.metrics = []
        self.dimensions = {}
        self.extras = {}
        self.result = Result("")

    def warn(self, path, message):
        self.result.warn(WarningCode.METRIC_VIEW_EXTENSION, path, message)

    def add_joins(self, joins, parent, extras):
        for join in joins:
            if not isinstance(join, Mapping) or not isinstance(join.get("name"), str):
                raise ConversionError("a metric view join needs a name")
            alias = join["name"]
            source = join.get("source")
            if not isinstance(source, str):
                raise ConversionError(f"the join {alias!r} needs a source")
            path = (*parent, alias.casefold())
            if path in self.datasets:
                raise ConversionError(f"duplicate metric view join {'.'.join(path)!r}")
            name = _unique_name(alias, (dataset["name"] for dataset in self.datasets.values()))
            child = {"name": name, "source": source}
            self.datasets[path] = child
            owner = self.datasets[parent]
            try:
                if join.get("cardinality", "many_to_one") != "many_to_one":
                    raise Untranslatable("the join is not many-to-one")
                pairs = _join_columns(join, parent)
            except Untranslatable as error:
                self.warn(f"joins[{'.'.join(path)}]", f"{error}; join preserved in the extension")
                extras.setdefault("opaque_joins", []).append(
                    {"parent": owner["name"], "dataset": name, "join": join}
                )
            else:
                left, right = zip(*pairs, strict=True)
                rel_name = _unique_name(
                    f"{owner['name']}_to_{name}", (rel["name"] for rel in self.relationships)
                )
                self.relationships.append(
                    {
                        "name": rel_name,
                        "from": owner["name"],
                        "to": name,
                        "from_columns": [_declare_column(owner, col) for col in left],
                        "to_columns": [_declare_column(child, col) for col in right],
                    }
                )
                keys = self.relationships[-1]["to_columns"]
                child["unique_keys"] = [list(keys)]
                vendor = {
                    k: v
                    for k, v in join.items()
                    if k not in {"name", "source", "joins", "on", "using"}
                }
                if alias != name:
                    vendor["name"] = alias
                if "using" in join:
                    vendor["using"] = join["using"]
                if vendor:
                    extras.setdefault("join_options", {})[rel_name] = vendor
                    if set(vendor) - {"name", "using", "cardinality"}:
                        self.warn(
                            f"joins[{'.'.join(path)}]", "join options preserved in the extension"
                        )
            self.add_joins(join.get("joins") or [], path, extras)

    def add_item(self, item, *, measure):
        if not isinstance(item, Mapping):
            raise ConversionError("a metric view dimension or measure must be a mapping")
        key = "measures" if measure else "dimensions"
        path = f"{key}[{item.get('name', '?')}]"
        name = item.get("name")
        text = item.get("expr")
        try:
            if not isinstance(name, str) or not isinstance(text, str):
                raise Untranslatable("a named expression is required")
            tree = parse(text, DATABRICKS)
            if (
                has_window(tree)
                or measure_names(tree)
                or tree.find(exp.Query, exp.Placeholder, exp.Parameter)
                or any(column.is_star for column in columns(tree))
            ):
                raise Untranslatable("the expression requires metric view evaluation")
            if item.get("window"):
                raise Untranslatable("window measures require metric view evaluation")
            tree = metric_view.renamed(tree, self._inline_dimension)
            owners = {self._owner(column) for column in columns(tree)}
            owner = next(iter(owners), ()) if len(owners) <= 1 else None
            if not measure and owner is None:
                raise Untranslatable("the dimension reads multiple sources")
            dataset = self.datasets[owner] if owner is not None else None
            if (
                measure
                and dataset is not None
                and any(
                    field["name"].casefold() in (name.casefold(), "source")
                    for field in dataset.get("fields", [])
                )
            ):
                owner = None

            def bind(column):
                source = self.datasets[self._owner(column)]
                if owner is None:
                    name = _declare_column(source, column)
                    return exp.column(name, source["name"], quoted=column.this.quoted)
                return exp.column(
                    column.name, "source" if measure else None, quoted=column.this.quoted
                )

            expression = {"sql": translate(metric_view.renamed(tree, bind), OSSIE)}
            out = {"name": name, "expression": expression}
            if item.get("comment"):
                out["description"] = item["comment"]
            if item.get("synonyms"):
                out["ai_context"] = {"synonyms": item["synonyms"]}
            if measure:
                target = (
                    self.metrics
                    if owner is None
                    else self.datasets[owner].setdefault("metrics", [])
                )
                if any(other["name"].casefold() == name.casefold() for other in target):
                    raise Untranslatable("duplicate metric name")
                target.append(out)
                if owner:
                    self.extras.setdefault("joined_measures", {}).setdefault(
                        self.datasets[owner]["name"], []
                    ).append(name)
            else:
                if item.get("display_name"):
                    out["label"] = item["display_name"]
                out["dimension"] = {}
                fields = dataset.setdefault("fields", [])
                previous = next(
                    (f for f in fields if f["name"].casefold() == name.casefold()), None
                )
                if previous is not None:
                    if previous["expression"] != expression:
                        raise Untranslatable("the dimension name collides with a join field")
                    previous.update(out)
                else:
                    fields.append(out)
                self.dimensions[name.casefold()] = metric_view.renamed(tree, self._qualify_source)
            supported = {"name", "expr", "comment", "synonyms"}
            if not measure:
                supported.add("display_name")
            extra = {k: v for k, v in item.items() if k not in supported}
            if extra:
                self.extras.setdefault("item_options", {}).setdefault(key, {})[name] = extra
                self.warn(path, "item options preserved in the extension")
        except Untranslatable as error:
            self.extras.setdefault("opaque_items", {}).setdefault(key, []).append(item)
            self.warn(path, f"{error}; definition preserved in the extension")

    def _owner(self, column):
        parts = tuple(part.name.casefold() for part in column.parts[:-1])
        path = () if parts == ("source",) else parts
        if path not in self.datasets:
            raise Untranslatable(f"unknown source {'.'.join(parts)!r}")
        return path

    def _inline_dimension(self, column):
        if len(column.parts) == 1:
            tree = self.dimensions.get(column.name.casefold())
            return tree.copy() if tree is not None else None
        return None

    def _qualify_source(self, column):
        if len(column.parts) == 1:
            return exp.column(column.name, "source", quoted=column.this.quoted)
        return None


def _unique_name(name, existing):
    used = {value.casefold() for value in existing}
    candidate = name
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{name}_{suffix}"
        suffix += 1
    return candidate


def _declare_column(dataset, column):
    fields = dataset.setdefault("fields", [])
    expression = {"sql": translate(exp.Column(this=column.this.copy()), OSSIE)}
    for field in fields:
        if field["name"].casefold() == column.name.casefold() and field["expression"] == expression:
            return field["name"]
    name = _unique_name(column.name, (field["name"] for field in fields))
    fields.append({"name": name, "expression": expression})
    return name


def _join_columns(join, parent):
    using = join.get("using")
    if using and isinstance(using, list) and all(isinstance(name, str) for name in using):
        return [(exp.column(name), exp.column(name)) for name in using]
    text = join.get("on")
    if not isinstance(text, str):
        raise Untranslatable("the join has no supported key condition")
    tree = parse(text, DATABRICKS)
    child = join["name"].casefold()

    def owner(column):
        prefix = tuple(p.name.casefold() for p in column.parts[:-1])
        if not prefix:
            return None
        if prefix in {(child,), (*parent, child)}:
            return "child"
        if prefix == parent or prefix == (parent[-1] if parent else "source",):
            return "parent"
        return "unknown"

    pairs = []
    tree = tree.unnest()
    for term in tree.flatten() if isinstance(tree, exp.And) else [tree]:
        term = term.unnest()
        if not isinstance(term, exp.EQ) or not all(
            isinstance(side, exp.Column) for side in (term.this, term.expression)
        ):
            raise Untranslatable("the join condition is not column equality")
        left, right = term.this, term.expression
        a, b = owner(left), owner(right)
        if a == "child" or b == "parent":
            left, right, a, b = right, left, b, a
        if a not in (None, "parent") or b not in (None, "child"):
            raise Untranslatable("the join condition does not connect its parent and child")
        pairs.append((left, right))
    return pairs
