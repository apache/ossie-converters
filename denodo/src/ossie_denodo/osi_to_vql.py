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
OSI YAML → Denodo VQL converter.

Transforms a flat Ossie semantic model (version 0.2.0.dev0 and 0.1.1) into a VQL string
containing CREATE OR REPLACE ASSOCIATION statements followed by a
CREATE OR REPLACE METRIC VIEW statement.
"""

from __future__ import annotations

import json
import warnings

import yaml

from ossie_denodo.models import (
    AssociationModel,
    AssociationRefModel,
    DimensionFieldModel,
    DimensionGroupModel,
    EndpointModel,
    MetricFieldModel,
    MetricViewModel,
    SourceModel,
)

SUPPORTED_VERSIONS = ["0.2.0.dev0","0.1.1"]


class OssieConversionError(Exception):
    """Raised when an Ossie YAML cannot be converted to Denodo VQL."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_ossie_to_vql(ossie_yaml_str: str) -> str:
    """Convert an Ossie YAML string to a Denodo VQL string.

    The output contains zero or more ``CREATE OR REPLACE ASSOCIATION``
    statements followed by one ``CREATE OR REPLACE METRIC VIEW`` statement.

    Args:
        ossie_yaml_str: Ossie YAML document as a string.

    Returns:
        Denodo VQL as a string.

    Raises:
        OssieConversionError: If the input is invalid or cannot be converted.
    """
    ossie = _load_and_validate(ossie_yaml_str)
    associations, metric_view = _build_internal_models(ossie)
    return _render_vql(associations, metric_view)


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------

def _load_and_validate(ossie_yaml_str: str) -> dict:
    """Parse and validate the Ossie YAML, returning the model dict."""
    root = yaml.safe_load(ossie_yaml_str)
    if not isinstance(root, dict):
        raise OssieConversionError("Invalid Ossie YAML: expected a mapping at the root")

    if _has_ai_context(root):
        warnings.warn(
            "Denodo currently does not support 'ai_context' and it will be ignored."
        )

    version_str = str(root.get("version", ""))
    if version_str not in SUPPORTED_VERSIONS:
        warnings.warn(
            f"Unsupported Ossie specification version '{version_str}'. It is recommended to update to a supported version."
            f"Supported versions: {SUPPORTED_VERSIONS}"
        )

    # Support both flat and semantic_model array formats
    if "semantic_model" in root:
        semantic_model = root["semantic_model"]
        if not isinstance(semantic_model, list) or len(semantic_model) == 0:
            raise OssieConversionError(
                "Invalid Ossie YAML: 'semantic_model' must be a non-empty list"
            )
        if len(semantic_model) > 1:
            warnings.warn(
                f"Ossie YAML contains {len(semantic_model)} semantic models; "
                f"only the first will be converted"
            )
        ossie = semantic_model[0]
        if not isinstance(ossie, dict):
            raise OssieConversionError(
                "Invalid Ossie YAML: 'semantic_model' entries must be mappings"
            )
        return ossie

    # Flat format: model properties live at the root
    return root


# ---------------------------------------------------------------------------
# Internal model construction
# ---------------------------------------------------------------------------

def _build_internal_models(
    ossie: dict,
) -> tuple[list[AssociationModel], MetricViewModel]:
    """Build internal AssociationModel and MetricViewModel from an Ossie dict."""
    name = ossie.get("name")
    if not name:
        raise OssieConversionError("Missing required 'name' field in Ossie model")

    datasets: list[dict] = ossie.get("datasets") or []
    relationships: list[dict] = ossie.get("relationships") or []
    metrics_raw: list[dict] = ossie.get("metrics") or []
    description = ossie.get("description")

    # Extract Denodo-specific vendor extensions
    denodo_ext = _extract_denodo_extension(ossie)
    folder = denodo_ext.get("folder") if denodo_ext else None
    context: dict[str, str] = denodo_ext.get("context", {}) if denodo_ext else {}
    tags_raw: list = denodo_ext.get("tags") or [] if denodo_ext else []
    filter_condition: str = denodo_ext.get("filter_condition") if denodo_ext else None

    # Build a lookup: dataset alias → source view name (last segment of source)
    alias_to_source: dict[str, str] = {}
    for ds in datasets:
        alias = ds.get("name") or ""
        source_str = ds.get("source") or ds.get("origin") or ""
        alias_to_source[alias] = source_str.split(".")[-1] if source_str else alias

    # Build a lookup: association_name → AssociationModel (for mapping resolution)
    assoc_by_name: dict[str, AssociationModel] = {}
    for rel in relationships:
        assoc = _relationship_to_association(rel, alias_to_source)
        if assoc:
            assoc_by_name[assoc.name] = assoc

    associations = list(assoc_by_name.values())

    # SOURCES
    sources = [_dataset_to_source(ds) for ds in datasets]

    # ASSOCIATIONS clause refs
    assoc_refs = [_relationship_to_assoc_ref(rel) for rel in relationships]

    # DIMENSIONS — group fields by dataset
    dimensions = _build_dimensions(datasets)

    # METRICS
    metric_fields = [_metric_to_field(m) for m in metrics_raw if m.get("name")]

    # Dataset-level metrics
    for ds in datasets:
        ds_alias = ds.get("name") or ""
        ds_metrics = ds.get("metrics") or []
        for m in ds_metrics:
            if m.get("name"):
                field = _metric_to_field(m)
                # If expression is simple and unqualified, prefix it with dataset alias
                if _is_simple_unprefixed(field.expression):
                    field.expression = f"{ds_alias}.{field.expression}"
                else:
                    # For complex expressions, prefix identifiers
                    field.expression = _prefix_identifiers_in_expr(field.expression, ds_alias)
                metric_fields.append(field)

    # TAGS — read from DENODO custom_extension, not from root OSI field
    tags = [str(t) for t in tags_raw] if tags_raw else []

    metric_view = MetricViewModel(
        name=name,
        folder=folder,
        filter_condition=filter_condition,
        description=description,
        tags=tags,
        sources=sources,
        associations=assoc_refs,
        dimensions=dimensions,
        metrics=metric_fields,
        context=context,
    )

    return associations, metric_view


def _has_ai_context(data: any) -> bool:
    """Recursively check if any level of the dict/list contains 'ai_context'."""
    if isinstance(data, dict):
        if "ai_context" in data:
            return True
        return any(_has_ai_context(v) for v in data.values())
    if isinstance(data, list):
        return any(_has_ai_context(item) for item in data)
    return False


def _extract_denodo_extension(ossie: dict) -> dict | None:
    """Return the parsed data dict from the DENODO custom_extension, or None."""
    for ext in ossie.get("custom_extensions") or []:
        if isinstance(ext, dict) and ext.get("vendor_name") == "DENODO":
            data = ext.get("data")
            if isinstance(data, str):
                try:
                    return json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    pass
            elif isinstance(data, dict):
                return data
    return None


def _dataset_to_source(dataset: dict) -> SourceModel:
    """Convert an Ossie dataset dict to a SourceModel.

    Denodo only supports <database>.<view> format. If the source contains a schema
    segment (e.g. database.schema.view), the schema is ignored and a warning is emitted.
    """
    alias = dataset.get("name") or ""
    source_str = dataset.get("source") or dataset.get("origin") or ""
    parts = source_str.split(".")
    if len(parts) >= 3:
        database = parts[0]
        view = parts[-1]
        schema = ".".join(parts[1:-1])
        warnings.warn(
            f"Source '{source_str}' contains schema segment '{schema}' which is not "
            f"supported by Denodo. It will be ignored; using '{database}.{view}' instead."
        )
    elif len(parts) == 2:
        database, view = parts
    else:
        database = None
        view = source_str
    return SourceModel(alias=alias, database=database, view=view)


def _relationship_to_association(rel: dict, alias_to_source: dict[str, str] | None = None) -> AssociationModel | None:
    """Convert an Ossie relationship dict to an AssociationModel."""
    name = rel.get("name")
    if not name:
        return None

    vdb = None
    vdbAndName = name.split(".")
    if len(vdbAndName) > 1:
        name = vdbAndName[1]
        vdb = vdbAndName[0]

    from_alias = rel.get("from") or ""
    to_alias = rel.get("to") or ""

    # Resolve real source view names from alias map (fallback to alias itself)
    _src = alias_to_source or {}
    from_view = _src.get(from_alias, from_alias)
    to_view = _src.get(to_alias, to_alias)
    from_columns: list[str] = rel.get("from_columns") or []
    to_columns: list[str] = rel.get("to_columns") or []

    # Read DENODO custom_extension for function mappings and non-default properties
    from_func_map, to_func_map = _extract_mapping_functions(rel)
    ext_data = _extract_denodo_ext_data(rel)

    # Apply functions: replace bare column names with function expressions
    from_columns_vql = [from_func_map.get(col, col) for col in from_columns]
    to_columns_vql = [to_func_map.get(col, col) for col in to_columns]

    # isReferentialConstraint: default True, only False when explicitly set
    referential_constraint = ext_data.get("isReferentialConstraint", True)

    #folder: Specific folder for the association vql
    folder = ext_data.get("folder", None)

    # Cardinalities: read from relation field in custom_extension, or use defaults.
    # In OSI custom_extension:
    #   from_columns.relation → cardinality of the PRINCIPAL (from/many/FK) endpoint → default (1)
    #   to_columns.relation   → cardinality of the non-principal (to/one/PK) endpoint → default (0,*)
    from_col_section = ext_data.get("from_columns") or {}
    to_col_section = ext_data.get("to_columns") or {}
    principal_cardinality = to_col_section.get("relation", "1") if isinstance(to_col_section, dict) else "1"
    non_principal_cardinality = from_col_section.get("relation", "0,*") if isinstance(from_col_section, dict) else "0,*"

    # Endpoints: from_alias is the "many" side, to_alias is the "one" side (PRINCIPAL).
    # In Denodo the ENDPOINT syntax is: ENDPOINT <role_name> <view_name>
    # where role_name is the name used by the *other* endpoint to refer to this one,
    # and view_name is the actual view this endpoint points to.
    # Default Denodo behaviour: role_name = other side's source view, view_name = own source view.
    endpoints = [
        EndpointModel(
            role_name=from_view,
            view_name=to_view,
            is_principal=True,
            cardinality=principal_cardinality,
        ),
        EndpointModel(
            role_name=to_view,
            view_name=from_view,
            is_principal=False,
            cardinality=non_principal_cardinality,
        ),
    ]

    mappings = list(zip(from_columns_vql, to_columns_vql))

    return AssociationModel(
        name=name,
        vdb=vdb,
        folder=folder,
        referential_constraint=referential_constraint,
        endpoints=endpoints,
        mappings=mappings,
    )


def _extract_mapping_functions(rel: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Extract function mappings from a relationship's DENODO custom_extension.

    Returns (from_func_map, to_func_map) where each is a dict mapping
    bare column name → function expression string.
    """
    for ext in rel.get("custom_extensions") or []:
        if isinstance(ext, dict) and ext.get("vendor_name") == "DENODO":
            data = ext.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
            if isinstance(data, dict):
                from_funcs = _extract_fields_replace(data.get("from_columns"))
                to_funcs = _extract_fields_replace(data.get("to_columns"))
                return from_funcs, to_funcs
    return {}, {}


def _extract_fields_replace(section) -> dict[str, str]:
    """Extract col→expr map from a columns section (new or legacy format)."""
    if not section:
        return {}
    # New format: {"fields_replace": [{col: expr}, ...], "relation": "..."}
    if isinstance(section, dict) and "fields_replace" in section:
        result = {}
        for item in section.get("fields_replace") or []:
            if isinstance(item, dict):
                result.update(item)
        return result
    # Legacy format: {col: expr}
    if isinstance(section, dict):
        return {k: v for k, v in section.items() if isinstance(v, str)}
    return {}


def _extract_denodo_ext_data(rel: dict) -> dict:
    """Return parsed DENODO custom_extension data dict, or empty dict."""
    for ext in rel.get("custom_extensions") or []:
        if isinstance(ext, dict) and ext.get("vendor_name") == "DENODO":
            data = ext.get("data")
            if isinstance(data, str):
                try:
                    return json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    pass
            if isinstance(data, dict):
                return data
    return {}


def _relationship_to_assoc_ref(rel: dict) -> AssociationRefModel:
    """Convert an Ossie relationship dict to an AssociationRefModel."""
    ext_data = _extract_denodo_ext_data(rel)
    join_type = None
    if ext_data:
        # join type
        join_type = ext_data.get("join_type", None)

    return AssociationRefModel(
            from_alias=rel.get("from") or "",
            to_alias=rel.get("to") or "",
            association_name=rel.get("name") or "",
            join_type=join_type,
        )


def _build_dimensions(datasets: list[dict]) -> list[DimensionGroupModel]:
    """Build DimensionGroupModel list from Ossie datasets."""
    groups: list[DimensionGroupModel] = []
    dim_fields_generic = []
    fields_by_dimension: dict[str, list[DimensionFieldModel]] = {}
    for ds in datasets:
        alias = ds.get("name") or ""
        source_view = alias
        fields_raw: list[dict] = ds.get("fields") or []
        if not fields_raw:
            continue

        denodo_ext = _extract_denodo_extension(ds)
        fields_dimension: dict[str, str] = (
            denodo_ext.get("fields_dimension") or {} if denodo_ext else {}
        )

        for f in fields_raw:
            field_name = f.get("name") or ""
            expr = _extract_expression(f.get("expression"))
            if expr is None:
                continue
            # If the expression is already prefixed (contains ".") or is a complex
            # expression (contains "(" or arithmetic operators outside parens),
            # preserve it as-is.
            # Otherwise, prefix with the source view name so that bare column
            # references like "n_name" become "psql_tpch_nation.n_name".
            if _is_simple_unprefixed(expr):
                # Bare column name → prefix with source view
                dim_expr = f"{source_view}.{expr}"
            elif "." in expr and not any(c in expr for c in ("(", "+", "-", "*", "/", "|")):
                # Already fully prefixed, no complex expression
                dim_expr = expr
            else:
                # Complex expression (functions, arithmetic, etc.) → tokenize and prefix
                dim_expr = _prefix_identifiers_in_expr(expr, source_view)

            dimension = fields_dimension.get(field_name)
            if dimension is not None:
                if fields_by_dimension.get(dimension) is None:
                    fields_by_dimension.setdefault(dimension, [])
                fields_by_dimension[dimension].append(DimensionFieldModel(name=field_name, expression=dim_expr))
            else:
                dim_fields_generic.append(DimensionFieldModel(name=field_name, expression=dim_expr))

    for group_name, dim_list in fields_by_dimension.items():
        groups.append(DimensionGroupModel(group_name=group_name, fields=dim_list))

    if len(dim_fields_generic) > 0:
        groups.append(DimensionGroupModel(group_name=None, fields=dim_fields_generic))

    return groups


def _metric_to_field(metric: dict) -> MetricFieldModel:
    """Convert an Ossie metric dict to a MetricFieldModel."""
    name = metric.get("name") or ""
    expr = _extract_expression(metric.get("expression")) or ""
    return MetricFieldModel(name=name, expression=expr)


def _is_simple_unprefixed(expr: str) -> bool:
    """Return True if *expr* is a bare column name with no view prefix or complexity.

    A simple unprefixed expression:
    - contains no "." (not already prefixed)
    - contains no "(" (not a function call)
    - contains no arithmetic operators (+, -, *, /) outside of identifiers
    - is not wrapped in parentheses
    """
    stripped = expr.strip()
    if stripped.startswith("(") or stripped.endswith(")"):
        return False
    if "." in stripped:
        return False
    if "(" in stripped:
        return False
    for op in ("+", "-", "*", "/", "||"):
        if op in stripped:
            return False
    return True


def _prefix_identifiers_in_expr(expr: str, source_view: str) -> str:
    """Tokenize *expr* and prefix bare column identifiers with *source_view*.

    Rules:
    - String literals ('...' or "...") → preserved as-is
    - Numeric literals → preserved as-is
    - Identifiers already prefixed (view.col, "#view".col) → preserved as-is
    - Identifiers followed by '(' (function names) → preserved as-is
    - Bare identifiers (column names) → prefixed with source_view
    """
    import re
    # An identifier atom can be a standard name or enclosed in double quotes (e.g. "#nation")
    ident_atom = r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")*")'

    # Token pattern: string literals, numbers, prefixed identifiers, plain identifiers, other chars
    token_re = re.compile(
        r"('(?:[^']|'')*')"  # single-quoted string
        rf"|({ident_atom}(?:\s*\.\s*{ident_atom})+)"  # already prefixed: a.b, "#nation".col, a."b", etc.
        r'|("(?:[^"]|"")*")'  # standalone double-quoted string
        r"|(\b\d+(?:\.\d+)?\b)"  # numeric literal
        r"|([A-Za-z_][A-Za-z0-9_]*)"  # plain identifier
        r"|(\S|\s+)"  # any other char/whitespace
    )
    result = []
    tokens = list(token_re.finditer(expr))

    for m in tokens:
        sq, prefixed, dq, num, plain, other = m.groups()

        if sq or dq or num:
            result.append(m.group(0))
        elif prefixed:
            result.append(prefixed)
        elif plain:
            # Check context:
            # - Followed by '(' (function call) → don't prefix
            # - Preceded or followed by '.' (part of a qualified identifier) → don't prefix
            before = expr[: m.start()].rstrip()
            after = expr[m.end() :].lstrip()

            if after.startswith("(") or before.endswith(".") or after.startswith("."):
                result.append(plain)
            else:
                result.append(f"{source_view}.{plain}")
        else:
            result.append(m.group(0))
    return "".join(result)


def _extract_expression(expression: dict | None) -> str | None:
    """Extract the best expression string from an Ossie expression dict.

    Preference order: DENODO dialect → ANSI_SQL dialect → first available.
    """
    if not expression or not isinstance(expression, dict):
        return None
    dialects: list[dict] = expression.get("dialects") or []
    if not dialects:
        return None

    by_dialect = {d.get("dialect"): d.get("expression") for d in dialects if isinstance(d, dict)}

    for preferred in ("DENODO", "ANSI_SQL"):
        if preferred in by_dialect and by_dialect[preferred]:
            return str(by_dialect[preferred])

    # Fall back to first available
    for d in dialects:
        expr = d.get("expression")
        if expr:
            return str(expr)

    return None


# ---------------------------------------------------------------------------
# VQL rendering
# ---------------------------------------------------------------------------

def _render_vql(associations: list[AssociationModel], mv: MetricViewModel) -> str:
    """Render the full VQL string from internal models."""
    parts: list[str] = []

    for assoc in associations:
        parts.append(_render_association(assoc))

    parts.append(_render_metric_view(mv))

    return "\n\n".join(parts)


def _render_association(assoc: AssociationModel) -> str:
    """Render a CREATE OR REPLACE ASSOCIATION VQL statement."""
    lines: list[str] = []

    if assoc.vdb is not None:
        lines.append(f"CONNECT DATABASE {assoc.vdb};")

    header = f"CREATE OR REPLACE ASSOCIATION {assoc.name}"
    if assoc.referential_constraint:
        header += " REFERENTIAL CONSTRAINT"
    lines.append(header)

    if assoc.folder:
        lines.append(f"FOLDER = '{assoc.folder}'")

    for ep in assoc.endpoints:
        principal = " PRINCIPAL" if ep.is_principal else ""
        lines.append(
            f"    ENDPOINT {ep.role_name} "
            f"{ep.view_name}{principal} ({ep.cardinality})"
        )

    for left, right in assoc.mappings:
        lines.append(f"    ADD MAPPING {left}={right}")

    if assoc.vdb is not None:
        lines[-1] += ";"
        lines.append(f"CLOSE")

    return "\n".join(lines) + ";"


def _render_metric_view(mv: MetricViewModel) -> str:
    """Render a CREATE OR REPLACE METRIC VIEW VQL statement."""
    lines: list[str] = []

    lines.append(f"CREATE OR REPLACE METRIC VIEW {mv.name}")

    if mv.folder:
        lines.append(f"    FOLDER = '{mv.folder}'")

    if mv.description:
        lines.append(f"    DESCRIPTION = '{mv.description}'")

    if mv.tags:
        tags_str = ", ".join(mv.tags)
        lines.append(f"    TAGS ({tags_str})")

    # SOURCES
    if mv.sources:
        source_lines = []
        for src in mv.sources:
            alias = src.alias
            if src.database:
                view_ref = f"{src.database}.{src.view}"
            else:
                view_ref = src.view
            source_lines.append(f"        {alias} AS {view_ref}")
        lines.append("    SOURCES (")
        lines.append(",\n".join(source_lines))
        lines.append("    )")

    #FILTER
    if mv.filter_condition:
        lines.append("    FILTER (")
        lines.append("        "+mv.filter_condition)
        lines.append("    )")

    # ASSOCIATIONS
    if mv.associations:
        assoc_lines = []
        for ref in mv.associations:
            from_q = ref.from_alias
            to_q = ref.to_alias
            assoc_name_q = ref.association_name
            join = f" {ref.join_type}" if ref.join_type else ""
            assoc_lines.append(f"        {from_q} {to_q} {assoc_name_q}{join}")
        lines.append("    ASSOCIATIONS (")
        lines.append(",\n".join(assoc_lines))
        lines.append("    )")

    # DIMENSIONS
    if mv.dimensions:
        dim_lines = []
        for group in mv.dimensions:
            if group.group_name is None:
                # Ungrouped fields
                for f in group.fields:
                    dim_lines.append(
                        f"        {f.name} AS {f.expression}"
                    )
            else:
                inner = []
                for f in group.fields:
                    inner.append(
                        f"            {f.name} AS {f.expression}"
                    )
                dim_lines.append(
                    f"        {group.group_name}(\n"
                    + ",\n".join(inner)
                    + "\n        )"
                )
        lines.append("    DIMENSIONS (")
        lines.append(",\n".join(dim_lines))
        lines.append("    )")

    # METRICS
    if mv.metrics:
        metric_lines = []
        for m in mv.metrics:
            metric_lines.append(
                f"        {m.name} AS {m.expression}"
            )
        lines.append("    METRICS (")
        lines.append(",\n".join(metric_lines))
        lines.append("    )")

    # CONTEXT
    if mv.context:
        ctx_pairs = ", ".join(f"'{k}' = '{v}'" for k, v in mv.context.items())
        lines.append(f"CONTEXT ({ctx_pairs});")
    else:
        # Close the statement
        lines[-1] = lines[-1].rstrip(";") + ";"

    return "\n".join(lines)

