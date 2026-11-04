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

"""
Converts LookML (Looker's semantic modeling language) to an Ossie YAML semantic
model. Pure offline conversion: no Looker or BigQuery connection required.

Input is a LookML project as a mapping of {relative filename: LookML string},
so views and explores can live in separate files, as they do in Looker.

Mapping summary:
    LookML view           -> Ossie dataset (sql_table_name or derived_table sql
                             becomes the dataset source)
    LookML dimension      -> Ossie field; `${TABLE}.col` becomes `col`, and
                             `${other_dimension}` is replaced by that
                             dimension's SQL, since Ossie field expressions
                             reference columns, not other fields

Expressions are tagged with the BIGQUERY dialect, as written in LookML.
"""

import re
import warnings

import lkml
import sqlglot
import yaml

from .ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


DEFAULT_MODEL_NAME = "lookml_model"

DIALECT = "BIGQUERY"

# LookML dimension `type` -> Ossie `datatype`. `number` is absent on purpose:
# LookML does not say whether it is an integer or a decimal, and the Ossie
# spec says to omit `datatype` when it is unknown.
_DATATYPES = {
    "string": "String",
    "yesno": "Boolean",
    "zipcode": "String",
}

# LookML types whose `sql` is a plain value Ossie can hold. Other types
# (tier, location, distance, duration, bin, ...) compute their value from
# `sql` or have none, so they are skipped.
_VALUE_TYPES = set(_DATATYPES) | {"number"}

# A LookML substitution: ${TABLE}, ${field}, ${view.field}, ${field._sql}.
_SUBSTITUTION = re.compile(r"\$\{([^}]*)\}")


class _Unsupported(Exception):
    """A LookML construct with no Ossie form. The message says which."""


def _warn(scope, msg):
    warnings.warn(f"[{scope}] {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_lookml_to_ossie(files, model_name=DEFAULT_MODEL_NAME):
    """Top-level entry point. Parses LookML files, converts them, returns an
    Ossie document as a YAML string.

    Args:
        files: {relative filename: LookML string}.
        model_name: Name of the Ossie semantic model.

    Returns:
        Ossie YAML string.

    Raises:
        OssieConversionError: If the input cannot be converted.
    """
    if not isinstance(files, dict) or not files:
        raise OssieConversionError("Expected a non-empty mapping of {filename: LookML}")

    views = _load_views(files)
    datasets = [ds for ds in (_convert_view(v) for v in views) if ds is not None]

    model = {
        "version": SUPPORTED_VERSION,
        "name": model_name,
        "datasets": datasets,
    }
    return yaml.safe_dump(model, sort_keys=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _load_views(files):
    """Parses every file and returns its views, rejecting duplicate names."""
    views = {}
    for fname, text in files.items():
        try:
            parsed = lkml.load(text)
        except SyntaxError as e:
            raise OssieConversionError(f"File '{fname}': invalid LookML: {e}")
        for view in parsed.get("views", []):
            name = view["name"]
            if name.startswith("+"):
                _warn(f"view '{name}'", "refinements are not supported yet; skipping")
                continue
            if name in views:
                raise OssieConversionError(
                    f"View '{name}' is defined in both '{views[name][0]}' and '{fname}'"
                )
            views[name] = (fname, view)
    return [view for _, view in views.values()]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _convert_view(view):
    """Converts one LookML view to an Ossie dataset, or None if it has no
    SQL source Ossie can express."""
    name = view["name"]
    source = _convert_source(view)
    if source is None:
        return None
    dataset = {"name": name, "source": source}

    dimensions = {d["name"]: d for d in view.get("dimensions", [])}
    fields = []
    primary_key = []
    for dim_name, dim in dimensions.items():
        field = _convert_dimension(name, dim, dimensions)
        if field is None:
            continue
        fields.append(field)
        if dim.get("primary_key") == "yes":
            primary_key.append(dim_name)

    if primary_key:
        dataset["primary_key"] = primary_key
    if fields:
        dataset["fields"] = fields
    return dataset


def _convert_source(view):
    """Returns the Ossie `source` for a view: its table name, or the SQL of its
    derived table.

    A view with neither uses its own name as the table, as Looker does.
    Native derived tables (`explore_source`) are built from an explore, not
    SQL, so they have no Ossie source; the view is skipped with a warning.
    """
    name = view["name"]
    derived = view.get("derived_table")
    if derived is not None:
        if "sql" in derived:
            return derived["sql"].strip()
        _warn(f"view '{name}'", "native derived tables (explore_source) have no "
              "Ossie source; skipping")
        return None

    table = view.get("sql_table_name", name).strip()
    # Backticks are BigQuery quoting, not part of the name.
    return table.replace("`", "")


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def _convert_dimension(view_name, dim, dimensions):
    """Converts one LookML dimension to an Ossie field, or None (with a
    warning) if it has no Ossie form."""
    name = dim["name"]
    scope = f"dimension '{view_name}.{name}'"
    lookml_type = dim.get("type", "string")  # Looker's default
    if lookml_type not in _VALUE_TYPES:
        _warn(scope, f"type '{lookml_type}' has no Ossie form; skipping")
        return None

    try:
        sql = _resolve_sql(view_name, name, dimensions, ())
    except _Unsupported as e:
        _warn(scope, f"{e}; skipping")
        return None
    try:
        sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        _warn(scope, f"SQL '{sql}' could not be parsed; skipping")
        return None

    field = {
        "name": name,
        "expression": {"dialects": [{"dialect": DIALECT, "expression": sql}]},
    }
    if lookml_type in _DATATYPES:
        field["datatype"] = _DATATYPES[lookml_type]
    for key in ("label", "description"):
        if key in dim:
            field[key] = dim[key]
    return field


def _resolve_sql(view_name, name, dimensions, path):
    """Returns the SQL of dimension `name` with every LookML substitution
    replaced, so it reads as a plain expression over the view's columns.

    `path` is the chain of dimensions being resolved, to catch cycles.
    Raises _Unsupported for references Ossie cannot express.
    """
    if name in path:
        raise _Unsupported(f"circular reference {' -> '.join(path + (name,))}")
    dim = dimensions.get(name)
    if dim is None:
        raise _Unsupported(f"references '${{{name}}}', which is not a dimension of this view")

    # A dimension without `sql` reads the column of the same name.
    sql = dim.get("sql", f"${{TABLE}}.{name}").strip()
    if "{%" in sql or "{{" in sql:
        raise _Unsupported("Liquid templating has no Ossie form")

    def substitute(match):
        ref = match.group(1).strip()
        if ref == "TABLE":
            return "${TABLE}"  # handled below, with the column after it
        parts = ref.split(".")
        if len(parts) == 2 and parts[0] == view_name:
            parts = parts[1:]  # ${this_view.field} is ${field}
        if len(parts) != 1:
            raise _Unsupported(f"'${{{ref}}}' references another view or a "
                               f"field property")
        inner = _resolve_sql(view_name, parts[0], dimensions, path + (name,))
        # Parenthesize so the inlined SQL keeps its precedence.
        return inner if _is_identifier(inner) else f"({inner})"

    sql = _SUBSTITUTION.sub(substitute, sql)
    # ${TABLE}.col is the source column `col`, which is what a bare name
    # means in an Ossie field expression.
    sql = sql.replace("${TABLE}.", "")
    if "${TABLE}" in sql:
        raise _Unsupported("'${TABLE}' is used without a column")
    return sql


def _is_identifier(sql):
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|`[^`]+`", sql) is not None
