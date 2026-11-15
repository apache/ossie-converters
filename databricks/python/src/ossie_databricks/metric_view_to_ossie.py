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

"""IMPORT direction: Metric View v1.1 YAML -> Apache Ossie semantic model.

Shared helpers come from :mod:`ossie_databricks.ossie_converter_common`; the public entry point is
re-exported through :mod:`ossie_databricks.ossie_converter`.
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from typing import Any

from ossie_databricks import expression_dialect, joined_integer_sum
from ossie_databricks.ossie_converter import (
    MV_VERSION,
    OSSIE_VERSION,
    ConversionError,
    Notices,
    Result,
)
from ossie_databricks.ossie_converter_common import (
    CARD_MANY_TO_ONE,
    CARD_ONE_TO_MANY,
    DIALECT_DATABRICKS,
    MAX_JOIN_NODES,
    SELECT_WITH_RE,
    STASH_SOURCE_KEY,
    as_list,
    as_map,
    is_simple_identifier,
    java_split,
    js,
    jstr,
    last_identifier,
    load_yaml,
    qualifier_chain_pattern,
    require_str,
    rewrite_qualifiers,
    str_list,
    truthy,
    validate_source,
    write_stash,
    write_yaml,
)

MODEL_STASH_KEYS = ("filter", "parameters", "materialization")
JOIN_STASH_KEYS = ("rely", "cardinality")
# Metric-View-only column fields with no Apache Ossie representation, preserved verbatim in the
# DATABRICKS stash so an import can restore them. `window` and `partition` are measure-only (they
# live on MeasureExpression); a dimension never carries them.
COLUMN_STASH_KEYS = ("format", "window", "partition")
# Measure-only fields with no Apache Ossie Metric representation. A dimension's display_name maps
# to the Field `label`, but the Ossie Metric schema has no `label`, so a measure's display_name is
# preserved in the DATABRICKS stash instead.
MEASURE_STASH_KEYS = ("display_name",)
NON_EQUI_RE = re.compile(r"[<>!]=|<>|[<>]")
AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE | re.ASCII)
EQ_CLAUSE_RE = re.compile(r"\s*(.+?)\s*=\s*(.+?)\s*")
# A fact wildcard expands to dimensions named after, and equal to, the fact's own columns.
FACT_WILDCARD_RE = re.compile(
    r"\s*(?:source\s*\.\s*)?\*(?:\s+EXCEPT\s*\(.*\))?\s*", re.IGNORECASE | re.ASCII
)


def _equals_ignore_case(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return len(a) == len(b) and (a.upper() == b.upper() or a.lower() == b.lower())


def convert_metric_view_to_ossie(mv_yaml_str: str | None, model_name: str | None = None) -> Result:
    notices = Notices()
    if mv_yaml_str is None or mv_yaml_str.strip() == "":
        raise ConversionError.invalid_input(
            "Invalid Metric View YAML: expected a mapping at the root", "the input is empty"
        )
    try:
        parsed = load_yaml(mv_yaml_str)
    except Exception as e:  # noqa: BLE001 - any parser failure is invalid input
        raise ConversionError.invalid_input(
            f"Invalid Metric View YAML: {e}", f"failed to parse YAML: {e}"
        ) from e
    if not isinstance(parsed, dict):
        raise ConversionError.invalid_input(
            "Invalid Metric View YAML: expected a mapping at the root",
            "it is not a mapping at the root",
        )
    view = parsed
    version_value = view.get("version")
    version = jstr(version_value)
    if version_value is None or version.strip() == "":  # type: ignore[union-attr]
        raise ConversionError.invalid_input(
            "Invalid Metric View YAML: missing required 'version' field",
            "it is missing the required 'version' field",
        )
    if version != MV_VERSION:
        raise ConversionError.for_unsupported_version(
            f"Unsupported Metric View version '{version}'. "
            f"This converter targets v{MV_VERSION} only.",
            version,  # type: ignore[arg-type]
        )
    model = _convert_view(view, model_name, notices)
    out: dict[str, Any] = {"version": OSSIE_VERSION}
    out.update(model)
    return serialize_ossie(out, notices)


def serialize_ossie(out: dict[str, Any], notices: Notices) -> Result:
    try:
        text = write_yaml(out)
    except Exception as e:  # noqa: BLE001
        raise ConversionError.internal_error(
            f"failed to serialize Apache Ossie YAML: {e}"
        ) from e
    return Result(text, notices.to_list())


def _convert_view(
    view: dict[Any, Any], model_name: str | None, notices: Notices
) -> dict[str, Any]:
    source = view.get("source")
    if source is None or js(source) == "":
        raise ConversionError("Metric View is missing required 'source'")
    # Same subquery test validate_source applies below, so the two cannot drift.
    is_sql = SELECT_WITH_RE.match(js(source).strip()) is not None
    last_id = last_identifier(source)
    if model_name:
        fact_name = model_name
    elif not is_sql and last_id is not None and is_simple_identifier(last_id):
        fact_name = last_id
    else:
        fact_name = "metric_view"
    validate_source(source, fact_name)

    datasets: list[dict[str, Any]] = []
    fact_ds: dict[str, Any] = {"name": fact_name, "source": source}
    datasets.append(fact_ds)
    relationships: list[dict[str, Any]] = []
    alias_to_dataset: dict[str, str] = {"source": fact_name, fact_name: fact_name}
    seen_names: set[str] = {fact_name.strip().lower()}

    _walk(
        fact_name, "source", as_list(view.get("joins")), datasets, relationships,
        alias_to_dataset, seen_names,
    )  # fmt: skip

    # The reverse conversion bounds how many datasets it will build (see build_join_tree), so a
    # Metric View that expands past that bound could not be imported again. Reject it here to keep
    # MV -> Ossie -> MV symmetric rather than emitting a model that fails on the way back in.
    if len(datasets) > MAX_JOIN_NODES:
        raise ConversionError(
            f"Metric View '{fact_name}' has {len(datasets)} datasets; "
            f"at most {MAX_JOIN_NODES} are supported."
        )

    # `fields` is a v1.1 alias for `dimensions`: an empty `dimensions: []` falls through to
    # `fields`, and the "both set" warning fires only when BOTH are non-empty.
    dim_list = as_list(view.get("dimensions"))
    field_list = as_list(view.get("fields"))
    if dim_list and field_list:
        notices.warn(
            "view",
            "both 'dimensions' and 'fields' are set; 'fields' is a v1.1 alias "
            "for 'dimensions', so the 'fields' list is ignored",
        )
    fields_by_dataset: dict[str, list[Any]] = {d["name"]: [] for d in datasets}
    dims = dim_list if dim_list else field_list
    dataset_names = {d["name"].lower() for d in datasets}
    scope = _DialectScope(dataset_names, _view_defined_names(view, dims), _has_hidden_names(dims))
    for dim_obj in dims:
        dim = as_map(dim_obj)
        if _is_wildcard(dim):
            notices.warn(
                "dimension",
                f"wildcard column '{js(jstr(dim.get('expr')))}' has no Apache Ossie field "
                "representation; skipped",
            )
            continue
        ds_name, field = _convert_dimension(dim, alias_to_dataset, fact_name, scope)
        fields_by_dataset[ds_name].append(field)
    for d in datasets:
        flds = fields_by_dataset[d["name"]]
        if flds:
            d["fields"] = flds

    metrics: list[Any] = []
    # Fixed once the join tree is walked, so compile it once for all measures.
    alias_head = qualifier_chain_pattern(alias_to_dataset.keys())
    # A view filter changes every measure's value, and it rides only in the DATABRICKS stash.
    filtered = "filter" in view
    for m_obj in as_list(view.get("measures")):
        m = as_map(m_obj)
        if _is_wildcard(m):
            notices.warn(
                "measure",
                f"wildcard measure '{js(jstr(m.get('expr')))}' has no Apache Ossie metric "
                "representation; skipped",
            )
            continue
        metrics.append(_convert_measure(m, alias_head, alias_to_dataset, scope, filtered))

    model: dict[str, Any] = {"name": fact_name}
    if truthy(view.get("comment")):
        model["description"] = view.get("comment")
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics

    model_stash: dict[str, Any] = {k: view[k] for k in MODEL_STASH_KEYS if k in view}
    if _has_otm(as_list(view.get("joins"))):
        model_stash[STASH_SOURCE_KEY] = fact_name
    write_stash(model, model_stash)
    return model


def _walk(
    parent_name: str,
    parent_alias: str,
    joins: list[Any],
    datasets: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    alias_to_dataset: dict[str, str],
    seen_names: set[str],
) -> None:
    for join_obj in joins:
        join = as_map(join_obj)
        child = require_str(join, "name", "join")
        if _equals_ignore_case(child.strip(), "source"):
            raise ConversionError(
                "Join name 'source' is reserved for the fact source; rename the join."
            )
        key = child.strip().lower()
        if key in seen_names:
            raise ConversionError(
                f"Duplicate dataset/join name '{child}'; Metric View join names and the source "
                "must be distinct (case-insensitively)."
            )
        seen_names.add(key)
        child_ds: dict[str, Any] = {"name": child}
        # Validated with the same rule the export applies to a dataset source, so a Metric View
        # that imports cleanly is always exportable again.
        child_source = require_str(join, "source", f"join '{child}'")
        validate_source(child_source, child)
        child_ds["source"] = child_source
        datasets.append(child_ds)
        alias_to_dataset[child] = child
        rel = _convert_join(join, parent_name, parent_alias, child)
        relationships.append(rel)
        # rely.at_most_one_match on a many_to_one join -> recover a unique_key on the child.
        rely = as_map(join.get("rely"))
        to_cols = str_list(rel.get("to_columns"))
        if child == jstr(rel.get("to")) and to_cols and rely.get("at_most_one_match") is True:
            child_ds["unique_keys"] = [list(to_cols)]
        _walk(
            child, child, as_list(join.get("joins")), datasets, relationships,
            alias_to_dataset, seen_names,
        )  # fmt: skip


def _has_otm(joins: list[Any]) -> bool:
    for j_obj in joins:
        j = as_map(j_obj)
        if _equals_ignore_case(CARD_ONE_TO_MANY, jstr(j.get("cardinality"))):
            return True
        if _has_otm(as_list(j.get("joins"))):
            return True
    return False


def _convert_join(
    join: dict[Any, Any], parent_name: str, parent_alias: str, child: str
) -> dict[str, Any]:
    has_using = join.get("using") is not None and bool(as_list(join.get("using")))
    on_value = jstr(join.get("on"))
    has_on = on_value is not None and on_value != ""
    if not has_using and not has_on:
        raise ConversionError(
            f"Join '{child}' has no join condition (empty or absent 'on'/'using'); "
            "condition-less (cross) joins have no Apache Ossie relationship representation."
        )
    parent_cols, child_cols, raw_on = _decompose_on(join, parent_alias, parent_name, child)
    if raw_on is not None:
        # The `on` is not an equi-join of simple `alias.column` pairs (it has a non-equi operator,
        # a SQL-function-wrapped key, or an extra filter predicate), so an Apache Ossie
        # relationship cannot represent it. Reject rather than emit an invalid relationship.
        raise ConversionError(
            f"Join '{child}' uses a non-equi or unsupported join condition ('on: {raw_on}') "
            "that an Apache Ossie relationship cannot represent. Apache Ossie joins are "
            "equi-joins of simple `alias.column` pairs (the fact side may be qualified with "
            "`source`, the source table name, or left bare). Cannot import."
        )
    assert parent_cols is not None and child_cols is not None
    if not has_on and has_using and not parent_cols:
        # Only fall back to `using` when there is no `on` to decompose (`on` wins).
        using = str_list(join.get("using"))
        parent_cols = list(using)
        child_cols = list(using)

    cardinality = jstr(join.get("cardinality"))
    if cardinality is None:
        cardinality = CARD_MANY_TO_ONE
    rel: dict[str, Any] = {}
    child_is_from = cardinality.lower() == CARD_ONE_TO_MANY
    if child_is_from:
        rel["name"] = f"{child}_to_{parent_name}"
        rel["from"] = child
        rel["to"] = parent_name
    else:
        rel["name"] = f"{parent_name}_to_{child}"
        rel["from"] = parent_name
        rel["to"] = child
    rel["from_columns"] = child_cols if child_is_from else parent_cols
    rel["to_columns"] = parent_cols if child_is_from else child_cols
    stash: dict[str, Any] = {k: join[k] for k in JOIN_STASH_KEYS if k in join}
    # Preserve the original `on` verbatim only when rebuilding it from the from/to columns would
    # not reproduce it. build_join restores a stashed `on` in preference to rebuilding.
    if has_on and on_value != _rebuilt_on(parent_alias, child, parent_cols, child_cols):
        stash["on"] = on_value
    write_stash(rel, stash)
    return rel


def _rebuilt_on(
    parent_alias: str, child: str, parent_cols: list[str], child_cols: list[str]
) -> str | None:
    """The `on` clause the export would rebuild, or None when it would instead emit `using`."""
    if parent_cols == child_cols:
        return None
    return " AND ".join(
        f"{parent_alias}.{p} = {child}.{c}" for p, c in zip(parent_cols, child_cols, strict=False)
    )


def _decompose_on(
    join: dict[Any, Any], parent_alias: str, parent_name: str, child_alias: str
) -> tuple[list[str] | None, list[str] | None, str | None]:
    """Returns (parent_cols, child_cols, raw_on); raw_on non-None means reject.

    A join may carry both `on` and `using`. `on` takes precedence, matching how the engine resolves
    the join criteria; falling back to `using` when `on` is present would silently join on
    different columns than the view does.
    """
    on = jstr(join.get("on"))
    if not on:
        # No usable `on`; the caller derives the columns from `using`.
        return [], [], None
    parent_aliases = {parent_alias, parent_name}
    allow_bare = parent_alias == "source"
    from_cols: list[str] = []
    to_cols: list[str] = []
    for clause in java_split(AND_SPLIT_RE, on):
        if NON_EQUI_RE.search(clause):
            return None, None, on
        m = EQ_CLAUSE_RE.fullmatch(clause)
        if m is None:
            return None, None, on
        la, lc = _split_alias(m.group(1))
        ra, rc = _split_alias(m.group(2))
        if not (is_simple_identifier(lc) and is_simple_identifier(rc)):
            return None, None, on
        l_parent = la in parent_aliases or (la is None and allow_bare)
        r_parent = ra in parent_aliases or (ra is None and allow_bare)
        if child_alias == la and r_parent:
            from_cols.append(rc)
            to_cols.append(lc)
        elif child_alias == ra and l_parent:
            from_cols.append(lc)
            to_cols.append(rc)
        else:
            return None, None, on
    return from_cols, to_cols, None


def _split_alias(operand: str) -> tuple[str | None, str]:
    """`customer.c_custkey` -> ("customer", "c_custkey"); `x` -> (None, "x")."""
    operand = operand.strip()
    dot = operand.find(".")
    if dot >= 0:
        return operand[:dot].strip(), operand[dot + 1 :].strip()
    return None, operand


def _convert_dimension(
    dim: dict[Any, Any],
    alias_to_dataset: dict[str, str],
    fact_name: str,
    scope: _DialectScope,
) -> tuple[str, dict[str, Any]]:
    name = require_str(dim, "name", "dimension")
    expr = require_str(dim, "expr", f"dimension '{name}'")
    ds_name, ossie_expr = _resolve_column(expr, alias_to_dataset, fact_name)

    field: dict[str, Any] = {
        "name": name,
        "expression": _dialect_expr(ossie_expr, scope.dialect_of(ossie_expr, name, False)),
    }
    if truthy(dim.get("comment")):
        field["description"] = dim.get("comment")
    if truthy(dim.get("display_name")):
        field["label"] = dim.get("display_name")
    if truthy(dim.get("synonyms")):
        field["ai_context"] = {"synonyms": list(as_list(dim.get("synonyms")))}
    stash = {k: dim[k] for k in COLUMN_STASH_KEYS if k in dim}
    write_stash(field, stash)
    return ds_name, field


def _resolve_column(
    expr: str, alias_to_dataset: dict[str, str], fact_name: str
) -> tuple[str, str]:
    """Map a dimension expression to (dataset_name, de-aliased_expression)."""
    # Keeping trailing empty segments files a malformed `customer.` under `customer` rather than
    # under the fact.
    segments = [s.strip() for s in expr.split(".")]
    ds = None
    i = 0
    while i < len(segments) - 1 and segments[i] in alias_to_dataset:
        ds = alias_to_dataset[segments[i]]
        i += 1
    if ds is None:
        return fact_name, expr
    rest = ".".join(segments[i:])
    return (ds, rest) if is_simple_identifier(rest) else (ds, expr)


def _convert_measure(
    measure: dict[Any, Any],
    alias_head: re.Pattern[str] | None,
    alias_to_dataset: dict[str, str],
    scope: _DialectScope,
    filtered: bool,
) -> dict[str, Any]:
    """Converts a measure to an Apache Ossie metric, de-aliasing the expression's qualifiers.

    A Metric View addresses a joined column by its full join path, an Apache Ossie metric by the
    dataset name, so ``parent.child.col`` becomes ``child.col`` and ``source.col`` becomes
    ``<fact>.col``. This is the inverse of the export's ``qualify_measure``.
    """
    name = require_str(measure, "name", "measure")
    raw_expr = require_str(measure, "expr", f"measure '{name}'")
    expr = rewrite_qualifiers(raw_expr, alias_head, alias_to_dataset.get)
    metric: dict[str, Any] = {"name": name}
    # A window, partition, or view filter changes the measure's value and rides only in the
    # DATABRICKS stash; a portable label would let another tool compute a plain aggregate instead.
    portable = not filtered and "window" not in measure and "partition" not in measure
    expression = _dialect_expr(
        expr, scope.dialect_of(expr, name, True) if portable else DIALECT_DATABRICKS
    )
    if portable:
        keyed_sum = joined_integer_sum.decode(raw_expr)
        if keyed_sum.keys:
            decoded = rewrite_qualifiers(keyed_sum.expression, alias_head, alias_to_dataset.get)
            dialect = scope.dialect_of(decoded, name, True, keyed_sum.physical_columns)
            keys: dict[str, str] = {}
            grain: dict[str, None] = {}
            for value, key in keyed_sum.keys.items():
                alias = last_identifier(joined_integer_sum.qualifier(value))
                dataset = alias_to_dataset.get(alias)  # type: ignore[arg-type]
                if dataset is not None and alias != "source":
                    grain[dataset] = None
                    keys[f"{dataset}.{last_identifier(value)}"] = (
                        f"{dataset}.{last_identifier(key)}"
                    )
            if dialect != DIALECT_DATABRICKS and len(keys) == len(keyed_sum.keys):
                expression["dialects"].append({"dialect": dialect, "expression": decoded})
                write_stash(
                    metric,
                    {"integer_sum_grain": list(grain), "integer_sum_keys": keys},
                    "CUBE",
                )
    metric["expression"] = expression
    if truthy(measure.get("comment")):
        metric["description"] = measure.get("comment")
    if truthy(measure.get("synonyms")):
        metric["ai_context"] = {"synonyms": list(as_list(measure.get("synonyms")))}
    stash = {k: measure[k] for k in COLUMN_STASH_KEYS if k in measure}
    stash.update({k: measure[k] for k in MEASURE_STASH_KEYS if k in measure})
    write_stash(metric, stash)
    return metric


def _dialect_expr(expr: str, label: str) -> dict[str, Any]:
    """An Apache Ossie expression with the single dialect entry ``label: expr``."""
    return {"dialects": [{"dialect": label, "expression": expr}]}


def _is_wildcard(col: dict[Any, Any]) -> bool:
    return "name" not in col


def _view_defined_names(view: dict[Any, Any], dims: list[Any]) -> set[str]:
    """Lower-cased bare names whose meaning comes from the view rather than a column.

    Parameters, and dimensions other than a plain same-named column (a later expression naming one
    may resolve to the dimension instead of the column).
    """
    names: set[str] = set()
    for p in as_list(view.get("parameters")):
        name = jstr(as_map(p).get("name"))
        if name is not None:
            names.add(name.lower())
    for d in dims:
        dim = as_map(d)
        name = jstr(dim.get("name"))
        expr = jstr(dim.get("expr"))
        if name is not None and (
            expr is None or not _equals_ignore_case(expr.strip(), name.strip())
        ):
            names.add(name.lower())
    return names


def _has_hidden_names(dims: list[Any]) -> bool:
    """Whether a dimension has no name and is not a fact wildcard.

    A joined wildcard such as ``customer.*``, or a derived name, adds dimensions this import cannot
    see, and any of them may shadow a column of the same name.
    """
    for d in dims:
        dim = as_map(d)
        expr = jstr(dim.get("expr"))
        if _is_wildcard(dim) and (expr is None or FACT_WILDCARD_RE.fullmatch(expr) is None):
            return True
    return False


class _DialectScope:
    """What an expression may name besides a column, for labeling its dialect.

    With ``hidden_names``, any bare name may be a dimension, so nothing is portable.
    """

    def __init__(self, datasets: set[str], view_names: set[str], hidden_names: bool) -> None:
        self.datasets = datasets
        self.view_names = view_names
        self.hidden_names = hidden_names

    def dialect_of(
        self,
        expr: str,
        self_name: str,
        measure: bool,
        physical_columns: AbstractSet[str] = frozenset(),
    ) -> str:
        """The most portable dialect for the expression of the column ``self_name``."""
        if self.hidden_names:
            return DIALECT_DATABRICKS
        # A column naming itself refers to the source column, not to itself.
        opaque = set(self.view_names)
        opaque.discard(self_name.lower())
        opaque -= set(physical_columns)
        return expression_dialect.classify(expr, self.datasets, opaque, measure)
