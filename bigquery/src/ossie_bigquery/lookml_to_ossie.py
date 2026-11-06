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
    LookML dimension      -> Ossie field marked as a dimension (`is_time` for
                             temporal ones); `${TABLE}.col` becomes `col`, and
                             `${other_dimension}` is replaced by that
                             dimension's SQL, since Ossie field expressions
                             reference columns, not other fields
    LookML dimension_group-> one temporal Ossie field holding the raw value.
                             Ossie has no time grain, so the timeframes
                             (date, week, month, ...) are not carried over.
                             See _group_field_name for how the field is named
    LookML measure        -> Ossie dataset-scoped metric. Metrics reference
                             fields, so a column no dimension exposes gets a
                             field of its own. `type: number` measures inline
                             the measures they reference, since Ossie metrics
                             cannot reference metrics. `filters:` become
                             `FILTER (WHERE ...)`. A measure that reads
                             another view of the explore becomes a
                             model-scoped metric over `dataset.field`
    LookML explore        -> Ossie model: the base view and every joined view
                             become datasets (named by their alias)
    LookML join           -> Ossie relationship from the many side to the one
                             side, keyed by the sql_on or foreign_key columns

One Ossie model per explore: with several explores, pick one, or merge them
all into one model (`all_explores`). Without any explore, every view becomes
a dataset and the model has no relationships.

Refinements (`view: +orders`, `explore: +orders`) are merged into what they
refine, in include order, as Looker does: they add fields and joins, and
their parameters override the original's. Then `extends:` is resolved: a view
or explore starts as a copy of those it extends, in order, and its own
definition merges on top. `extension: required` marks a view or explore that
exists only to be extended, so it gets no dataset or model of its own.

Only the views a Looker model can see are converted: the files its
`include:` statements reach, transitively. Two files may define the same view
as long as no model includes both, as in Looker. Without a .model.lkml file,
every file is read.

Every expression is given twice: BIGQUERY, as written in LookML, and ANSI_SQL,
transpiled by sqlglot, which is the dialect most Ossie consumers read. A
metric with a `FILTER (WHERE ...)` clause is ANSI_SQL and OSSIE_SQL_2026
instead: BigQuery has no FILTER clause, and SQL:2003 and Ossie's expression
language both do.
"""

import posixpath
import re
import warnings

import lkml
import sqlglot
import yaml
from sqlglot import exp

from .ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


DEFAULT_MODEL_NAME = "lookml_model"

DIALECT = "BIGQUERY"
ANSI_DIALECT = "ANSI_SQL"
OSSIE_DIALECT = "OSSIE_SQL_2026"

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
_VALUE_TYPES = set(_DATATYPES) | {"number", "date_time"}

_TEMPORAL_DATATYPES = {"Date", "DateTime", "DateTimeTz"}

# Dimension `type: date_time` -> Ossie `datatype`. Looker reads a timestamp
# and only converts its display timezone, so the value is the same instant.
# `type: date` is not here: Looker converts the timestamp's timezone and then
# truncates it to a date, which the raw SQL does not do.
_DATE_TYPES = {"date_time": "DateTimeTz"}

# dimension_group `datatype` -> Ossie `datatype`. Looker's default is
# timestamp. epoch and yyyymmdd store a number, not a temporal value.
_GROUP_DATATYPES = {
    "date": "Date",
    "datetime": "DateTime",
    "timestamp": "DateTimeTz",
}

# A LookML substitution: ${TABLE}, ${field}, ${view.field}, ${field._sql}.
_SUBSTITUTION = re.compile(r"\$\{([^}]*)\}")


# LookML measure `type` -> SQL aggregate over the measure's `sql`.
_AGGREGATES = {
    "sum": "SUM({})",
    "average": "AVG({})",
    "min": "MIN({})",
    "max": "MAX({})",
    "count_distinct": "COUNT(DISTINCT {})",
}

# Measure types that always return a whole number.
_INTEGER_MEASURES = {"count", "count_distinct"}

# A plain number in a Looker filter, with an optional comparison.
_NUMBER_FILTER = re.compile(r"(>=|<=|>|<)?\s*(-?\d+(?:\.\d+)?)")

# ${TABLE}.col, with the column bare or backtick-quoted.
_TABLE_COLUMN = re.compile(r"\$\{\s*TABLE\s*\}\.(`[^`]+`|[A-Za-z_][A-Za-z0-9_]*)")


def _expression_dialects(sql, tree):
    """The Ossie `expression` for SQL read as BigQuery (`tree` is its parse):
    BIGQUERY as written plus ANSI_SQL, or, with a FILTER clause, which
    BigQuery lacks, ANSI_SQL plus OSSIE_SQL_2026."""
    ansi = tree.sql()
    if tree.find(exp.Filter) is not None:
        dialects = [(ANSI_DIALECT, ansi), (OSSIE_DIALECT, ansi)]
    else:
        dialects = [(DIALECT, sql), (ANSI_DIALECT, ansi)]
    return {"dialects": [{"dialect": d, "expression": e} for d, e in dialects]}


class _Unsupported(Exception):
    """A LookML construct with no Ossie form. The message says which."""


class _CrossView(_Unsupported):
    """A measure that reads another view: not dataset-scoped, but it can be
    a model-scoped metric when the explore joins that view."""


def _warn(scope, msg):
    warnings.warn(f"[{scope}] {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_lookml_to_ossie(files, explore=None, model_name=None, model=None,
                            all_explores=False):
    """Top-level entry point. Parses LookML files, converts them, returns an
    Ossie document as a YAML string.

    Args:
        files: {relative filename: LookML string}.
        explore: Name of the explore to convert. Optional when the files
            define at most one explore.
        model_name: Name of the Ossie semantic model. Defaults to the
            explore's name.
        model: Name of the Looker model (`<model>.model.lkml`) whose
            includes define the views. Optional unless the explore is in
            several models.
        all_explores: Merge every explore into one model instead of
            converting one: the datasets of all of them, and their
            relationships and metrics without duplicates. This reads back a
            whole Ossie model exported by ossie_to_lookml, which makes one
            explore per dataset on the many side of a relationship.

    Returns:
        Ossie YAML string.

    Raises:
        OssieConversionError: If the input cannot be converted.
    """
    if not isinstance(files, dict) or not files:
        raise OssieConversionError("Expected a non-empty mapping of {filename: LookML}")

    if all_explores and explore is not None:
        raise OssieConversionError("Choose an explore or all_explores, not both")
    views, explores = _Project(files).scope(model, explore)
    if explore is None:
        # Explores that exist only to be extended are not choices.
        explores = {n: e for n, e in explores.items() if e.get("extension") != "required"}
    if all_explores and explores:
        return yaml.safe_dump(_merge_explores(explores, views, model_name), sort_keys=False)
    if explore is None and len(explores) > 1:
        raise OssieConversionError(
            f"Found {len(explores)} explores; choose one to convert ("
            + ", ".join(sorted(explores)) + "), or merge them with all_explores"
        )
    if explore is None and explores:
        explore = next(iter(explores))
    if explore is not None and explore not in explores:
        raise OssieConversionError(
            f"Explore '{explore}' not found. Explores: "
            + (", ".join(sorted(explores)) or "none")
        )

    model = {
        "version": SUPPORTED_VERSION,
        "name": model_name or explore or DEFAULT_MODEL_NAME,
    }
    if explore is None:
        converted = {name: _convert_view(view) for name, view in views.items()
                     if view.get("extension") != "required"}
        model["datasets"] = [ds for ds, _, _ in converted.values() if ds is not None]
        for _, _, builder in converted.values():
            for measure, reason in builder.deferred if builder else ():
                _warn(f"measure '{builder.view_name}.{measure['name']}'",
                      f"{reason}; without an explore there is no join to reach it; skipping")
        return yaml.safe_dump(model, sort_keys=False)

    spec = explores[explore]
    if "description" in spec:
        model["description"] = spec["description"]
    datasets, relationships, metrics = _convert_explore(spec, views)
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    return yaml.safe_dump(model, sort_keys=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class _Project:
    """A LookML project as {path: text}, parsed on demand, with Looker's
    include resolution."""

    def __init__(self, files):
        self.files = {_normalize_path(path): text for path, text in files.items()}
        self._parsed = {}

    def parsed(self, path):
        if path not in self._parsed:
            try:
                self._parsed[path] = lkml.load(self.files[path])
            except SyntaxError as e:
                raise OssieConversionError(f"File '{path}': invalid LookML: {e}")
        return self._parsed[path]

    def scope(self, model, explore):
        """Returns (views, explores) by name, as the chosen Looker model
        sees them. Without model files, every file is in scope."""
        models = {
            posixpath.basename(path)[: -len(".model.lkml")]: path
            for path in sorted(self.files) if path.endswith(".model.lkml")
        }
        if model is not None:
            if model not in models:
                raise OssieConversionError(
                    f"Model '{model}' not found. Models: {', '.join(models) or 'none'}"
                )
            return self._collect(self._closure(models[model]))
        if not models:
            return self._collect(sorted(self.files))

        # Pick the model whose includes define the explore (or any explore).
        found = {}
        for name, path in models.items():
            for p in self._closure(path):
                for e in self.parsed(p).get("explores", []):
                    if explore in (None, e["name"]):
                        found.setdefault(name, e["name"])
        if not found:
            if explore is None:
                return self._collect(sorted(self.files))
            raise OssieConversionError(
                f"Explore '{explore}' not found in any model. Models: {', '.join(models)}"
            )
        if len(found) > 1:
            what = f"Explore '{explore}' is" if explore else "Explores are"
            raise OssieConversionError(
                f"{what} in models {', '.join(found)}; choose one with model="
            )
        return self._collect(self._closure(models[next(iter(found))]))

    def _closure(self, path):
        """The files `path` includes, transitively, starting with itself."""
        seen = [path]
        for current in seen:
            for pattern in self.parsed(current).get("includes", []):
                for match in self._resolve(pattern, current):
                    if match not in seen:
                        seen.append(match)
        return seen

    def _resolve(self, pattern, from_path):
        """The project files an `include:` pattern matches, in path order.

        A leading `/` starts at the project root, anything else at the
        including file's folder. `*` stays within a folder, `**` crosses
        folders, and a pattern may leave off `.lkml` (`*.view`).
        """
        scope = f"file '{from_path}'"
        if pattern.endswith((".lookml", ".dashboard")):
            return []  # dashboards are not part of the semantic model
        if pattern.startswith("//"):
            _warn(scope, f"include '{pattern}' is in another project; skipping")
            return []
        if pattern.startswith("/"):
            full = pattern.lstrip("/")
        else:
            full = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), pattern))
        regex = _include_regex(full)
        matches = [p for p in sorted(self.files)
                   if regex.fullmatch(p) or regex.fullmatch(p.removesuffix(".lkml"))]
        if not matches:
            _warn(scope, f"include '{pattern}' matches no files")
        return matches

    def _collect(self, paths):
        """Returns the views and explores defined in `paths`, with their
        refinements merged in, rejecting a name defined twice (Looker
        rejects it too)."""
        views = {}
        explores = {}
        refinements = []
        for fname in paths:
            parsed = self.parsed(fname)
            for kind, found in (("view", views), ("explore", explores)):
                for entry in parsed.get(f"{kind}s", []):
                    name = entry["name"]
                    if name.startswith("+"):
                        refinements.append((kind, found, fname, entry))
                        continue
                    if name in found:
                        raise OssieConversionError(
                            f"{kind.capitalize()} '{name}' is defined in both "
                            f"'{found[name][0]}' and '{fname}'"
                        )
                    found[name] = (fname, entry)

        # Refinements apply after every definition is known, in the order
        # their files are included, so a later one wins.
        for kind, found, fname, refinement in refinements:
            name = refinement["name"][1:]
            if name not in found:
                _warn(f"{kind} '+{name}'", f"refines a {kind} the model does not "
                      f"define or include; skipping")
                continue
            base_file, base = found[name]
            found[name] = (base_file, _merge(base, refinement))

        # extends is resolved after refinements, so a refinement can add one.
        for kind, found in (("view", views), ("explore", explores)):
            resolved = {}
            for name in found:
                _resolve_extends(kind, name, found, resolved, ())
            for name, entry in resolved.items():
                found[name] = (found[name][0], entry)
        return (
            {name: view for name, (_, view) in views.items()},
            {name: explore for name, (_, explore) in explores.items()},
        )


# LookML parameters that hold named entries, which a refinement merges by
# name instead of replacing.
_NAMED_LISTS = (
    "dimensions", "dimension_groups", "measures", "filters", "parameters",
    "sets", "joins",
)


def _merge(base, refinement):
    """Merges a refinement into a view or explore, as Looker does: a named
    entry (field, join) is merged parameter by parameter, so the refinement
    can add or override parameters; any other parameter is replaced."""
    merged = dict(base)
    for key, value in refinement.items():
        if key == "name":
            continue
        if key in _NAMED_LISTS:
            entries = {entry["name"]: entry for entry in merged.get(key, [])}
            for entry in value:
                entries[entry["name"]] = {**entries.get(entry["name"], {}), **entry}
            merged[key] = list(entries.values())
        else:
            merged[key] = value
    return merged


def _resolve_extends(kind, name, found, resolved, path):
    """Returns the view or explore `name` with what it extends merged in,
    memoized in `resolved`. A missing or circular parent is skipped with a
    warning, so the entry keeps only its own definition from it."""
    if name in resolved:
        return resolved[name]
    entry = found[name][1]
    parents = [p for group in entry.get("extends__all", []) for p in group]
    result = {}
    for parent in parents:
        scope = f"{kind} '{name}'"
        if parent in path + (name,):
            _warn(scope, f"extends '{parent}' in a cycle; ignoring that extends")
            continue
        if parent not in found:
            _warn(scope, f"extends '{parent}', which the model does not define or "
                  f"include; its fields are missing")
            continue
        inherited = dict(_resolve_extends(kind, parent, found, resolved, path + (name,)))
        # Being abstract is not inherited.
        inherited.pop("extension", None)
        result = _merge(result, inherited)
    own = {k: v for k, v in entry.items() if k != "extends__all"}
    resolved[name] = _merge(result, own) if result else own
    resolved[name]["name"] = name
    return resolved[name]


def _normalize_path(path):
    path = path.replace("\\", "/")
    return posixpath.normpath(path).lstrip("/")


def _include_regex(pattern):
    """Translates an include glob to a regex over project paths."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _convert_view(view, alias=None):
    """Converts one LookML view to an Ossie dataset named `alias` (default:
    the view's name).

    Returns (dataset, ref_names, builder): `ref_names` maps each LookML field
    name the view exposes (`created_raw` for a group) to its Ossie field name;
    `builder` holds the measures that read other views, for the explore to
    convert as model-scoped metrics. The dataset is None if the view has no
    SQL source Ossie can express.
    """
    name = view["name"]
    source = _convert_source(view)
    if source is None:
        return None, {}, None
    dataset = {"name": alias or name, "source": source}

    dimensions = {d["name"]: d for d in view.get("dimensions", [])}
    groups = {g["name"]: g for g in view.get("dimension_groups", [])}
    fields = []
    primary_key = []
    for dim_name, dim in dimensions.items():
        field = _convert_dimension(name, dim, dimensions, groups)
        if field is None:
            continue
        fields.append(field)
        if dim.get("primary_key") == "yes":
            primary_key.append(dim_name)
    # How a measure's `${ref}` reads each converted dimension: its field name.
    ref_names = {f["name"]: f["name"] for f in fields}
    taken = set(dimensions)
    for group in groups.values():
        field = _convert_dimension_group(name, group, dimensions, groups, taken)
        if field is not None:
            fields.append(field)
            taken.add(field["name"])
            ref_names[f"{group['name']}_raw"] = field["name"]

    measures = {m["name"]: m for m in view.get("measures", [])}
    builder = _MetricBuilder(name, dataset, measures, dimensions, groups, fields, ref_names)
    metrics = [m for m in (builder.convert(m) for m in measures.values()) if m]

    if primary_key:
        dataset["primary_key"] = primary_key
    if fields:
        dataset["fields"] = fields
    if metrics:
        dataset["metrics"] = metrics
    return dataset, ref_names, builder


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

def _convert_dimension(view_name, dim, dimensions, groups):
    """Converts one LookML dimension to an Ossie field, or None (with a
    warning) if it has no Ossie form."""
    name = dim["name"]
    scope = f"dimension '{view_name}.{name}'"
    lookml_type = dim.get("type", "string")  # Looker's default
    if lookml_type == "date":
        _warn(scope, "type 'date' truncates a timestamp to a date in the query "
              "timezone, which has no Ossie form; skipping")
        return None
    if lookml_type not in _VALUE_TYPES:
        _warn(scope, f"type '{lookml_type}' has no Ossie form; skipping")
        return None
    datatype = _DATATYPES.get(lookml_type) or _DATE_TYPES.get(lookml_type)
    return _build_field(view_name, dim, name, datatype, scope, dimensions, groups)


def _convert_dimension_group(view_name, group, dimensions, groups, taken):
    """Converts one LookML dimension_group to a single temporal Ossie field,
    or None (with a warning) if it has no Ossie form."""
    name = group["name"]
    scope = f"dimension_group '{view_name}.{name}'"
    if group.get("type", "time") != "time":
        _warn(scope, f"type '{group['type']}' has no Ossie form; skipping")
        return None
    lookml_datatype = group.get("datatype", "timestamp")  # Looker's default
    datatype = _GROUP_DATATYPES.get(lookml_datatype)
    if datatype is None:
        _warn(scope, f"datatype '{lookml_datatype}' stores a number, not a "
              "temporal value; skipping")
        return None
    field = _build_field(view_name, group, None, datatype, scope, dimensions, groups)
    if field is not None:
        field["name"] = _group_field_name(group["name"], field, taken)
    return field


def _group_field_name(group_name, field, taken):
    """Names the Ossie field for a dimension_group.

    The group name itself (`created`) would read as a different thing than a
    timestamp, so the field takes the name of the column the group reads
    (`created_at`) when its SQL is a plain column. This also round-trips with
    ossie_to_lookml, which turns field `order_date` into group `order` over
    `${TABLE}.order_date`.

    Otherwise (computed SQL, or a column name another field already uses) it
    falls back to `<group>_raw`, Looker's own name for the group's raw value.
    Open for review: other naming schemes are easy to swap in here.
    """
    sql = field["expression"]["dialects"][0]["expression"]
    if _is_identifier(sql) and not sql.startswith("`") and sql not in taken:
        return sql
    return f"{group_name}_raw"


def _build_field(view_name, entry, name, datatype, scope, dimensions, groups):
    """Builds an Ossie field from a dimension or dimension_group, resolving
    its SQL. Returns None (with a warning) if the SQL has no Ossie form."""
    try:
        sql = _resolve_sql(view_name, entry, dimensions, groups, ())
    except _Unsupported as e:
        _warn(scope, f"{e}; skipping")
        return None
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        _warn(scope, f"SQL '{sql}' could not be parsed; skipping")
        return None

    # A LookML dimension can be grouped and filtered by, which is what
    # Ossie's `dimension` marks. Fields added for measure columns are not.
    field = {
        "name": name,
        "expression": _expression_dialects(sql, tree),
        "dimension": {"is_time": datatype in _TEMPORAL_DATATYPES},
    }
    if datatype is not None:
        field["datatype"] = datatype
    for key in ("label", "description"):
        if key in entry:
            field[key] = entry[key]
    return field


def _resolve_sql(view_name, entry, dimensions, groups, path):
    """Returns the SQL of a dimension or dimension_group with every LookML
    substitution replaced, so it reads as a plain expression over the view's
    columns.

    `path` is the chain of names being resolved, to catch cycles.
    Raises _Unsupported for references Ossie cannot express.
    """
    name = entry["name"]
    if name in path:
        raise _Unsupported(f"circular reference {' -> '.join(path + (name,))}")

    # A dimension without `sql` reads the column of the same name.
    sql = entry.get("sql", f"${{TABLE}}.{name}").strip()
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
        target = _lookup(parts[0], dimensions, groups)
        inner = _resolve_sql(view_name, target, dimensions, groups, path + (name,))
        # Parenthesize so the inlined SQL keeps its precedence.
        return inner if _is_identifier(inner) else f"({inner})"

    sql = _SUBSTITUTION.sub(substitute, sql)
    # ${TABLE}.col is the source column `col`, which is what a bare name
    # means in an Ossie field expression.
    sql = sql.replace("${TABLE}.", "")
    if "${TABLE}" in sql:
        raise _Unsupported("'${TABLE}' is used without a column")
    return sql


def _lookup(ref, dimensions, groups):
    """Returns the dimension, or the dimension_group, that `${ref}` reads.

    A group is referenced by timeframe (`${created_date}`). Only `_raw` is the
    group's own value; the others truncate or extract it, which an Ossie
    field expression cannot reference without restating Looker's timezone
    handling.
    """
    if ref in dimensions:
        return dimensions[ref]
    # Longest group name first, so `created_at_raw` prefers group `created_at`.
    for group in sorted(groups, key=len, reverse=True):
        if ref.startswith(f"{group}_"):
            timeframe = ref[len(group) + 1:]
            if timeframe == "raw":
                return groups[group]
            raise _Unsupported(
                f"references '${{{ref}}}', the '{timeframe}' timeframe of "
                f"dimension_group '{group}'; only '{group}_raw' has an Ossie form"
            )
    raise _Unsupported(f"references '${{{ref}}}', which is not a dimension of this view")


def _is_identifier(sql):
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|`[^`]+`", sql) is not None


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------

class _MetricBuilder:
    """Converts the measures of one view to Ossie metrics.

    Ossie metric expressions reference the dataset's fields, never its source
    columns. A measure over `${TABLE}.col` uses the field that reads `col`;
    if there is none, a field for `col` is added to the dataset (only once the
    measure converts, so a skipped measure leaves no stray field).

    A measure that reads only its own view becomes a dataset-scoped metric
    over bare field names. One that reads another view is set aside in
    `deferred`; given the explore's builders (`explore`), `convert` turns it
    into a model-scoped metric over `dataset.field`.
    """

    def __init__(self, view_name, dataset, measures, dimensions, groups, fields, ref_names):
        self.view_name = view_name
        self.dataset = dataset
        self.alias = dataset["name"]
        self.measures = measures
        self.dimensions = dimensions
        self.groups = groups
        self.fields = fields          # the dataset's fields; added to in place
        self.ref_names = ref_names    # LookML reference -> Ossie field name
        self.by_column = {}           # column SQL -> name of the field reading it
        for field in fields:
            sql = field["expression"]["dialects"][0]["expression"]
            if _is_identifier(sql):
                self.by_column.setdefault(sql, field["name"])
        self.taken = {f["name"] for f in fields} | set(measures) | set(dimensions) | set(groups)
        self.deferred = []            # (measure, reason) for measures reading other views

    def convert(self, measure, explore=None):
        """Returns the Ossie metric for `measure`, or None. Without `explore`
        it is dataset-scoped, and a measure reading another view is deferred;
        with `explore` ({alias: builder}) it is model-scoped."""
        name = measure["name"]
        scope = f"measure '{self.view_name}.{name}'"
        new_fields = {}  # builder -> {column SQL: field name}, added if the measure converts
        try:
            sql = self._expression(measure, (), new_fields, explore)
        except _CrossView as e:
            if explore is None:
                self.deferred.append((measure, str(e)))
                return None
            _warn(scope, f"{e}; skipping")
            return None
        except _Unsupported as e:
            _warn(scope, f"{e}; skipping")
            return None
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except sqlglot.errors.ParseError:
            _warn(scope, f"SQL '{sql}' could not be parsed; skipping")
            return None

        for builder, columns in new_fields.items():
            builder._add_fields(columns)

        metric = {"name": name, "expression": _expression_dialects(sql, tree)}
        if measure.get("type") in _INTEGER_MEASURES:
            metric["datatype"] = "Integer"
        if "description" in measure:
            metric["description"] = measure["description"]
        return metric

    def _add_fields(self, columns):
        for column, field_name in columns.items():
            self.fields.append({
                "name": field_name,
                "expression": _expression_dialects(
                    column, sqlglot.parse_one(column, read="bigquery")),
            })
            self.by_column[column] = field_name
            self.taken.add(field_name)
        if self.fields:
            self.dataset["fields"] = self.fields

    def _ref(self, field, explore):
        """How a metric names one of this view's fields."""
        return f"{self.alias}.{field}" if explore is not None else field

    def _expression(self, measure, path, new_fields, explore, datasets=None):
        """Returns the SQL for a measure, over field names. `datasets`
        collects the aliases its aggregate reads, to check FILTER."""
        name = measure["name"]
        if (self.alias, name) in path:
            chain = " -> ".join(n for _, n in path + ((self.alias, name),))
            raise _Unsupported(f"circular reference {chain}")
        path = path + ((self.alias, name),)
        lookml_type = measure.get("type")
        sql = measure.get("sql", "").strip()

        if lookml_type == "number":
            if not sql:
                raise _Unsupported("type 'number' without sql")
            if _filter_entries(measure):
                raise _Unsupported("filters on a type 'number' measure")
            return self._substitute(sql, path, new_fields, explore, set(), measures=True)

        read = {self.alias}
        if lookml_type == "count":
            body = "COUNT(*)" if explore is None else self._count_rows(explore)
        elif lookml_type in _AGGREGATES:
            if not sql:
                raise _Unsupported(f"type '{lookml_type}' without sql")
            read = set()
            body = _AGGREGATES[lookml_type].format(
                self._substitute(sql, path, new_fields, explore, read))
        else:
            raise _Unsupported(f"type '{lookml_type}' has no Ossie form")

        conditions = [self._filter(field, value, explore, read)
                      for field, value in _filter_entries(measure)]
        if conditions:
            if len(read) > 1:
                # Ossie: the FILTER predicate reads the aggregate's dataset only.
                raise _Unsupported("filters and aggregate read different views "
                                   f"({', '.join(sorted(read))})")
            body = f"{body} FILTER (WHERE {' AND '.join(conditions)})"
        return body

    def _count_rows(self, explore):
        """COUNT(*) in a model-scoped metric would count joined rows, which a
        fanning join multiplies; Looker counts the view's primary keys."""
        keys = [d["name"] for d in self.dimensions.values() if d.get("primary_key") == "yes"]
        if not keys or keys[0] not in self.ref_names:
            raise _Unsupported(f"count of view '{self.alias}' in a metric across "
                               "views needs its primary key")
        return f"COUNT(DISTINCT {self._ref(self.ref_names[keys[0]], explore)})"

    def _substitute(self, sql, path, new_fields, explore, read, measures=False):
        """Replaces LookML substitutions in a measure's SQL with field names,
        and, when `measures` is set (type: number), `${measure}` with that
        measure's SQL. Adds the aliases it reads to `read`."""
        if "{%" in sql or "{{" in sql:
            raise _Unsupported("Liquid templating has no Ossie form")

        def column(match):
            read.add(self.alias)
            return self._ref(self._column_field(match.group(1), new_fields), explore)

        sql = _TABLE_COLUMN.sub(column, sql)

        def substitute(match):
            ref = match.group(1).strip()
            if ref == "TABLE":
                raise _Unsupported("'${TABLE}' is used without a column")
            parts = ref.split(".")
            if len(parts) == 2 and parts[0] in (self.view_name, self.alias):
                parts = parts[1:]
            if len(parts) == 2:
                return self._other_view(parts, path, new_fields, explore, read, measures)
            if len(parts) != 1:
                raise _Unsupported(f"'${{{ref}}}' references a field property")
            return self._own(parts[0], path, new_fields, explore, read, measures)

        return _SUBSTITUTION.sub(substitute, sql)

    def _own(self, target, path, new_fields, explore, read, measures):
        """Resolves `${target}` in this view."""
        if measures and target in self.measures:
            inner = self._expression(self.measures[target], path, new_fields, explore)
            return inner if _is_atom(inner) else f"({inner})"
        if target in self.ref_names:
            read.add(self.alias)
            return self._ref(self.ref_names[target], explore)
        if target in self.measures:
            raise _Unsupported(f"'${{{target}}}' is a measure inside an aggregate")
        _lookup(target, self.dimensions, self.groups)  # raises the reason
        raise _Unsupported(f"references '${{{target}}}', which was skipped")

    def _other_view(self, parts, path, new_fields, explore, read, measures):
        """Resolves `${view.target}` for another view of the explore."""
        alias, target = parts
        if target.startswith("_"):
            raise _Unsupported(f"'${{{alias}.{target}}}' references a field property")
        if explore is None:
            raise _CrossView(f"reads view '{alias}'")
        other = explore.get(alias)
        if other is None:
            raise _Unsupported(f"reads view '{alias}', which the explore does not join")
        return other._own(target, path, new_fields, explore, read, measures)

    def _column_field(self, column, new_fields):
        """Returns the name of the field that reads `column`, planning a new
        one if no field does."""
        planned = new_fields.setdefault(self, {})
        if column in self.by_column:
            return self.by_column[column]
        if column in planned:
            return planned[column]
        base = column.strip("`")
        if not _is_identifier(base):
            raise _Unsupported(f"column {column} is not a valid field name")
        taken = self.taken | set(planned.values())
        name, n = base, 1
        while name in taken:
            name = f"{base}_value" if n == 1 else f"{base}_value_{n}"
            n += 1
        planned[column] = name
        return name

    def _filter(self, field, value, explore, read):
        """Translates one `field: "value"` filter to a SQL condition."""
        ref = field.split(".")
        if len(ref) == 2 and ref[0] in (self.view_name, self.alias):
            ref = ref[1:]
        if len(ref) == 2:
            if explore is None:
                raise _CrossView(f"filters on view '{ref[0]}'")
            other = explore.get(ref[0])
            if other is None:
                raise _Unsupported(f"filters on view '{ref[0]}', which the explore does not join")
            return other._filter(ref[1], value, explore, read)
        if len(ref) != 1 or ref[0] not in self.dimensions or ref[0] not in self.ref_names:
            raise _Unsupported(f"filter on '{field}', which is not a converted "
                               f"dimension of this view")
        read.add(self.alias)
        dim = self.dimensions[ref[0]]
        table = self.alias if explore is not None else None
        return _filter_condition(self.ref_names[ref[0]], dim.get("type", "string"), value, table)


def _filter_entries(measure):
    """Returns a measure's filters as (field, value) pairs. lkml reads both
    `filters: [field: "value"]` and the older `filters: { field: value: }`."""
    entries = []
    for entry in measure.get("filters__all", []):
        for item in entry if isinstance(entry, list) else [entry]:
            if "field" in item and "value" in item:
                entries.append((item["field"], item["value"]))
            else:
                entries.extend(item.items())
    return entries


def _filter_condition(field, lookml_type, value, table=None):
    """Translates a Looker filter expression to SQL over `field`.

    Only the forms whose meaning is certain are translated: exact values,
    lists, numeric comparisons, wildcards, yes/no and NULL. Anything else
    (negation, whose NULL handling Looker does not document; ranges; dates;
    escapes) raises _Unsupported, since a wrong filter is a wrong number.
    """
    column = exp.column(field, table=table)
    text = value.strip()
    unsupported = _Unsupported(f"filter '{field}: \"{value}\"' has no exact Ossie form")

    if text.upper() == "NULL":
        return column.is_(exp.Null()).sql("bigquery")
    if text.upper() in ("-NULL", "NOT NULL"):
        return exp.not_(column.is_(exp.Null())).sql("bigquery")

    if lookml_type == "yesno":
        if text.lower() == "yes":
            return column.sql("bigquery")
        if text.lower() == "no":
            # Looker's "No" is anything not true, NULL included.
            return f"NOT COALESCE({column.sql('bigquery')}, FALSE)"
        raise unsupported

    terms = [t.strip() for t in text.split(",")]
    if lookml_type == "number":
        matches = [_NUMBER_FILTER.fullmatch(t) for t in terms]
        if not all(matches):
            raise unsupported
        if len(terms) > 1:
            if any(m.group(1) for m in matches):
                raise unsupported
            return column.isin(*(exp.Literal.number(m.group(2)) for m in matches)).sql("bigquery")
        op, number = matches[0].group(1) or "=", exp.Literal.number(matches[0].group(2))
        return f"{column.sql('bigquery')} {op} {number.sql('bigquery')}"

    if lookml_type not in ("string", "zipcode"):
        raise unsupported
    if any(t.startswith("-") or t.upper() in ("EMPTY", "NULL") or "^" in t or '"' in t
           or not t for t in terms):
        raise unsupported
    wildcard = any("%" in t or "_" in t for t in terms)
    if len(terms) > 1:
        if wildcard:
            raise unsupported
        return column.isin(*(exp.Literal.string(t) for t in terms)).sql("bigquery")
    literal = exp.Literal.string(terms[0])
    condition = column.like(literal) if wildcard else column.eq(literal)
    return condition.sql("bigquery")


def _is_atom(sql):
    """True when `sql` needs no parentheses to keep its precedence when
    inlined: a column or a single function call, such as an aggregate."""
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        return False
    return isinstance(tree, (exp.Column, exp.Func)) and not isinstance(tree, exp.Filter)


# ---------------------------------------------------------------------------
# Explores and joins
# ---------------------------------------------------------------------------

# Explore parameters that change query results but have no Ossie form.
_EXPLORE_FILTERS = (
    "sql_always_where", "sql_always_having", "always_filter",
    "conditionally_filter", "access_filter",
)


def _convert_explore(explore, views):
    """Returns (datasets, relationships, metrics) for an explore: one dataset
    per view alias in it (base view first), one relationship per join, and
    the model-scoped metrics for measures that read other views."""
    name = explore["name"]
    scope = f"explore '{name}'"
    for param in _EXPLORE_FILTERS:
        if param in explore:
            _warn(scope, f"{param} has no Ossie form; the model does not apply it")

    # The base view: `from:` reads another view under the explore's name;
    # `view_name:` makes another view the base under its own name.
    if "from" in explore:
        aliases = {name: explore["from"]}
    else:
        base = explore.get("view_name", name)
        aliases = {base: base}
    base_alias = next(iter(aliases))
    for join in explore.get("joins", []):
        aliases[join["name"]] = join.get("from", join["name"])

    datasets = []
    builders = {}  # alias -> _MetricBuilder
    ref_names = {}  # alias -> {LookML field name: Ossie field name}
    primary_keys = {}  # alias -> LookML primary key dimension name
    for alias, view_name in aliases.items():
        view = views.get(view_name)
        if view is None:
            raise OssieConversionError(
                f"Explore '{name}': view '{view_name}' is not defined in or "
                f"included by its model"
            )
        dataset, refs, builder = _convert_view(view, alias)
        if dataset is None:
            continue
        datasets.append(dataset)
        ref_names[alias] = refs
        builders[alias] = builder
        primary_keys[alias] = next(
            (d["name"] for d in view.get("dimensions", []) if d.get("primary_key") == "yes"),
            None,
        )

    relationships = []
    used = set()
    for join in explore.get("joins", []):
        try:
            relationship = _convert_join(join, base_alias, ref_names, primary_keys)
        except _Unsupported as e:
            _warn(f"join '{name}.{join['name']}'", f"{e}; skipping the relationship")
            continue
        join_type = join.get("type", "left_outer")
        if join_type != "left_outer":
            _warn(f"join '{name}.{join['name']}'", f"type '{join_type}' has no "
                  "Ossie form; the relationship keeps only the keys")
        relationship["name"] = _unique(f"{relationship['from']}_to_{relationship['to']}", used)
        relationships.append(relationship)

    metrics = []
    names = set()
    for alias, builder in builders.items():
        for measure, _ in builder.deferred:
            metric = builder.convert(measure, explore=builders)
            if metric is None:
                continue
            if metric["name"] in names:
                metric["name"] = _unique(f"{alias}_{metric['name']}", names)
            names.add(metric["name"])
            metrics.append(metric)
    return datasets, relationships, metrics


def _merge_explores(explores, views, model_name):
    """Merges every explore into one Ossie model. A dataset, relationship or
    metric that several explores share appears once; a name the explores
    use for different things keeps the first, with a warning."""
    datasets, relationships, metrics = {}, {}, {}
    rel_names = set()
    for name, spec in explores.items():
        ds_list, rel_list, metric_list = _convert_explore(spec, views)
        for dataset in ds_list:
            kept = datasets.setdefault(dataset["name"], dataset)
            if kept is dataset:
                continue
            if kept["source"] != dataset["source"]:
                _warn(f"explore '{name}'", f"'{dataset['name']}' is a different view "
                      "than in an earlier explore; keeping the first")
                continue
            # The same view; a model-scoped metric may have added fields.
            have = {f["name"] for f in kept.get("fields", [])}
            extra = [f for f in dataset.get("fields", []) if f["name"] not in have]
            if extra:
                kept.setdefault("fields", []).extend(extra)
        for rel in rel_list:
            key = (rel["from"], rel["to"], tuple(rel["from_columns"]), tuple(rel["to_columns"]))
            if key not in relationships:
                rel["name"] = _unique(rel["name"], rel_names)
                relationships[key] = rel
        for metric in metric_list:
            kept = metrics.setdefault(metric["name"], metric)
            if kept is not metric and kept["expression"] != metric["expression"]:
                _warn(f"explore '{name}'", f"metric '{metric['name']}' differs from an "
                      "earlier explore's; keeping the first")

    model = {"version": SUPPORTED_VERSION, "name": model_name or DEFAULT_MODEL_NAME,
             "datasets": list(datasets.values())}
    if relationships:
        model["relationships"] = list(relationships.values())
    if metrics:
        model["metrics"] = list(metrics.values())
    return model


def _convert_join(join, base_alias, ref_names, primary_keys):
    """Converts one join to an Ossie relationship (without its name)."""
    alias = join["name"]
    if alias not in ref_names:
        raise _Unsupported(f"view '{join.get('from', alias)}' has no Ossie dataset")
    kind = join.get("relationship", "many_to_one")  # Looker's default
    if kind not in ("many_to_one", "one_to_one", "one_to_many"):
        raise _Unsupported(f"relationship '{kind}' has no Ossie form")
    if "sql_on" in join:
        other, pairs = _parse_sql_on(join["sql_on"], alias)
    elif "foreign_key" in join:
        # foreign_key names a dimension of the base view (or view.dimension)
        # that matches the joined view's primary key.
        other, _, key = join["foreign_key"].rpartition(".")
        other = other or base_alias
        if primary_keys.get(alias) is None:
            raise _Unsupported("foreign_key needs a primary key on the joined view")
        pairs = [(key, primary_keys[alias])]
    else:
        raise _Unsupported("no sql_on or foreign_key")
    if other not in ref_names:
        raise _Unsupported(f"joins to '{other}', which is not in the explore")

    other_columns, join_columns = [], []
    for other_field, join_field in pairs:
        other_columns.append(_join_field(other, other_field, ref_names))
        join_columns.append(_join_field(alias, join_field, ref_names))

    # Ossie relationships point from the many side to the one side.
    if kind == "one_to_many":
        return {"from": alias, "to": other,
                "from_columns": join_columns, "to_columns": other_columns}
    return {"from": other, "to": alias,
            "from_columns": other_columns, "to_columns": join_columns}


def _parse_sql_on(sql_on, alias):
    """Parses `${a.x} = ${b.y} AND ...` into the other view's alias and the
    (other field, joined field) pairs. Raises _Unsupported for anything that
    is not a conjunction of equalities between the joined view and one
    other view."""
    unsupported = _Unsupported(f"sql_on '{sql_on.strip()}' is not equalities "
                               "between two views")
    # ${view.field} is LookML templating; as view.field it parses as a column.
    sql = _SUBSTITUTION.sub(lambda m: m.group(1).strip(), sql_on)
    try:
        tree = sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        raise unsupported
    conditions = list(tree.flatten()) if isinstance(tree, exp.And) else [tree]

    other, pairs = None, []
    for condition in conditions:
        if not (isinstance(condition, exp.EQ)
                and isinstance(condition.left, exp.Column)
                and isinstance(condition.right, exp.Column)):
            raise unsupported
        sides = {condition.left.table: condition.left.name,
                 condition.right.table: condition.right.name}
        if alias not in sides or len(sides) != 2:
            raise unsupported
        [this_other] = set(sides) - {alias}
        if other not in (None, this_other):
            raise unsupported
        other = this_other
        pairs.append((sides[other], sides[alias]))
    return other, pairs


def _join_field(alias, lookml_name, ref_names):
    """Returns the Ossie field a join key names, or raises _Unsupported."""
    field = ref_names[alias].get(lookml_name)
    if field is None:
        raise _Unsupported(f"key '{alias}.{lookml_name}' is not a converted "
                           "dimension (only a group's _raw timeframe can be a key)")
    return field


def _unique(name, used):
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{name}_{n}"
        n += 1
    used.add(candidate)
    return candidate
