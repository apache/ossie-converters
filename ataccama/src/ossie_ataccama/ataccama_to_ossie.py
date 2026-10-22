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

"""Convert Ataccama ONE catalog metadata to an Ossie semantic model.

Scope: the caller supplies one or more catalog items (as :class:`CatalogItemBundle`s);
each becomes an Ossie dataset, and the collection becomes a single Ossie semantic model.

Mapping summary (see README for the full table and known limitations):
  CatalogItem            -> dataset (name, source, description, ai_context, custom_extensions)
  CatalogAttribute       -> field (name, ANSI_SQL identifier expression, dimension.is_time,
                                    description, ai_context, custom_extensions)
  Term / termAssignments -> ai_context.synonyms + ATACCAMA custom_extension
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Iterable
from typing import Any, NamedTuple

from ossie import (
    OssieAIContext,
    OssieAIContextObject,
    OssieCustomExtension,
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieRelationship,
)

from ossie_ataccama.models import CatalogAttribute, CatalogItem, CatalogItemBundle, Term

# todo: Until apache/ossie#629 adds OssieVendor.ATACCAMA and the ossie submodule is bumped past it.
ATACCAMA_VENDOR_NAME = "ATACCAMA"

# Ataccama semantic data types that represent points in time.
TIME_DATA_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIME"}

# Ataccama semantic data types, mapped to Ossie's portable vocabulary. Ataccama has no
# exact-decimal type, so FLOAT becomes the approximate Ossie Float even for columns the
# source stores as NUMERIC; columnType is preserved in custom_extensions for consumers
# that need the exact/approximate distinction back.
OSSIE_DATA_TYPES = {
    "STRING": OssieDataType.STRING,
    "INTEGER": OssieDataType.INTEGER,
    "LONG": OssieDataType.INTEGER,
    "FLOAT": OssieDataType.FLOAT,
    "BOOLEAN": OssieDataType.BOOLEAN,
    "DATE": OssieDataType.DATE,
    "DATETIME": OssieDataType.DATE_TIME,
}

# A value safe to leave unquoted. This is the intersection of what the engines
# accept, not any one engine's full grammar: a letter or underscore first (Snowflake,
# PostgreSQL, Oracle, SQL Server and BigQuery all reject a leading digit), then letters,
# digits and underscores. `$` is legal unquoted in Snowflake and Oracle but not in
# Databricks or BigQuery, so it is left out -- quoting a segment that did not need it
# only costs case sensitivity for that one value.
_BARE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Words every engine reserves, which need quoting even though their characters are fine.
# Deliberately short: each engine reserves far more, but a catalog item named `JOIN` or
# `ORDER` is the realistic case, and over-quoting a non-reserved word only costs case
# sensitivity.
_RESERVED_WORDS = frozenset(
    """
    all alter and any as between by case cast check column create cross current default
    delete distinct drop else end exists false for from full grant group having in inner
    insert intersect into is join left like natural not null on or order outer primary
    references revoke right select set some table then to true union unique update using
    values when where with
    """.split()  # noqa: SIM905
)


# --- helpers -------------------------------------------------------------


def flatten_richtext(value: Any) -> str | None:
    """Flatten an Ataccama description into plain text.

    Descriptions arrive either as a plain string or as Slate rich-text JSON, e.g.
    ``[{"type": "paragraph", "children": [{"text": "..."}]}]`` (sometimes as a JSON
    string). Returns ``None`` when there is no usable text.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        # Rich text is occasionally delivered as a JSON-encoded string.
        if stripped[0] in "[{":
            try:
                return flatten_richtext(json.loads(stripped))
            except (ValueError, TypeError):
                return stripped
        return stripped

    def collect(node: Any, out: list[str]) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                out.append(text)
            for child in node.get("children", []) or []:
                collect(child, out)
        elif isinstance(node, list):
            for child in node:
                collect(child, out)

    parts: list[str] = []
    collect(value, parts)
    text = "".join(parts).strip()
    return text or None


def _as_identifier(value: str) -> str:
    """Render a catalog name or source segment as an identifier a consumer can resolve.

    A value that already reads as a plain unquoted identifier is left alone so it keeps
    case-folding; anything else becomes a delimited identifier. Delimiting preserves the
    catalog name exactly instead of rewriting it, which matters because the same value
    also has to match the physical object.

    This settles both roles because the rule is the same. For a dataset or field ``name``,
    consumers read it as a SQL identifier -- Snowflake rejects a bare ``TX_2022-07`` with
    "Invalid identifier". For a ``source`` segment, the segments are joined with ``.``, so
    a dot would add levels (``orders.csv``), a dash would read as subtraction and a space
    or newline would end the identifier.
    """
    if _BARE_IDENTIFIER.fullmatch(value) and value.lower() not in _RESERVED_WORDS:
        return value
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _unique_name(desired: str, used: set[str], *, fallback: str) -> str:
    """Return a declarable name unique within ``used``.

    The de-duplication suffix is applied to the catalog name before it is rendered, so a
    repeated awkward name becomes ``"test coll_2"`` rather than ``"test coll"_2``.
    """
    base = desired.strip() or fallback
    candidate = _as_identifier(base)
    n = 2
    while candidate in used:
        candidate = _as_identifier(f"{base}_{n}")
        n += 1
    used.add(candidate)
    return candidate


def _sql_identifier(name: str) -> str:
    """Quote a column name as an ANSI SQL identifier (names may contain spaces/punctuation)."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def build_source(item: CatalogItem) -> str:
    """Best-effort physical source reference.

    The Catalog API does not expose ``database.schema.table``; it only gives a
    folder-name hierarchy (``locations``, leaf-first) plus the item name. We build a
    dotted namespace from the reversed locations followed by the item name. The
    authoritative connection/source URNs are preserved in custom_extensions.
    """
    parts = [loc.name for loc in reversed(item.locations) if loc.name]
    parts.append(item.name)
    return ".".join(_as_identifier(part) for part in parts)


def _terms_ai_context(term_urns: list[str], terms: dict[str, Term]) -> OssieAIContextObject | None:
    """Build an ai_context object from assigned business terms."""
    synonyms: list[str] = []
    instructions: list[str] = []
    for urn in term_urns:
        term = terms.get(urn)
        if not term:
            continue
        if term.name and term.name not in synonyms:
            synonyms.append(term.name)
        desc = flatten_richtext(term.description)
        if desc:
            instructions.append(f"{term.name}: {desc}" if term.name else desc)
    if not instructions and not synonyms:
        return None
    return OssieAIContextObject(
        instructions=" ".join(instructions) or None,
        synonyms=tuple(synonyms) or None,
    )


def _ataccama_extension(data: dict[str, Any]) -> OssieCustomExtension:
    """Wrap Ataccama-specific metadata as an Ossie custom_extension entry."""
    return OssieCustomExtension(vendor_name=ATACCAMA_VENDOR_NAME, data=json.dumps(data, sort_keys=True))


# --- data quality ---


def _quality_summary(overall_quality: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn an OverallQuality {passedCount, failedCount} into a compact summary.

    ``pass_rate_pct`` is Ataccama's overall quality expressed as a percentage
    (passed / (passed + failed)). The DQ API returns only counts, not a preformatted
    percentage, so the converter formats it; the underlying counts are Ataccama's own.
    """
    if not overall_quality:
        return None
    passed = overall_quality.get("passedCount") or 0
    failed = overall_quality.get("failedCount") or 0
    total = passed + failed
    summary: dict[str, Any] = {"passed": passed, "failed": failed}
    if total:
        summary["pass_rate_pct"] = round(passed / total * 100, 1)
    return summary


def _dataset_dq(dq_results: dict[str, Any] | None, threshold_pct: float | None = None) -> dict[str, Any] | None:
    """Build the dataset-level DQ block from a raw DqEvaluationResults payload.

    ``threshold_pct`` is the monitor's configured overall pass/fail bar (from the
    monitor config). When present alongside a pass rate, ``below_threshold`` records
    whether the data is under that bar — the same pass/fail state shown in Ataccama.
    """
    if not dq_results:
        return None
    dq = _quality_summary(dq_results.get("overallQuality")) or {}

    if threshold_pct is not None:
        dq["threshold_pct"] = threshold_pct
        if "pass_rate_pct" in dq:
            # Use the unrounded rate for classification; pass_rate_pct is display-only.
            total = dq["passed"] + dq["failed"]
            dq["below_threshold"] = dq["passed"] / total * 100 < threshold_pct

    dimensions = []
    for dim in dq_results.get("dimensionResults", []) or []:
        passed = dim.get("passedCount") or 0
        failed = dim.get("failedCount") or 0
        if passed + failed == 0:  # dimension not evaluated in this processing
            continue
        entry = {"name": (dim.get("dimension") or {}).get("name"), "passed": passed, "failed": failed}
        entry["pass_rate_pct"] = round(passed / (passed + failed) * 100, 1)
        dimensions.append(entry)
    if dimensions:
        dq["dimensions"] = dimensions

    # Always report the active-finding count (0 = no open data-quality issues) so the
    # "clean" state is an explicit signal, not just an omission.
    dq["active_findings"] = sum(1 for f in dq_results.get("overallDqFindings", []) or [] if f.get("status") == "ACTIVE")
    if dq_results.get("workflowUrn"):
        dq["processing_urn"] = dq_results["workflowUrn"]
    if dq_results.get("dqEvaluationResultsLink"):
        dq["results_link"] = dq_results["dqEvaluationResultsLink"]

    return dq or None


def _attribute_dq_index(dq_results: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Map attributeUrn -> quality summary from a raw DqEvaluationResults payload."""
    index: dict[str, dict[str, Any]] = {}
    if not dq_results:
        return index
    for attr_result in dq_results.get("dqAttributeResults", []) or []:
        urn = attr_result.get("attributeUrn")
        summary = _quality_summary(attr_result.get("overallQuality"))
        if urn and summary:
            index[urn] = summary
    return index


# --- optional AI warnings driven by DQ ---


def _dataset_dq_warning(dq: dict[str, Any] | None) -> str | None:
    """A natural-language warning for AI consumers when a dataset is below quality.

    Fires only on Ataccama's own signals — below the monitor's configured threshold, or
    an active finding — so Ataccama remains the sole source of truth. If neither applies
    (including when no threshold is configured), no warning is produced.
    """
    if not dq:
        return None
    reasons: list[str] = []
    if dq.get("below_threshold") is True:
        reasons.append(f"below the configured {dq.get('threshold_pct')}% quality threshold")
    if dq.get("active_findings"):
        reasons.append(f"{dq['active_findings']} active data-quality finding(s)")
    if not reasons:
        return None
    pass_rate = dq.get("pass_rate_pct")
    prefix = (
        f"{pass_rate}% of quality checks passed on the latest run" if pass_rate is not None else "quality checks failed"
    )
    return (
        f"Data-quality warning: {prefix} ({'; '.join(reasons)}). "
        "Verify this data before using it for analysis or automated decisions."
    )


def _inject_warning(ai_context: OssieAIContextObject | None, warning: str | None) -> OssieAIContextObject | None:
    """Append a warning to an ai_context object's instructions (creating one if needed)."""
    if not warning:
        return ai_context
    if ai_context is None:
        return OssieAIContextObject(instructions=warning)
    existing = ai_context.instructions
    return ai_context.model_copy(update={"instructions": f"{existing} {warning}" if existing else warning})


# --- attribute -> field --------------------------------------------------


def _ossie_datatype(data_type: str | None, field_name: str) -> OssieDataType | None:
    """Map an Ataccama dataType to Ossie's portable vocabulary.

    Ossie omits ``datatype`` when the type is unknown rather than inventing one, so an
    Ataccama type outside the mapping warns and yields ``None``.
    """
    if not data_type:
        return None
    datatype = OSSIE_DATA_TYPES.get(data_type.upper())
    if datatype is None:
        warnings.warn(
            f"Field '{field_name}' has unrecognized Ataccama dataType '{data_type}'; datatype omitted.",
            stacklevel=2,
        )
    return datatype


def attribute_to_field(
    attr: CatalogAttribute,
    terms: dict[str, Term],
    used: set[str],
    dq_by_attr: dict[str, dict[str, Any]] | None = None,
) -> OssieField:
    name = _unique_name(attr.name, used, fallback="column")
    is_time = bool(attr.data_type and attr.data_type.upper() in TIME_DATA_TYPES)
    field_dq = dq_by_attr.get(attr.urn) if dq_by_attr else None

    # Preserve source-system typing and the attribute URN for round-tripping.
    ext_data: dict[str, Any] = {"attribute_urn": attr.urn}
    if attr.data_type:
        ext_data["data_type"] = attr.data_type
    if attr.column_type:
        ext_data["column_type"] = attr.column_type
    if attr.term_assignments:
        ext_data["term_urns"] = [ta.term_urn for ta in attr.term_assignments]
    if field_dq:
        ext_data["dq"] = field_dq

    return OssieField(
        name=name,
        expression=OssieExpression(
            dialects=[
                OssieDialectExpression(
                    dialect=OssieDialect.ANSI_SQL,
                    expression=_sql_identifier(attr.name or name),
                )
            ]
        ),
        dimension=OssieDimension(is_time=True) if is_time else None,
        datatype=_ossie_datatype(attr.data_type, name),
        description=flatten_richtext(attr.description) or flatten_richtext(attr.comment),
        ai_context=_terms_ai_context([ta.term_urn for ta in attr.term_assignments], terms),
        custom_extensions=[_ataccama_extension(ext_data)],
    )


# --- catalog item -> dataset ---------------------------------------------


def _field_names_by_column(attributes: list[CatalogAttribute], fields: list[OssieField]) -> dict[str, str]:
    """Map each physical column name to the field name the dataset declares for it."""
    return {attr.name: field.name for attr, field in zip(attributes, fields) if attr.name}


def _as_field_names(columns: list[str], field_names_by_column: dict[str, str]) -> list[str]:
    """Translate physical column names into the field names the dataset declares.

    A column with no field of its own (an index over a column the catalog item does not
    expose as an attribute) falls back to its sanitized name, which is the name that
    field would have had.
    """
    return [field_names_by_column.get(column) or _as_identifier(column) for column in columns]


def _secondary_unique_key_sets(
    primary_key_columns: list[str] | None, unique_indexes: list[dict[str, Any]]
) -> list[list[str]]:
    """Unique indexes other than the primary key, as column sets without repeats.

    A primary key is a unique constraint by definition, and ``dataset.primary_key``
    already carries it, so restating it in ``unique_keys`` adds nothing -- and Snowflake
    rejects a semantic view that declares the same columns as both. Every relational
    engine backs a primary key with a real unique index, so that index does arrive here
    and has to be filtered out.

    Column order is a property of the index, not of the uniqueness guarantee, so key
    sets are compared as sets: a unique index on ``(b, a)`` is the same constraint as a
    primary key on ``(a, b)``. The surviving index keeps its own column order.
    """
    seen: set[frozenset[str]] = {frozenset(primary_key_columns)} if primary_key_columns else set()
    key_sets: list[list[str]] = []
    for index in unique_indexes:
        columns = index.get("columns")
        if not columns or frozenset(columns) in seen:
            continue
        seen.add(frozenset(columns))
        key_sets.append(columns)
    return key_sets


def bundle_to_dataset(
    bundle: CatalogItemBundle,
    used_dataset_names: set[str],
    dq_ai_warnings: bool = False,
) -> OssieDataset:
    item = bundle.item
    name = _unique_name(item.name, used_dataset_names, fallback="dataset")

    dataset_dq = _dataset_dq(bundle.dq_results, bundle.dq_threshold_pct)

    ai_context = _terms_ai_context([ta.term_urn for ta in item.term_assignments], bundle.terms)
    if dq_ai_warnings:
        ai_context = _inject_warning(ai_context, _dataset_dq_warning(dataset_dq))

    dq_by_attr = _attribute_dq_index(bundle.dq_results)
    used_field_names: set[str] = set()
    fields = [attribute_to_field(attr, bundle.terms, used_field_names, dq_by_attr) for attr in bundle.attributes]

    # Keys (from the Generic Metadata Entities API) name physical columns, but a key has
    # to reference the field names the dataset actually declares, which sanitizing and
    # de-duplication may have changed. Fields are built first so the mapping exists.
    # primary_key = the key's columns; unique_keys = the unique indexes that are not it.
    columns_to_fields = _field_names_by_column(bundle.attributes, fields)
    key_columns = (bundle.primary_key or {}).get("columns")
    primary_key_columns = _as_field_names(key_columns, columns_to_fields) if key_columns is not None else None
    unique_key_sets = [
        _as_field_names(columns, columns_to_fields)
        for columns in _secondary_unique_key_sets(key_columns, bundle.unique_indexes)
    ]

    # Preserve everything with no Ossie-core home so the model round-trips.
    ext_data: dict[str, Any] = {"catalog_item_urn": item.urn}
    if item.connection_urn:
        ext_data["connection_urn"] = item.connection_urn
    if item.source_urn:
        ext_data["source_urn"] = item.source_urn
    if item.origin_path:
        ext_data["origin_path"] = item.origin_path
    if item.locations:
        ext_data["locations"] = [loc.name for loc in item.locations]
    if item.stewardship_group_urn:
        ext_data["stewardship_group_urn"] = item.stewardship_group_urn
    if item.primary_dq_monitor_urn:
        ext_data["primary_dq_monitor_urn"] = item.primary_dq_monitor_urn
    if item.term_assignments:
        ext_data["term_urns"] = [ta.term_urn for ta in item.term_assignments]
    if item.aliases:
        ext_data["aliases"] = item.aliases
    if dataset_dq:
        ext_data["dq"] = dataset_dq

    return OssieDataset(
        name=name,
        source=build_source(item),
        primary_key=primary_key_columns,
        unique_keys=unique_key_sets or None,
        description=flatten_richtext(item.description),
        ai_context=ai_context,
        fields=fields or None,
        custom_extensions=[_ataccama_extension(ext_data)],
    )


# --- relationships -------------------------------------------------------


class ConvertedDataset(NamedTuple):
    """What relationship building needs from a converted item, without the bundle.

    Conversion makes a single pass over the bundles, so the bundle itself is released
    once its dataset exists; the foreign keys and the column mapping are kept instead.
    """

    name: str
    table: str
    schema: str | None
    foreign_keys: list[dict[str, Any]]
    field_names_by_column: dict[str, str]


def _foreign_key_target(
    foreign_key: dict[str, Any], datasets_by_table: dict[str, list[ConvertedDataset]]
) -> ConvertedDataset | None:
    """The converted dataset a foreign key references, or None if it is missing or ambiguous.

    Tables are matched by name, and by schema too whenever the foreign key names one. A
    candidate whose schema is unknown does not match a named schema: a missing relationship
    is reported, a wrong one would not be.
    """
    referenced_table = foreign_key.get("referenced_table")
    if referenced_table is None:
        warnings.warn(f"Foreign key {foreign_key.get('name')} has no referenced table; skipped")
        return None
    referenced_schema = foreign_key.get("referenced_schema")
    candidates = datasets_by_table.get(referenced_table, [])
    if referenced_schema is not None:
        candidates = [candidate for candidate in candidates if candidate.schema == referenced_schema]
    if len(candidates) == 1:
        return candidates[0]

    qualified_table = f"{referenced_schema}.{referenced_table}" if referenced_schema else referenced_table
    if candidates:
        warnings.warn(
            f"Foreign key {foreign_key.get('name')} references table {qualified_table}, which matches "
            f"{len(candidates)} converted datasets ({', '.join(c.name for c in candidates)}); skipped"
        )
    else:
        warnings.warn(
            f"Foreign key {foreign_key.get('name')} references table {qualified_table} which is not in the converted set"
        )
    return None


def _build_relationships(converted: list[ConvertedDataset]) -> list[OssieRelationship]:
    """Derive Ossie relationships from foreign keys.

    Foreign keys name physical tables and columns, while a relationship has to reference
    the dataset and field names the model declares, so both ends are resolved through the
    converted set.

    Only relationships whose target table is also in that set are emitted — Ossie requires
    both endpoints of a relationship to exist as datasets, so an FK to a table the caller
    didn't include is skipped (rather than producing an invalid model).
    """
    datasets_by_table: dict[str, list[ConvertedDataset]] = {}
    for entry in converted:
        if entry.table:
            datasets_by_table.setdefault(entry.table, []).append(entry)
    relationships: list[OssieRelationship] = []
    used_names: set[str] = set()
    for entry in converted:
        for fk in entry.foreign_keys:
            target = _foreign_key_target(fk, datasets_by_table)
            from_cols = fk.get("columns") or []
            to_cols = fk.get("referenced_columns") or []
            if target is None:
                continue
            if not from_cols or len(from_cols) != len(to_cols):
                continue  # need a well-formed, equal-cardinality column pairing
            rel_name = _unique_name(
                fk.get("name") or f"{entry.name}_to_{target.name}", used_names, fallback="relationship"
            )
            relationships.append(
                OssieRelationship(
                    name=rel_name,
                    from_dataset=entry.name,
                    to=target.name,
                    from_columns=_as_field_names(from_cols, entry.field_names_by_column),
                    to_columns=_as_field_names(to_cols, target.field_names_by_column),
                )
            )
    return relationships


# --- consumer compatibility ----------------------------------------------


def _flat_ai_context(ai_context: OssieAIContext | None) -> str | None:
    """Render an ai_context as the plain-string form the Ossie schema also allows.

    ``ai_context`` is a ``Union[str, OssieAIContextObject]``, so both forms are
    spec-valid; some consumers (e.g. Snowflake) accept only the string.
    """
    if ai_context is None or isinstance(ai_context, str):
        return ai_context
    parts: list[str] = []
    if ai_context.instructions:
        parts.append(ai_context.instructions)
    if ai_context.synonyms:
        parts.append("Synonyms: " + ", ".join(ai_context.synonyms) + ".")
    if ai_context.examples:
        parts.append("Examples: " + "; ".join(ai_context.examples) + ".")
    return "\n".join(parts) or None


def _flattened_nodes(
    nodes: list[OssieField] | list[OssieRelationship] | None,
) -> list[OssieField] | list[OssieRelationship] | None:
    """Flatten each node's ai_context, leaving an absent list absent.

    Serialization drops ``None`` but keeps ``[]``, so turning a missing list into an
    empty one would add a meaningless ``fields: []`` / ``relationships: []`` to the output.
    """
    if not nodes:
        return nodes
    return [node.model_copy(update={"ai_context": _flat_ai_context(node.ai_context)}) for node in nodes]


def _flatten_ai_contexts(document: OssieDocument) -> OssieDocument:
    """Return a copy of ``document`` with every ai_context rendered as a plain string."""
    datasets = [
        dataset.model_copy(
            update={
                "ai_context": _flat_ai_context(dataset.ai_context),
                "fields": _flattened_nodes(dataset.fields),
            }
        )
        for dataset in document.datasets
    ]
    return document.model_copy(
        update={
            "ai_context": _flat_ai_context(document.ai_context),
            "datasets": datasets,
            "relationships": _flattened_nodes(document.relationships),
        }
    )


# --- top level -----------------------------------------------------------


def ataccama_to_ossie(
    bundles: Iterable[CatalogItemBundle],
    model_name: str = "ataccama_model",
    model_description: str | None = None,
    tenant: str | None = None,
    dq_ai_warnings: bool = False,
    flat_ai_context: bool = False,
    ossie_version: str | None = None,
) -> OssieDocument:
    """Convert catalog-item bundles into an Ossie document.

    When ``dq_ai_warnings`` is set, datasets that Ataccama flags as below quality (below
    the configured threshold, or with active findings) get a natural-language warning
    appended to their ``ai_context.instructions`` (see ``_dataset_dq_warning``).

    When ``flat_ai_context`` is set, every ``ai_context`` is emitted as a plain string
    instead of an object. Both forms are valid Ossie, but some consumers (e.g. Snowflake)
    only accept the string form.

    ``ossie_version`` stamps a different spec version on the document, for consumers
    pinned to a released spec that reject this repo's in-development version. The Ossie
    schema pins the version, so an overridden document does not validate against it --
    hence the warning.
    """
    used_dataset_names: set[str] = set()
    datasets: list[OssieDataset] = []
    converted: list[ConvertedDataset] = []
    for bundle in bundles:
        dataset = bundle_to_dataset(bundle, used_dataset_names, dq_ai_warnings)
        datasets.append(dataset)
        converted.append(
            ConvertedDataset(
                name=dataset.name,
                table=bundle.item.name,
                schema=bundle.schema_name,
                foreign_keys=bundle.foreign_keys,
                field_names_by_column=_field_names_by_column(bundle.attributes, dataset.fields or []),
            )
        )

    model_ext: dict[str, Any] = {"source": "ataccama-one-catalog"}
    if tenant:
        model_ext["tenant"] = tenant

    document = OssieDocument(
        name=model_name,
        description=model_description,
        datasets=datasets,
        relationships=_build_relationships(converted) or None,
        custom_extensions=[_ataccama_extension(model_ext)],
    )
    if ossie_version and ossie_version != document.version:
        warnings.warn(
            f"Stamping Ossie version '{ossie_version}' instead of '{document.version}'. "
            "The Ossie schema pins the version, so this document will not validate "
            "against it.",
            stacklevel=2,
        )
        document = document.model_copy(update={"version": ossie_version})
    return _flatten_ai_contexts(document) if flat_ai_context else document
