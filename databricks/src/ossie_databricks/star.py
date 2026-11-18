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

"""Build metric view joins and measures from the datasets reachable at the root grain."""

from collections import Counter

from sqlglot import exp

from ossie_databricks import _ossie, metric_view
from ossie_databricks.expressions import (
    DATABRICKS,
    Untranslatable,
    columns,
    measure_names,
    parse,
    translate,
)
from ossie_databricks.result import ConversionError, WarningCode


def _column(name, prefix=()):
    return (
        exp.Column(
            **dict(
                zip(
                    ("this", "table", "db", "catalog"),
                    map(exp.to_identifier, (name, *prefix[::-1])),
                    strict=False,
                )
            )
        )
        if len(prefix) < 4
        else exp.Dot.build([exp.to_identifier(part) for part in (*prefix, name)])
    )


def _selected(expression):
    native = next(
        (
            entry["expression"]
            for entry in expression.get("dialects", [])
            if entry["dialect"] == DATABRICKS
        ),
        None,
    )
    if native:
        return native, DATABRICKS
    picked = metric_view.portable_text(expression)
    if picked is None:
        raise Untranslatable("no suitable SQL expression")
    return picked


def _render(tree, dialect):
    if dialect == DATABRICKS:
        return tree.sql(dialect="databricks")
    return translate(tree, DATABRICKS)


def _physical(dataset, name, prefix, *, required=False):
    field = next(
        (f for f in dataset.get("fields", []) if f["name"].casefold() == name.casefold()),
        None,
    )
    if field is None:
        if required:
            raise Untranslatable(f"missing join field {name!r}")
        return _column(name, prefix)
    text, dialect = _selected(field["expression"])

    def bind(column):
        if len(column.parts) > 1 and column.table.casefold() not in (
            "source",
            dataset["name"].casefold(),
        ):
            raise Untranslatable("field expression references another source")
        return _column(column.name, prefix)

    tree = metric_view.renamed(parse(text, dialect), bind)
    return parse(_render(tree, dialect), DATABRICKS)


def unique_target(relationship, target):
    columns = {part.casefold() for part in relationship["to_columns"]}
    return any(
        columns == {part.casefold() for part in key}
        for key in [target.get("primary_key") or [], *(target.get("unique_keys") or [])]
    )


class Star:
    def __init__(self, root, datasets, relationships, result, extras):
        self.root = root
        self.datasets = datasets
        self.extras = extras
        self.bound = {root: ()}
        self.relationships = set()
        self.ambiguous = set()
        self.joins = self._joins(root, relationships, set(), result)

    def _joins(self, parent, relationships, ancestors, result):
        out = []
        for rel in relationships:
            if rel["from"].casefold() != parent:
                continue
            child = rel["to"].casefold()
            target = self.datasets[child]
            if not unique_target(rel, target) or child in ancestors | {parent}:
                continue
            options = self.extras.get("join_options", {}).get(rel["name"], {})
            alias = options.get("name", target["name"])
            if (
                child in self.bound
                or (parent == self.root and alias.casefold() == "source")
                or any(item["name"].casefold() == alias.casefold() for item in out)
            ):
                self.ambiguous.add(child)
                result.warn(
                    WarningCode.NOT_DEPLOYED,
                    f"relationships[{rel['name']}]",
                    f"the join alias is ambiguous in the {self.root} metric view",
                )
                continue
            prefix = (*self.bound[parent], alias)
            try:
                pairs = [
                    exp.EQ(
                        this=_physical(
                            self.datasets[parent],
                            left,
                            self.bound[parent] or ("source",),
                            required=True,
                        ),
                        expression=_physical(target, right, prefix, required=True),
                    )
                    for left, right in zip(rel["from_columns"], rel["to_columns"], strict=True)
                ]
            except Untranslatable as error:
                result.warn(WarningCode.NOT_DEPLOYED, f"relationships[{rel['name']}]", str(error))
                continue
            self.bound[child] = prefix
            self.relationships.add(rel["name"])
            same_columns = all(
                isinstance(pair.this, exp.Column)
                and isinstance(pair.expression, exp.Column)
                and pair.this.name == pair.expression.name
                for pair in pairs
            )
            if parent != self.root and all(
                isinstance(side, exp.Column)
                for pair in pairs
                for side in (pair.this, pair.expression)
            ):
                # Databricks documents nested predicates against the parent and
                # child source columns in the context of that nested join.
                for pair in pairs:
                    for side in (pair.this, pair.expression):
                        for qualifier in ("catalog", "db", "table"):
                            side.set(qualifier, None)
            join = {
                "name": alias,
                "source": target["source"].strip().removesuffix(";").rstrip(),
                "on": exp.and_(*pairs).sql(dialect="databricks"),
            }
            if parent != self.root and same_columns:
                join.pop("on")
                join["using"] = [pair.this.name for pair in pairs]
            using = options.get("using")
            if (
                isinstance(using, list)
                and len(pairs) == len(using)
                and all(
                    isinstance(pair.this, exp.Column)
                    and isinstance(pair.expression, exp.Column)
                    and pair.this.name == pair.expression.name == column
                    for pair, column in zip(pairs, using, strict=True)
                )
            ):
                join.pop("on", None)
                join["using"] = using
            join.update(
                {key: value for key, value in options.items() if key not in {"name", "using"}}
            )
            nested = self._joins(child, relationships, ancestors | {parent}, result)
            if nested:
                join["joins"] = nested
            out.append(join)
        for record in self.extras.get("opaque_joins", []):
            if record["parent"].casefold() != parent:
                continue
            join = record["join"]
            if any(item["name"].casefold() == join["name"].casefold() for item in out):
                continue
            dataset_name = record["dataset"].casefold()
            if dataset_name in self.datasets:
                self.bound[dataset_name] = (*self.bound[parent], join["name"])
                join = {**join, "source": self.datasets[dataset_name]["source"]}
                # The original subtree carries aliases and predicates opaque to Ossie.
                self._bind_opaque_children(dataset_name, join.get("joins", []))
                out.append(join)
        return out

    def _bind_opaque_children(self, parent, joins):
        for join in joins:
            prefix = (*self.bound[parent], join["name"])
            child = self.extras.get("paths", {}).get(".".join(p.casefold() for p in prefix))
            if child and child.casefold() in self.datasets:
                self.bound[child.casefold()] = prefix
                self._bind_opaque_children(child.casefold(), join.get("joins", []))

    def restore_options(self, view):
        for key in ("dimensions", "measures"):
            options = self.extras.get("item_options", {}).get(key, {})
            for item in view.get(key, []):
                item.update(options.get(item["name"], {}))
            taken = {item["name"].casefold() for item in view.get(key, [])}
            for item in self.extras.get("opaque_items", {}).get(key, []):
                if item.get("name", "").casefold() not in taken:
                    view.setdefault(key, []).append(item)
        internal = {
            "item_options",
            "opaque_items",
            "opaque_joins",
            "join_options",
            "joined_measures",
            "paths",
        }
        view.update({k: v for k, v in self.extras.items() if k not in internal})

    def add_imported_measures(self, view, result):
        for dataset_name, names in self.extras.get("joined_measures", {}).items():
            owner = dataset_name.casefold()
            if owner not in self.bound:
                continue
            dataset = self.datasets[owner]
            prefix = self.bound[owner]
            for metric in dataset.get("metrics", []):
                if metric["name"] not in names:
                    continue
                try:
                    text, dialect = _selected(metric["expression"])
                    tree = parse(text, dialect)

                    def bind(column, dataset=dataset, prefix=prefix):
                        if column.table.casefold() == "source":
                            return _column(column.name, prefix)
                        if len(column.parts) != 1:
                            raise Untranslatable("joined measure references a different source")
                        return _physical(dataset, column.name, prefix, required=True)

                    expression = _render(metric_view.renamed(tree, bind), dialect)
                except Untranslatable as error:
                    result.warn(WarningCode.NOT_DEPLOYED, f"metrics[{metric['name']}]", str(error))
                    continue
                view.setdefault("measures", []).append(
                    metric_view._item(metric, expression, f"metrics[{metric['name']}]", result)
                )

    def add_dimensions(self, view, result):
        if self.joins:
            view["joins"] = self.joins
        taken = {
            item["name"].casefold()
            for item in [*view.get("dimensions", []), *view.get("measures", [])]
        }
        for name, prefix in self.bound.items():
            if not prefix:
                continue
            dataset = self.datasets[name]
            for field in dataset.get("fields", []):
                path = f"datasets[{dataset['name']}].fields[{field['name']}]"
                try:
                    expr = _physical(dataset, field["name"], prefix).sql(dialect="databricks")
                except Untranslatable as error:
                    result.warn(WarningCode.NOT_DEPLOYED, path, str(error))
                    continue
                alias = field["name"]
                if alias.casefold() in taken:
                    alias = "__".join((*prefix, alias))
                    base = alias
                    suffix = 2
                    while alias.casefold() in taken:
                        alias = f"{base}_{suffix}"
                        suffix += 1
                item = metric_view._item(field, expr, path, result)
                item["name"] = alias
                view.setdefault("dimensions", []).append(item)
                taken.add(alias.casefold())

    def measure(self, metric, tree, dialect, view, result):
        name = metric["name"]
        if any(
            item["name"].casefold() == name.casefold()
            for item in [*view.get("dimensions", []), *view.get("measures", [])]
        ):
            return False

        root_fields = {item["name"].casefold() for item in view.get("dimensions", [])}
        joined_fields = {
            field["name"].casefold()
            for owner, prefix in self.bound.items()
            if prefix
            for field in self.datasets[owner].get("fields", [])
        }

        def bind(column):
            if len(column.parts) == 1:
                argument = exp.column(column.name)
                if isinstance(column.parent, exp.Agg):
                    return argument
                return exp.Anonymous(this="MEASURE", expressions=[argument])
            owner = column.parts[0].name.casefold()
            prefix = self.bound[owner]
            name = column.parts[1].name
            if not prefix and name.casefold() in root_fields and len(column.parts) == 2:
                return _column(name)
            if not prefix and name.casefold() in joined_fields:
                prefix = ("source",)
            physical = _physical(self.datasets[owner], name, prefix)
            for part in column.parts[2:]:
                physical = exp.Dot(this=physical, expression=part.copy())
            return physical

        try:
            expr = _render(metric_view.renamed(tree, bind), dialect)
        except Untranslatable:
            return False
        view.setdefault("measures", []).append(
            metric_view._item(metric, expr, f"metrics[{name}]", result)
        )
        return True


def add_model_metrics(metrics, stars, views, result):
    deployed = {}
    names = {metric["name"].casefold() for metric in metrics}
    incoming = Counter(name for star in stars.values() for name, path in star.bound.items() if path)
    pending = []
    for metric in metrics:
        try:
            text, dialect = _selected(metric["expression"])
            tree = parse(text, dialect)
        except Untranslatable:
            continue
        if tree.find(exp.Query, exp.Command, exp.Window):
            continue
        refs = columns(tree)
        if any(column.is_star for column in refs):
            continue
        # Databricks requires MEASURE(...) at the referenced measure's grain.
        # A bare name in portable model SQL can only refer to a model metric.
        dependencies = {column.name.casefold() for column in refs if len(column.parts) == 1}
        if not dependencies <= names or (dialect == DATABRICKS and dependencies):
            continue
        if measure_names(tree) and (
            dialect == DATABRICKS
            or any(
                not isinstance(call.this, exp.Column) or len(call.this.parts) != 1
                for call in tree.find_all(exp.Agg)
            )
        ):
            continue
        if any(
            len(column.parts) == 1
            and (aggregate := column.find_ancestor(exp.AggFunc)) is not None
            and not isinstance(aggregate, exp.Agg)
            for column in refs
        ):
            continue
        owners = {column.parts[0].name.casefold() for column in refs if len(column.parts) > 1}
        pending.append((metric, tree, dialect, dependencies, owners))

    while pending:
        waiting = []
        for metric, tree, dialect, dependencies, owners in pending:
            if not dependencies <= deployed.keys():
                waiting.append((metric, tree, dialect, dependencies, owners))
                continue
            candidates = [
                name
                for name, star in stars.items()
                if owners <= star.bound.keys()
                and not owners & star.ambiguous
                and all(deployed[dep].casefold() == name for dep in dependencies)
            ]
            preferred = [name for name in candidates if name in owners and len(owners) == 1]
            roots = [name for name in candidates if not incoming[name]]
            imported = [name for name in candidates if stars[name].extras]
            ranked = preferred or imported or roots or candidates
            if len(ranked) == 1:
                name = ranked[0]
                if stars[name].measure(metric, tree, dialect, views[name], result):
                    key = metric["name"].casefold()
                    deployed[key] = stars[name].datasets[name]["name"]
        if len(waiting) == len(pending):
            break
        pending = waiting
    return deployed


def imported_options(model):
    for extension in model.get("custom_extensions", []):
        if extension["vendor_name"] != DATABRICKS:
            continue
        try:
            data = _ossie.load(extension["data"]).get("metric_view_import")
        except ConversionError:
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("root"), str)
            and isinstance(data.get("extras"), dict)
        ):
            return data["root"].casefold(), data["extras"]
    return None, {}
