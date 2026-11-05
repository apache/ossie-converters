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
    LookML dimension_group-> one temporal Ossie field holding the raw value.
                             Ossie has no time grain, so the timeframes
                             (date, week, month, ...) are not carried over.
                             See _group_field_name for how the field is named
    LookML measure        -> Ossie dataset-scoped metric. Metrics reference
                             fields, so a column no dimension exposes gets a
                             field of its own. `type: number` measures inline
                             the measures they reference, since Ossie metrics
                             cannot reference metrics. `filters:` become
                             `FILTER (WHERE ...)`
    LookML explore        -> Ossie model: the base view and every joined view
                             become datasets (named by their alias)
    LookML join           -> Ossie relationship from the many side to the one
                             side, keyed by the sql_on or foreign_key columns

One Ossie model per explore: with several explores, pick one. Without any,
every view becomes a dataset and the model has no relationships.

Expressions are tagged BIGQUERY, as written in LookML, except metrics with a
`FILTER (WHERE ...)` clause: that is Ossie's expression language, not
BigQuery's, so those are transpiled with sqlglot and tagged OSSIE_SQL_2026.
"""

import re
import warnings

import lkml
import sqlglot
import yaml
from sqlglot import exp

from .ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


DEFAULT_MODEL_NAME = "lookml_model"

DIALECT = "BIGQUERY"
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


class _Unsupported(Exception):
    """A LookML construct with no Ossie form. The message says which."""


def _warn(scope, msg):
    warnings.warn(f"[{scope}] {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_lookml_to_ossie(files, explore=None, model_name=None):
    """Top-level entry point. Parses LookML files, converts them, returns an
    Ossie document as a YAML string.

    Args:
        files: {relative filename: LookML string}.
        explore: Name of the explore to convert. Optional when the files
            define at most one explore.
        model_name: Name of the Ossie semantic model. Defaults to the
            explore's name.

    Returns:
        Ossie YAML string.

    Raises:
        OssieConversionError: If the input cannot be converted.
    """
    if not isinstance(files, dict) or not files:
        raise OssieConversionError("Expected a non-empty mapping of {filename: LookML}")

    views, explores = _load(files)
    if explore is None and len(explores) > 1:
        raise OssieConversionError(
            f"Found {len(explores)} explores; choose one to convert: "
            + ", ".join(sorted(explores))
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
        converted = {name: _convert_view(view) for name, view in views.items()}
        model["datasets"] = [ds for ds, _ in converted.values() if ds is not None]
        return yaml.safe_dump(model, sort_keys=False)

    spec = explores[explore]
    if "description" in spec:
        model["description"] = spec["description"]
    datasets, relationships = _convert_explore(spec, views)
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    return yaml.safe_dump(model, sort_keys=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _load(files):
    """Parses every file and returns its views and explores by name,
    rejecting duplicate names."""
    views = {}
    explores = {}
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
        for explore in parsed.get("explores", []):
            name = explore["name"]
            if name.startswith("+"):
                _warn(f"explore '{name}'", "refinements are not supported yet; skipping")
                continue
            if name in explores:
                raise OssieConversionError(
                    f"Explore '{name}' is defined in both '{explores[name][0]}' and '{fname}'"
                )
            explores[name] = (fname, explore)
    return (
        {name: view for name, (_, view) in views.items()},
        {name: explore for name, (_, explore) in explores.items()},
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _convert_view(view, alias=None):
    """Converts one LookML view to an Ossie dataset named `alias` (default:
    the view's name).

    Returns (dataset, ref_names): `ref_names` maps each LookML field name the
    view exposes (`created_raw` for a group) to its Ossie field name. The
    dataset is None if the view has no SQL source Ossie can express.
    """
    name = view["name"]
    source = _convert_source(view)
    if source is None:
        return None, {}
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
    builder = _MetricBuilder(name, measures, dimensions, groups, fields, ref_names)
    metrics = [m for m in (builder.convert(m) for m in measures.values()) if m]

    if primary_key:
        dataset["primary_key"] = primary_key
    if fields:
        dataset["fields"] = fields
    if metrics:
        dataset["metrics"] = metrics
    return dataset, ref_names


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
        sqlglot.parse_one(sql, read="bigquery")
    except sqlglot.errors.ParseError:
        _warn(scope, f"SQL '{sql}' could not be parsed; skipping")
        return None

    field = {
        "name": name,
        "expression": {"dialects": [{"dialect": DIALECT, "expression": sql}]},
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
    """Converts the measures of one view to dataset-scoped Ossie metrics.

    Ossie metric expressions reference the dataset's fields, never its source
    columns. A measure over `${TABLE}.col` uses the field that reads `col`;
    if there is none, a field for `col` is added to the dataset (only once the
    measure converts, so a skipped measure leaves no stray field).
    """

    def __init__(self, view_name, measures, dimensions, groups, fields, ref_names):
        self.view_name = view_name
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

    def convert(self, measure):
        """Returns the Ossie metric for `measure`, or None with a warning."""
        name = measure["name"]
        scope = f"measure '{self.view_name}.{name}'"
        new_fields = {}  # column SQL -> field name, added if the measure converts
        try:
            sql = self._expression(measure, (), new_fields)
        except _Unsupported as e:
            _warn(scope, f"{e}; skipping")
            return None
        try:
            tree = sqlglot.parse_one(sql, read="bigquery")
        except sqlglot.errors.ParseError:
            _warn(scope, f"SQL '{sql}' could not be parsed; skipping")
            return None

        for column, field_name in new_fields.items():
            self.fields.append({
                "name": field_name,
                "expression": {"dialects": [{"dialect": DIALECT, "expression": column}]},
            })
            self.by_column[column] = field_name
            self.taken.add(field_name)

        if tree.find(exp.Filter) is not None:
            # BigQuery has no FILTER clause; Ossie's expression language does.
            dialect, sql = OSSIE_DIALECT, tree.sql()
        else:
            dialect = DIALECT
        metric = {
            "name": name,
            "expression": {"dialects": [{"dialect": dialect, "expression": sql}]},
        }
        if measure.get("type") in _INTEGER_MEASURES:
            metric["datatype"] = "Integer"
        if "description" in measure:
            metric["description"] = measure["description"]
        return metric

    def _expression(self, measure, path, new_fields):
        """Returns the SQL for a measure, over field names."""
        name = measure["name"]
        if name in path:
            raise _Unsupported(f"circular reference {' -> '.join(path + (name,))}")
        path = path + (name,)
        lookml_type = measure.get("type")
        sql = measure.get("sql", "").strip()

        if lookml_type == "count":
            body = "COUNT(*)"
        elif lookml_type in _AGGREGATES:
            if not sql:
                raise _Unsupported(f"type '{lookml_type}' without sql")
            body = _AGGREGATES[lookml_type].format(self._substitute(sql, path, new_fields))
        elif lookml_type == "number":
            if not sql:
                raise _Unsupported("type 'number' without sql")
            if _filter_entries(measure):
                raise _Unsupported("filters on a type 'number' measure")
            return self._substitute(sql, path, new_fields, measures=True)
        else:
            raise _Unsupported(f"type '{lookml_type}' has no Ossie form")

        conditions = [self._filter(field, value) for field, value in _filter_entries(measure)]
        if conditions:
            body = f"{body} FILTER (WHERE {' AND '.join(conditions)})"
        return body

    def _substitute(self, sql, path, new_fields, measures=False):
        """Replaces LookML substitutions in a measure's SQL with field names,
        and, when `measures` is set (type: number), `${measure}` with that
        measure's SQL."""
        if "{%" in sql or "{{" in sql:
            raise _Unsupported("Liquid templating has no Ossie form")
        sql = _TABLE_COLUMN.sub(lambda m: self._column_field(m.group(1), new_fields), sql)

        def substitute(match):
            ref = match.group(1).strip()
            parts = ref.split(".")
            if len(parts) == 2 and parts[0] == self.view_name:
                parts = parts[1:]
            if ref == "TABLE":
                raise _Unsupported("'${TABLE}' is used without a column")
            if len(parts) != 1:
                raise _Unsupported(f"'${{{ref}}}' references another view or a "
                                   f"field property")
            target = parts[0]
            if measures and target in self.measures:
                inner = self._expression(self.measures[target], path, new_fields)
                return inner if _is_atom(inner) else f"({inner})"
            if target in self.ref_names:
                return self.ref_names[target]
            if target in self.measures:
                raise _Unsupported(f"'${{{target}}}' is a measure inside an aggregate")
            _lookup(target, self.dimensions, self.groups)  # raises the reason
            raise _Unsupported(f"references '${{{target}}}', which was skipped")

        return _SUBSTITUTION.sub(substitute, sql)

    def _column_field(self, column, new_fields):
        """Returns the name of the field that reads `column`, planning a new
        one if no field does."""
        if column in self.by_column:
            return self.by_column[column]
        if column in new_fields:
            return new_fields[column]
        base = column.strip("`")
        if not _is_identifier(base):
            raise _Unsupported(f"column {column} is not a valid field name")
        taken = self.taken | set(new_fields.values())
        name, n = base, 1
        while name in taken:
            name = f"{base}_value" if n == 1 else f"{base}_value_{n}"
            n += 1
        new_fields[column] = name
        return name

    def _filter(self, field, value):
        """Translates one `field: "value"` filter to a SQL condition."""
        ref = field.split(".")
        if len(ref) == 2 and ref[0] == self.view_name:
            ref = ref[1:]
        if len(ref) != 1 or ref[0] not in self.dimensions or ref[0] not in self.ref_names:
            raise _Unsupported(f"filter on '{field}', which is not a converted "
                               f"dimension of this view")
        dim = self.dimensions[ref[0]]
        return _filter_condition(self.ref_names[ref[0]], dim.get("type", "string"), value)


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


def _filter_condition(field, lookml_type, value):
    """Translates a Looker filter expression to SQL over `field`.

    Only the forms whose meaning is certain are translated: exact values,
    lists, numeric comparisons, wildcards, yes/no and NULL. Anything else
    (negation, whose NULL handling Looker does not document; ranges; dates;
    escapes) raises _Unsupported, since a wrong filter is a wrong number.
    """
    column = exp.column(field)
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
    """Returns (datasets, relationships) for an explore: one dataset per view
    alias in it (base view first), and one relationship per join."""
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
    ref_names = {}  # alias -> {LookML field name: Ossie field name}
    primary_keys = {}  # alias -> LookML primary key dimension name
    for alias, view_name in aliases.items():
        view = views.get(view_name)
        if view is None:
            raise OssieConversionError(f"Explore '{name}': view '{view_name}' is not defined")
        dataset, refs = _convert_view(view, alias)
        if dataset is None:
            continue
        datasets.append(dataset)
        ref_names[alias] = refs
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
    return datasets, relationships


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
