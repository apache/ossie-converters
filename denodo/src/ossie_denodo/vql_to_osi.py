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
Denodo VQL → OSI YAML converter.

Transforms a VQL string containing CREATE OR REPLACE ASSOCIATION and
CREATE OR REPLACE METRIC VIEW statements into a flat Ossie semantic model
(version 0.2.0.dev0) YAML string.
"""

from __future__ import annotations

import json
import re

import yaml

from ossie_denodo.models import (
    AssociationModel,
    DimensionFieldModel,
    MetricViewModel,
    SourceModel,
)
from ossie_denodo.parser import parse_vql

SUPPORTED_VERSION = "0.2.0.dev0"


class VqlConversionError(Exception):
    """Raised when a Denodo VQL string cannot be converted to Ossie YAML."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_vql_to_ossie(vql_str: str) -> str:
    """Convert a Denodo VQL string to an Ossie YAML string.

    The input must contain at least one ``CREATE OR REPLACE METRIC VIEW``
    statement. ``CREATE OR REPLACE ASSOCIATION`` statements are optional but
    recommended to reconstruct relationships.

    Args:
        vql_str: Denodo VQL string.

    Returns:
        Ossie YAML document as a string (version 0.2.0.dev0).

    Raises:
        VqlConversionError: If the input is invalid or cannot be converted.
    """
    try:
        associations, metric_view = parse_vql(vql_str)
    except Exception as exc:
        raise VqlConversionError(f"Failed to parse VQL: {exc}") from exc

    if metric_view is None:
        raise VqlConversionError(
            "No CREATE METRIC VIEW statement found in the VQL input."
        )

    ossie_dict = _build_ossie_dict(associations, metric_view)
    return yaml.dump(ossie_dict, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Internal model construction
# ---------------------------------------------------------------------------

def _build_ossie_dict(
    associations: list[AssociationModel],
    mv: MetricViewModel,
) -> dict:
    """Build the nested Ossie dict from parsed VQL internal models."""
    assoc_by_name = {a.name: a for a in associations}

    datasets = _build_datasets(mv)
    relationships = _build_relationships(mv, assoc_by_name)
    metrics = _build_metrics(mv)

    model: dict = {
        "name": mv.name,
    }

    if mv.description:
        model["description"] = mv.description

    model["datasets"] = datasets
    model["relationships"] = relationships
    model["metrics"] = metrics

    ossie: dict = {
        "version": SUPPORTED_VERSION,
    }
    ossie.update(model)

    # Custom Extension for Denodo
    custom_data = {}
    if mv.folder:
        custom_data["folder"] = mv.folder
    if mv.context:
        custom_data["context"] = mv.context
    if mv.tags:
        custom_data["tags"] = mv.tags
    if mv.filter_condition:
        custom_data["filter_condition"] = mv.filter_condition.strip()

    if custom_data:
        ossie["custom_extensions"] = [{
            "vendor_name": "DENODO",
            "data": json.dumps(custom_data)
        }]

    return ossie


def _build_datasets(
    mv: MetricViewModel,
) -> list[dict]:
    """Reconstruct Ossie datasets from SOURCES and DIMENSIONS."""
    # Build a map: alias → fields list
    fields_by_alias: dict[str, list[DimensionFieldModel]] = {
        src.alias: [] for src in mv.sources
    }
    # Map: (alias, field_name) → dimension_group_name
    field_to_dim: dict[tuple[str, str], str] = {}

    for group in mv.dimensions:
        for f in group.fields:
            alias = _infer_alias_from_single_field(f, mv.sources)

            fields_by_alias[alias].append(f)
            if group.group_name:
                field_to_dim[(alias, f.name)] = group.group_name

    datasets = []
    for src in mv.sources:
        alias = src.alias
        source_str = f"{src.database}.{src.view}" if src.database else src.view

        ds: dict = {
            "name": alias,
            "source": source_str,
        }

        dim_fields = fields_by_alias.get(alias, [])
        if dim_fields:
            ds["fields"] = [_dim_field_to_ossie(f, alias) for f in dim_fields]

        # Add custom_extension for fields_dimension if needed
        ds_field_dim = {
            f.name: field_to_dim[(alias, f.name)]
            for f in dim_fields
            if (alias, f.name) in field_to_dim
        }
        if ds_field_dim:
            ds["custom_extensions"] = [{
                "vendor_name": "DENODO",
                "data": json.dumps({"fields_dimension": ds_field_dim})
            }]

        datasets.append(ds)

    return datasets


def _infer_alias_from_single_field(
    field: DimensionFieldModel,
    sources: list[SourceModel],
) -> str:
    """Infer source alias for a single field."""
    source_aliases = {s.alias for s in sources}
    # Find all identifiers followed by a dot.
    candidates = re.findall(r'("(?:[^"]|""|\\")*"|\w+)\.', field.expression)
    for candidate in candidates:
        if candidate in source_aliases:
            return candidate
    # Fallback: return first source alias
    return sources[0].alias if sources else ""




def _dim_field_to_ossie(field: DimensionFieldModel, alias: str) -> dict:
    """Convert a DimensionFieldModel to an Ossie field dict."""
    field_name = field.name

    # Determine the expression string:
    # - "alias.field_name" → simple field reference → expression = field_name
    # - anything else → complex expression → preserve as-is
    simple_ref = f"{alias}.{field_name}"
    if field.expression == simple_ref:
        expr_str = field_name
    else:
        expr_str = field.expression

    return {
        "name": field_name,
        "expression": {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": expr_str}
            ]
        },
    }


def _build_relationships(
    mv: MetricViewModel,
    assoc_by_name: dict[str, AssociationModel],
) -> list[dict]:
    """Reconstruct Ossie relationships from ASSOCIATIONS clause + AssociationModels."""
    relationships = []

    for ref in mv.associations:
        assoc_name = ref.association_name
        assoc_list = assoc_name.split('.')
        vdb_name = None
        if len(assoc_list)>1:
            assoc_name = assoc_list[1]
            vdb_name = assoc_list[0]

        from_alias = ref.from_alias
        to_alias = ref.to_alias

        # Determine which alias is "many" (from) and which is "one" (to)
        # using cardinality/PRINCIPAL info from the AssociationModel.
        assoc = assoc_by_name.get(assoc_name)
        if assoc:
            from_alias, to_alias = _resolve_from_to(assoc, from_alias, to_alias, mv.sources)

        rel: dict = {
            "name": assoc_name if vdb_name is None else vdb_name+"."+assoc_name,
            "from": from_alias,
            "to": to_alias,
        }

        # Reconstruct column mappings from the AssociationModel if available
        if assoc and assoc.mappings:
            from_cols, to_cols = _extract_columns_and_extensions(
                assoc.mappings, from_alias, assoc, mv.sources
            )
            rel["from_columns"] = from_cols
            rel["to_columns"] = to_cols
        else:
            rel["from_columns"] = []
            rel["to_columns"] = []

        # Generate DENODO custom_extension for non-default properties
        ext = _build_mapping_custom_extension(
            assoc.mappings if assoc and assoc.mappings else [],
            from_alias, assoc, mv.sources,
            join_type=ref.join_type
        )
        if ext:
            rel["custom_extensions"] = [ext]

        relationships.append(rel)

    return relationships


def _is_one_side(cardinality: str) -> bool:
    """Return True if the cardinality string represents the "one" side.

    In Denodo, the cardinality on an ENDPOINT indicates how many of *that*
    view exist relative to the other endpoint.  An endpoint with (0,*) or
    (1,*) is the "one" side (it has many of the other view), while an
    endpoint with (1), (0,1) or (1,1) is the "many" (FK) side.
    """
    # Normalise: remove spaces
    card = cardinality.replace(" ", "")
    return card in ("0,*", "1,*", "+")


def _resolve_from_to(
    assoc: "AssociationModel",
    from_alias: str,
    to_alias: str,
    sources: "list[SourceModel] | None" = None,
) -> tuple[str, str]:
    """Return (many_alias, one_alias) based on endpoint cardinality/PRINCIPAL.

    Rules (in priority order):
    1. If any endpoint has PRINCIPAL and its cardinality is a "one" side
       ((0,1) or (1,1)), that endpoint is "one"; the other is "many".
    2. If no PRINCIPAL but one endpoint has a "one" cardinality, that is "one".
    3. Otherwise keep the original order (from_alias, to_alias).

    The OSI model convention is: ``from`` = many side, ``to`` = one side.
    """
    endpoints = assoc.endpoints
    if not endpoints:
        return from_alias, to_alias

    # Build a lookup: role_name → endpoint
    ep_by_role = {ep.role_name: ep for ep in endpoints}

    # Try to match aliases to endpoints by role_name
    from_ep = ep_by_role.get(from_alias)
    to_ep = ep_by_role.get(to_alias)

    # Fallback: match by view_name
    if from_ep is None or to_ep is None:
        ep_by_view = {ep.view_name: ep for ep in endpoints}
        if from_ep is None:
            from_ep = ep_by_view.get(from_alias)
        if to_ep is None:
            to_ep = ep_by_view.get(to_alias)

    # Fallback: resolve alias → view_name via sources, then match by role_name or view_name
    if (from_ep is None or to_ep is None) and sources:
        ep_by_view = {ep.view_name: ep for ep in endpoints}
        alias_to_view = {s.alias: s.view for s in sources}
        if from_ep is None:
            from_view = alias_to_view.get(from_alias)
            if from_view:
                from_ep = ep_by_role.get(from_view) or ep_by_view.get(from_view)
        if to_ep is None:
            to_view = alias_to_view.get(to_alias)
            if to_view:
                to_ep = ep_by_role.get(to_view) or ep_by_view.get(to_view)

    if from_ep is None or to_ep is None:
        return from_alias, to_alias

    # Rule 1: use PRINCIPAL to determine the many (FK) side.
    # The PRINCIPAL endpoint is always the many (FK) side; OSI: from = many, to = one.
    principal_eps = [ep for ep in [from_ep, to_ep] if ep.is_principal]
    if principal_eps:
        principal_ep = principal_eps[0]
        # principal is the many (FK) side → must be "from"
        if principal_ep is from_ep:
            return from_alias, to_alias
        else:
            return to_alias, from_alias

    # Rule 2: No PRINCIPAL — use cardinality alone.
    # An endpoint with (0,*) or (1,*) is the "one" side.
    from_is_one = _is_one_side(from_ep.cardinality)
    to_is_one = _is_one_side(to_ep.cardinality)

    if to_is_one and not from_is_one:
        return from_alias, to_alias
    if from_is_one and not to_is_one:
        return to_alias, from_alias

    return from_alias, to_alias


def _build_metrics(mv: MetricViewModel) -> list[dict]:
    """Reconstruct Ossie metrics from the METRICS clause."""
    metrics = []
    for m in mv.metrics:
        metric_name = m.name
        metrics.append({
            "name": metric_name,
            "expression": {
                "dialects": [
                    {"dialect": "ANSI_SQL", "expression": m.expression}
                ]
            },
        })
    return metrics


# ---------------------------------------------------------------------------
# Mapping helpers (function detection for custom_extensions)
# ---------------------------------------------------------------------------

def _is_function_expr(expr: str) -> bool:
    """Return True if *expr* contains a function call (i.e. has parentheses)."""
    return bool(re.search(r'\w+\s*\(', expr))


def _bare_column(expr: str) -> str:
    """Extract the bare column name from a mapping side.

    For plain columns like ``customer_key`` returns ``customer_key``.
    For function expressions like ``round(customer_key)`` returns ``customer_key``
    (the first identifier argument inside the outermost function call).
    """
    m = re.search(r'\w+\s*\(([^)]+)\)', expr)
    if m:
        # Take the first comma-separated argument and strip whitespace
        return m.group(1).split(',')[0].strip()
    return expr.strip()


def _extract_columns_and_extensions(
    mappings: list[tuple[str, str]],
    from_alias: str,
    assoc: "AssociationModel",
    sources: "list[SourceModel] | None" = None,
) -> tuple[list[str], list[str]]:
    """Return (from_cols, to_cols) using bare column names (stripping functions)."""
    left_is_from = _mapping_left_is_from(assoc, from_alias, sources)

    from_cols = []
    to_cols = []
    for left, right in mappings:
        if left_is_from:
            from_cols.append(_bare_column(left))
            to_cols.append(_bare_column(right))
        else:
            from_cols.append(_bare_column(right))
            to_cols.append(_bare_column(left))
    return from_cols, to_cols


def _mapping_left_is_from(
    assoc: "AssociationModel",
    from_alias: str,
    sources: "list[SourceModel] | None" = None,
) -> bool:
    """Return True if the left side of ADD MAPPING corresponds to the from_alias endpoint.

    The left side of ADD MAPPING corresponds to the first ENDPOINT (endpoints[0]).
    We check role_name and view_name directly, and also resolve via sources alias→view.
    """
    if not assoc.endpoints:
        return True
    first_ep = assoc.endpoints[0]
    if first_ep.role_name == from_alias:
        return True
    # Resolve alias → view_name via sources, then match by role_name
    if sources:
        alias_to_view = {s.alias: s.view for s in sources}
        from_view = alias_to_view.get(from_alias)
        if from_view and first_ep.role_name == from_view:
            return True
    return False


def _get_from_to_endpoints(
    assoc: "AssociationModel",
    from_alias: str,
    to_alias: str,
    sources: "list[SourceModel] | None" = None,
) -> tuple["object | None", "object | None"]:
    """Return (from_endpoint, to_endpoint) matched by alias/view_name."""
    if not assoc or not assoc.endpoints:
        return None, None
    ep_by_role = {ep.role_name: ep for ep in assoc.endpoints}
    ep_by_view = {ep.view_name: ep for ep in assoc.endpoints}
    alias_to_view: dict[str, str] = {}
    if sources:
        alias_to_view = {s.alias: s.view for s in sources}

    def _find_ep(alias: str):
        ep = ep_by_role.get(alias) or ep_by_view.get(alias)
        if ep is None:
            view = alias_to_view.get(alias)
            if view:
                ep = ep_by_role.get(view) or ep_by_view.get(view)
        return ep

    return _find_ep(from_alias), _find_ep(to_alias)


def _build_mapping_custom_extension(
    mappings: list[tuple[str, str]],
    from_alias: str,
    assoc: "AssociationModel | None",
    sources: "list[SourceModel] | None" = None,
    join_type: str | None = None,
) -> dict | None:
    """Build a DENODO custom_extension dict capturing non-default association properties.

    Captures:
    - ``isReferentialConstraint``: only when False (default is True).
    - ``join_type``: join type if present (LEFT, RIGHT, INNER).
    - ``folder``: folder if present.
    - ``from_columns.fields_replace``: mapping col→expr for function expressions on the from side.
    - ``to_columns.fields_replace``: mapping col→expr for function expressions on the to side.
    - ``from_columns.relation``: cardinality of the non-principal (many/FK) endpoint when not the default (0,*).
    - ``to_columns.relation``: cardinality of the PRINCIPAL (one/PK) endpoint when not the default (1).

    Returns None if nothing non-default is found.
    """
    data: dict = {}

    # isReferentialConstraint: only emit when False
    if assoc is not None and not assoc.referential_constraint:
        data["isReferentialConstraint"] = False

    # join_type: emit if present
    if join_type:
        data["join_type"] = join_type

    # folder: emit if present
    if assoc is not None and assoc.folder:
        data["folder"] = assoc.folder

    # Resolve from/to endpoints by is_principal flag:
    # - from side = non-principal endpoint (many/FK) → default cardinality (0,*)
    # - to side = PRINCIPAL endpoint (one/PK) → default cardinality (1)
    # These match the OSI→VQL defaults so round-trips omit them when unchanged.
    _DEFAULT_FROM_CARD = "0,*"
    _DEFAULT_TO_CARD = "1"

    if assoc and assoc.endpoints:
        principal_ep = next((ep for ep in assoc.endpoints if ep.is_principal), None)
        non_principal_ep = next((ep for ep in assoc.endpoints if not ep.is_principal), None)
        to_card = principal_ep.cardinality.replace(" ", "") if principal_ep else _DEFAULT_TO_CARD
        from_card = non_principal_ep.cardinality.replace(" ", "") if non_principal_ep else _DEFAULT_FROM_CARD
    else:
        from_card = _DEFAULT_FROM_CARD
        to_card = _DEFAULT_TO_CARD

    # Function expressions in mappings
    from_funcs: dict[str, str] = {}
    to_funcs: dict[str, str] = {}

    if mappings:
        left_is_from = _mapping_left_is_from(assoc, from_alias, sources) if assoc else True
        for left, right in mappings:
            if left_is_from:
                from_expr, to_expr = left, right
            else:
                from_expr, to_expr = right, left
            if _is_function_expr(from_expr):
                col = _bare_column(from_expr)
                from_funcs[col] = from_expr.strip()
            if _is_function_expr(to_expr):
                col = _bare_column(to_expr)
                to_funcs[col] = to_expr.strip()

    # Build from_columns section if anything non-default
    from_section: dict = {}
    if from_card != _DEFAULT_FROM_CARD:
        from_section["relation"] = from_card
    if from_funcs:
        from_section["fields_replace"] = [{k: v} for k, v in from_funcs.items()]
    if from_section:
        data["from_columns"] = from_section

    # Build to_columns section if anything non-default
    to_section: dict = {}
    if to_card != _DEFAULT_TO_CARD:
        to_section["relation"] = to_card
    if to_funcs:
        to_section["fields_replace"] = [{k: v} for k, v in to_funcs.items()]
    if to_section:
        data["to_columns"] = to_section

    if not data:
        return None

    return {
        "vendor_name": "DENODO",
        "data": json.dumps(data),
    }
