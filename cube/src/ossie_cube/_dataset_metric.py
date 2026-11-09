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

"""Field resolution and change guards for dataset-scoped metrics."""

import sqlglot

from ._common import (
    DIALECT_OSSIE_SQL,
    SQL_DIALECT_READERS,
    ConversionError,
    cube_reference_bodies,
    lookup_map,
    measure_signature,
    resolve_identifier,
    snake,
    unescape_braces_from_cube,
)
from .expressions import UnattributableSQL, native_tokens, parse


def local_columns(expr, dialect, fields, scope):
    """Yield each SQL column and its declared field, or None for source columns."""
    tree = parse(expr, dialect)
    try:
        invalid = (
            tree is None
            or tree.find(sqlglot.exp.Query, sqlglot.exp.Command) is not None
            or any(
                t.token_type is sqlglot.TokenType.SEMICOLON
                for t in native_tokens(expr, dialect)
            )
        )
    except UnattributableSQL:
        invalid = True
    if invalid:
        raise ConversionError(
            f"{scope}: expected a SQL expression over declared fields"
        )
    names = lookup_map(list(fields))
    for column in tree.find_all(sqlglot.exp.Column):
        ident = column.this
        if column.table or not isinstance(ident, sqlglot.exp.Identifier):
            if (
                len(column.parts) == 2
                and column.table.casefold() == "source"
                and isinstance(ident, sqlglot.exp.Identifier)
                and resolve_identifier(names, "source") is None
            ):
                yield column, None
                continue
            raise ConversionError(
                f"{scope}: dataset-scoped metrics require unqualified declared fields "
                f"or source.column (when no field named source shadows it), got '{column.sql()}'"
            )
        written = (
            '"' + ident.name.replace('"', '""') + '"'
            if ident.args.get("quoted")
            else ident.name
        )
        field = resolve_identifier(names, written)
        if field is None:
            raise ConversionError(
                f"{scope}: '{written}' is not a declared field of this dataset"
            )
        yield column, field


def expression_fields(expression, fields, scope):
    """Resolve fields used by every SQL alternative; other languages are opaque."""
    return {
        field
        for entry in expression["dialects"]
        if entry["dialect"] in {*SQL_DIALECT_READERS, DIALECT_OSSIE_SQL}
        for _, field in local_columns(
            entry["expression"].strip(), entry["dialect"], fields, scope
        )
        if field is not None
    }


def _parked(member):
    meta = member.get("meta")
    data = meta.get("ossie") if isinstance(meta, dict) else None
    return unescape_braces_from_cube(data) if isinstance(data, dict) else {}


def field_signatures(dimensions, cube_name):
    """Fingerprint field definitions and the native member chains they read."""
    members = {dim["name"]: dim for dim in dimensions}
    aliases = {"CUBE", "TABLE", cube_name}
    semantic_keys = {
        "sql",
        "type",
        "case",
        "latitude",
        "longitude",
        "sub_query",
        "switch",
    }

    def definition(dim):
        out = {k: v for k, v in dim.items() if k in semantic_keys}
        parked = _parked(dim)
        out.update({k: parked[k] for k in ("expression", "dialect") if k in parked})
        return out

    def sql_strings(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "sql":
                    yield v
                elif isinstance(v, (dict, list)):
                    yield from sql_strings(v)
        elif isinstance(value, list):
            for item in value:
                yield from sql_strings(item)

    def signature(name, value):
        dependencies = {name: value}
        pending = [value]
        while pending:
            for sql in sql_strings(pending.pop()):
                for body in cube_reference_bodies(sql):
                    head, dot, tail = body.partition(".")
                    ref = (
                        tail
                        if dot and head in aliases
                        else head
                        if not dot and head not in aliases
                        else None
                    )
                    if ref and ref not in dependencies:
                        dependencies[ref] = (
                            definition(members[ref]) if ref in members else None
                        )
                        if dependencies[ref] is not None:
                            pending.append(dependencies[ref])
        return measure_signature({"fields": dependencies})

    out = {}
    for name, dim in members.items():
        dtype = snake(dim.get("type") or "")
        if dim.get("sub_query") or dtype == "switch" or _parked(dim).get("key_field"):
            continue
        value = definition(dim)
        if dtype == "geo":
            for part in ("latitude", "longitude"):
                if dim.get(part) is not None:
                    out[f"{name}_{part}"] = signature(name, {part: value[part]})
        else:
            out[name] = signature(name, value)
    return out


def origin_signature(measure, origin):
    return measure_signature(
        {
            "native": measure_signature(measure),
            "origin": {k: v for k, v in origin.items() if k != "signature"},
        }
    )


def count_key_signature(measure, cube, dimensions):
    """Bare Cube counts read the current key even though their SQL is empty."""
    if snake(measure.get("type") or "") != "count" or measure.get("sql") is not None:
        return None
    parked = _parked(cube)
    return measure_signature(
        {
            "key": parked.get("primary_key"),
            "members": [
                {k: dim[k] for k in ("name", "sql", "case", "type") if k in dim}
                for dim in dimensions
                if dim.get("primary_key")
            ],
        }
    )


def model_fields_resolve(expr, dialect, member_names):
    """A native fallback must not emit model metrics pointing at absent fields."""
    tree = parse(expr, dialect)
    if tree is None:
        return False
    datasets = lookup_map(
        {name: lookup_map(list(fields)) for name, fields in member_names.items()}
    )
    for column in tree.find_all(sqlglot.exp.Column):
        if len(column.parts) < 2:
            return False
        dataset, field = column.parts[:2]
        names = resolve_identifier(datasets, dataset.name)
        if names is None or resolve_identifier(names, field.name) is None:
            return False
    return True
