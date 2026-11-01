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

"""Independent, deliberately bounded inspection of native Cube aggregate bindings.

No converter helpers or stashed Ossie metadata participate in the oracle. Cube
member references are followed from native SQL, with physical column ownership
and the cube on which each aggregation runs recorded separately.
"""

from collections import Counter
from pathlib import Path
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
import yaml

from .manifest import ManifestError, UniqueLoader


class BindingError(ValueError):
    pass


# Match quoted SQL and comments before templates, so a brace in prose never
# becomes a dependency. Unsupported dynamic templates fail closed during parsing.
_TOKENS = re.compile(
    r"'(?:''|\\.|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|"
    r"--[^\n]*|/\*.*?\*/|(?<!\\)\$?\{([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\}",
    re.S,
)
_FUNCTIONS = {
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "count": "count",
    "countDistinct": "count_distinct",
}


def _identifier(node):
    return node.sql() if node.args.get("quoted") else node.name.lower()


class CubeBindings:
    def __init__(self, path: Path):
        self.cubes = {}
        paths = (
            [path]
            if path.is_file()
            else sorted(
                item
                for item in path.rglob("*")
                if item.suffix in (".yaml", ".yml") and item.is_file()
            )
        )
        try:
            for item in paths:
                data = yaml.load(item.read_text(encoding="utf-8"), Loader=UniqueLoader)
                if not isinstance(data, dict):
                    raise BindingError(f"{item}: expected native model mapping")
                for cube in data.get("cubes") or []:
                    name = cube["name"]
                    if name in self.cubes:
                        raise BindingError(f"duplicate native cube {name}")
                    self.cubes[name] = cube
            self.sources = {}
            self.members = {}
            for name, cube in self.cubes.items():
                if cube.get("sql") or cube.get("extends") or cube.get("includes"):
                    raise BindingError(
                        f"{name}: SQL-backed or inherited cubes are not supported"
                    )
                table = sqlglot.parse_one(cube["sql_table"], into=exp.Table)
                if (
                    not table.parts
                    or table.args.get("alias")
                    or any(not isinstance(p, exp.Identifier) for p in table.parts)
                ):
                    raise BindingError(
                        f"{name}: sql_table must be a physical table reference"
                    )
                self.sources[name] = ".".join(_identifier(part) for part in table.parts)
                for kind in ("dimensions", "measures"):
                    for member in cube.get(kind) or []:
                        key = (name, member["name"])
                        if key in self.members:
                            raise BindingError(
                                f"duplicate native member {name}.{member['name']}"
                            )
                        self.members[key] = (kind, member)
        except (
            OSError,
            yaml.YAMLError,
            SqlglotError,
            ManifestError,
            KeyError,
            TypeError,
            AttributeError,
        ) as error:
            raise BindingError(str(error)) from error

    def metric(self, name):
        matches = [
            key
            for key, (kind, member) in self.members.items()
            if kind == "measures"
            and key[1] == name
            and member.get("public") is not False
        ]
        if len(matches) != 1:
            raise BindingError(
                f"metric {name}: expected one public native measure, found {len(matches)}"
            )
        _, bindings = self._member(*matches[0], ())
        if not bindings:
            raise BindingError(f"metric {name}: no supported aggregate bindings")
        return sorted(
            bindings, key=lambda item: tuple(item[key] for key in sorted(item))
        )

    def _member(self, cube, name, stack):
        key = (cube, name)
        if key in stack:
            raise BindingError(f"cyclic native member reference at {cube}.{name}")
        if key not in self.members:
            raise BindingError(f"missing native member {cube}.{name}")
        kind, member = self.members[key]
        if any(
            member.get(option)
            for option in ("filters", "rolling_window", "multi_stage", "case")
        ):
            raise BindingError(
                f"{cube}.{name}: filtered, windowed, staged, or case members are unsupported"
            )
        stack = (*stack, key)
        sql = member.get("sql")
        if kind == "measures" and member.get("type") == "count" and sql is None:
            keys = [
                m["name"]
                for m in self.cubes[cube].get("dimensions", [])
                if m.get("primary_key")
            ]
            if len(keys) != 1:
                raise BindingError(
                    f"{cube}.{name}: a bare Cube count requires one primary-key dimension"
                )
            columns, aggregates = self._member(cube, keys[0], stack)
            if aggregates or len(columns) != 1:
                raise BindingError(f"{cube}.{name}: unsupported primary-key expression")
            return [], self._aggregate("count_distinct", cube, columns)
        if not isinstance(sql, str) or not sql.strip():
            raise BindingError(f"{cube}.{name}: missing native SQL")
        tree, refs = self._parse(sql, cube)
        columns, aggregates = self._value(tree, cube, refs, stack)
        if kind == "dimensions":
            if aggregates:
                raise BindingError(f"{cube}.{name}: dimension depends on an aggregate")
            return columns, []
        measure_type = member.get("type")
        if measure_type in _FUNCTIONS:
            if aggregates or not columns:
                raise BindingError(f"{cube}.{name}: nested or column-free aggregate")
            return [], self._aggregate(_FUNCTIONS[measure_type], cube, columns)
        if measure_type != "number" or columns:
            raise BindingError(
                f"{cube}.{name}: unsupported measure type or unaggregated columns"
            )
        return [], aggregates

    def _parse(self, sql, cube):
        refs = {}
        prefix = "__interop_binding_"
        while prefix in sql:
            prefix = "_" + prefix

        def replace(match):
            reference = match.group(1)
            if reference is None:
                return match.group()
            parts = reference.split(".")
            if (
                len(parts) == 1
                and parts[0] != "CUBE"
                and not re.match(r"\s*\.", sql[match.end() :])
            ):
                # Cube also permits an unqualified same-cube member: {revenue}.
                owner, member = cube, parts[0]
            else:
                owner = cube if parts[0] == "CUBE" else parts[0]
                member = parts[1] if len(parts) == 2 else None
            if owner not in self.cubes:
                raise BindingError(f"unknown native cube reference: {reference}")
            marker = f"{prefix}{len(refs)}"
            refs[marker] = (owner, member)
            return marker

        try:
            statements = sqlglot.parse(_TOKENS.sub(replace, sql))
            if len(statements) != 1 or statements[0] is None:
                raise BindingError("expected exactly one native SQL expression")
            tree = statements[0]
            if any(
                isinstance(node, (exp.Query, exp.Window, exp.Anonymous))
                for node in tree.walk()
            ):
                raise BindingError(
                    "queries, windows, and unmodelled SQL functions are unsupported"
                )
            return tree, refs
        except SqlglotError as error:
            raise BindingError(f"cannot parse native SQL: {error}") from error

    def _value(self, node, cube, refs, stack):
        if isinstance(node, exp.Column):
            if node.table:
                if (
                    len(node.parts) != 2
                    or node.table not in refs
                    or refs[node.table][1] is not None
                ):
                    raise BindingError(
                        f"unsupported native column qualifier: {node.sql()}"
                    )
                return [(self.sources[refs[node.table][0]], _identifier(node.this))], []
            if node.name in refs:
                owner, member = refs[node.name]
                if member is None:
                    raise BindingError("cube placeholder without a column")
                return self._member(owner, member, stack)
            return [(self.sources[cube], _identifier(node.this))], []
        if isinstance(node, exp.Star):
            return [(self.sources[cube], "*")], []
        if isinstance(node, exp.AggFunc):
            function = node.sql_name().lower()
            if function not in ("sum", "avg", "min", "max", "count"):
                raise BindingError(f"unsupported native aggregate {function}")
            operand = node.this
            if isinstance(operand, exp.Distinct):
                if function != "count" or len(operand.expressions) != 1:
                    raise BindingError(
                        "only single-expression COUNT(DISTINCT ...) is supported"
                    )
                function, operand = "count_distinct", operand.expressions[0]
            columns, nested = self._value(operand, cube, refs, stack)
            if nested or not columns:
                raise BindingError("nested or column-free SQL aggregate")
            return [], self._aggregate(function, cube, columns)
        columns, aggregates = [], []
        for child in node.iter_expressions():
            child_columns, child_aggregates = self._value(child, cube, refs, stack)
            columns += child_columns
            aggregates += child_aggregates
        return columns, aggregates

    def _aggregate(self, function, cube, columns):
        return [
            {
                "function": function,
                "source": source,
                "column": column,
                "grain": self.sources[cube],
            }
            for source, column in columns
        ]


def check_bindings(path: Path, expectations: dict) -> dict:
    result = {
        "grade": "OK",
        "metrics": {},
        "errors": [],
        "scope": "native aggregate function, physical column source, and aggregation grain; not SQL equivalence",
    }
    try:
        inspector = CubeBindings(path)
    except BindingError as error:
        result.update(grade="FAIL", errors=[str(error)])
        return result
    for name, expected in expectations.items():
        metric = {"expected": expected, "actual": [], "grade": "OK", "error": None}
        try:
            metric["actual"] = inspector.metric(name)

            def signature(items):
                return Counter(tuple(sorted(item.items())) for item in items)

            if signature(expected) != signature(metric["actual"]):
                raise BindingError(
                    f"metric {name}: native aggregate bindings differ from the oracle"
                )
        except BindingError as error:
            metric.update(grade="FAIL", error=str(error))
            result["errors"].append(str(error))
        result["metrics"][name] = metric
    if result["errors"]:
        result["grade"] = "FAIL"
    return result
