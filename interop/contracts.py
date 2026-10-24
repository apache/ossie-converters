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

"""Bounded assertions for dialect-only expressions, key roles, and table identity."""

import json
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
import yaml

from .manifest import ManifestError, UniqueLoader


_DIALECTS = {
    "ANSI_SQL": None,
    "OSSIE_SQL_2026": None,
    "DATABRICKS": "databricks",
    "SNOWFLAKE": "snowflake",
}


def _check(checks, path, expected, actual, error=None):
    checks.append(
        {
            "path": path,
            "expected": expected,
            "actual": actual,
            "grade": "FAIL" if error or actual != expected else "OK",
            "error": error,
        }
    )


def _result(checks):
    return {
        "grade": "FAIL" if any(check["grade"] == "FAIL" for check in checks) else "OK",
        "checks": checks,
    }


def _table(source):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("missing physical source reference")
    tables = sqlglot.Dialect.get_or_raise("databricks").parse_into(exp.Table, source)
    if len(tables) != 1 or not isinstance(tables[0], exp.Table):
        raise ValueError("expected exactly one physical table reference")
    return tables[0]


def table_parts(source):
    table = _table(source)
    if (
        table.args.get("alias")
        or len(table.parts) not in (2, 3)
        or any(not isinstance(part, exp.Identifier) for part in table.parts)
    ):
        raise ValueError("expected a two- or three-part physical table reference")
    return [
        part.name if part.args.get("quoted") else part.name.lower()
        for part in table.parts
    ]


def _expression(sql, dialect, dataset, metric):
    if dialect not in _DIALECTS:
        raise ValueError(f"unsupported expression dialect: {dialect}")
    statements = sqlglot.parse(sql, read=_DIALECTS[dialect])
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("expected exactly one SQL expression")
    tree = statements[0]
    if any(isinstance(node, exp.Query) for node in tree.walk()):
        raise ValueError("queries are outside the expression assertion scope")
    for column in tree.find_all(exp.Column):
        qualifier = column.args.get("table")
        # Ownership follows the same identifier rules as expression comparison:
        # unquoted spellings are case-insensitive; quoted names must match exactly.
        owned = qualifier is None or (
            qualifier.name == dataset
            if qualifier.args.get("quoted")
            else qualifier.name.lower() == dataset.lower()
        )
        if len(column.parts) > 2 or not owned:
            raise ValueError(f"unexpected dataset qualifier: {column.sql()}")
        column.set("table", exp.to_identifier(dataset) if metric else None)
    return tree.sql(dialect="databricks", normalize=True)


def check_document(data, category, oracle, *, input_stage=False):
    checks = []
    if category == "sources":
        try:
            expected = sorted(table_parts(source) for source in oracle["sources"])
            actual = sorted(table_parts(ds.get("source")) for ds in data["datasets"])
            _check(checks, "datasets.sources", expected, actual)
        except (ValueError, SqlglotError) as error:
            _check(checks, "datasets.sources", oracle["sources"], None, str(error))
        return _result(checks)
    datasets = {ds["name"]: ds for ds in data["datasets"]}
    _check(checks, "datasets.names", sorted(oracle["datasets"]), sorted(datasets))
    if category == "keys":
        for name, expected in oracle["datasets"].items():
            actual = datasets.get(name, {})
            _check(
                checks,
                f"datasets.{name}.primary_key",
                sorted(expected["primary_key"]),
                sorted(actual.get("primary_key") or []),
            )
            _check(
                checks,
                f"datasets.{name}.unique_keys",
                sorted(sorted(key) for key in expected["unique_keys"]),
                sorted(sorted(key) for key in actual.get("unique_keys") or []),
            )
            # These fixtures explicitly expose their key columns. A copied key
            # declaration must not hide the loss of those field definitions.
            required = set(expected["primary_key"]) | {
                col for key in expected["unique_keys"] for col in key
            }
            fields = {field["name"] for field in actual.get("fields") or []}
            _check(
                checks,
                f"datasets.{name}.key_fields",
                sorted(required),
                sorted(required & fields),
            )
        return _result(checks)
    dataset = next(iter(oracle["datasets"]))
    metrics = {metric["name"]: metric for metric in data.get("metrics") or []}
    _check(checks, "metrics.names", sorted(oracle["metrics"]), sorted(metrics))
    elements = []
    for name, fields in oracle["datasets"].items():
        actual_fields = {
            field["name"]: field for field in datasets.get(name, {}).get("fields") or []
        }
        _check(checks, f"datasets.{name}.fields", sorted(fields), sorted(actual_fields))
        elements += [
            (f"datasets.{name}.fields.{field}", sql, actual_fields.get(field), False)
            for field, sql in fields.items()
        ]
    elements += [
        (f"metrics.{name}", sql, metrics.get(name), True)
        for name, sql in oracle["metrics"].items()
    ]
    for path, expected_sql, element, metric in elements:
        dialects = (element or {}).get("expression", {}).get("dialects") or []
        if input_stage:
            _check(
                checks,
                path + ".dialects",
                [oracle["dialect"]],
                [item["dialect"] for item in dialects],
            )
        try:
            expected = _expression(expected_sql, oracle["dialect"], dataset, metric)
            if not dialects:
                _check(
                    checks, path + ".expression", expected, None, "missing expression"
                )
            for index, item in enumerate(dialects):
                actual = _expression(
                    item["expression"], item["dialect"], dataset, metric
                )
                _check(checks, f"{path}.expression[{index}]", expected, actual)
        except (ValueError, SqlglotError) as error:
            _check(checks, path + ".expression", expected_sql, None, str(error))
    return _result(checks)


def _native_documents(path, native_format):
    paths = (
        [path]
        if path.is_file()
        else sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and item.suffix in (".yaml", ".yml")
        )
    )
    for item in paths:
        text = item.read_text(encoding="utf-8")
        yield (
            json.loads(text)
            if native_format == "dbt"
            else yaml.load(text, Loader=UniqueLoader)
        )


def check_native_sources(path: Path, native_format, oracle):
    checks, actual = [], []
    try:
        expected = sorted(table_parts(source) for source in oracle["sources"])
        for data in _native_documents(path, native_format):
            if native_format == "cube":
                actual += [
                    table_parts(cube.get("sql_table"))
                    for cube in data.get("cubes") or []
                ]
            elif native_format == "databricks":

                def walk(node):
                    yield table_parts(node.get("source"))
                    for join in node.get("joins") or []:
                        yield from walk(join)

                actual += list(walk(data))
            elif native_format == "dbt":
                for model in data.get("semantic_models") or []:
                    relation = model["node_relation"]
                    raw = [
                        relation.get(key)
                        for key in ("database", "schema_name", "alias")
                    ]
                    source = relation.get("relation_name") or ".".join(
                        value for value in raw if value
                    )
                    parts = table_parts(source)
                    actual.append(parts)
                    expected_slots = ([None] if len(parts) == 2 else []) + parts
                    for key, value, wanted in zip(
                        ("database", "schema_name", "alias"), raw, expected_slots
                    ):
                        try:
                            if value is None or value == "":
                                atom = None
                            else:
                                table = _table(value)
                                if (
                                    table.args.get("alias")
                                    or len(table.parts) != 1
                                    or not isinstance(table.parts[0], exp.Identifier)
                                ):
                                    raise ValueError(
                                        "native catalog/schema/table slot is not one identifier"
                                    )
                                part = table.parts[0]
                                atom = (
                                    part.name
                                    if part.args.get("quoted")
                                    else part.name.lower()
                                )
                            _check(
                                checks,
                                f"semantic_models.{model['name']}.node_relation.{key}",
                                wanted,
                                atom,
                            )
                        except (ValueError, SqlglotError) as error:
                            _check(
                                checks,
                                f"semantic_models.{model['name']}.node_relation.{key}",
                                wanted,
                                value,
                                str(error),
                            )
            else:
                raise ValueError(f"unsupported native source format: {native_format}")
        _check(checks, "native.sources", expected, sorted(actual))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
        ManifestError,
        SqlglotError,
    ) as error:
        _check(checks, "native.sources", oracle["sources"], actual, str(error))
    return _result(checks)
