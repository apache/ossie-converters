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
from typing import Any

from ossie import (
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

from ataccama_ossie.models import CatalogAttribute, CatalogItem, CatalogItemBundle, Term

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

# A source segment safe to leave unquoted. This is the intersection of what the engines
# accept, not any one engine's full grammar: a letter or underscore first (Snowflake,
# PostgreSQL, Oracle, SQL Server and BigQuery all reject a leading digit), then letters,
# digits and underscores. `$` is legal unquoted in Snowflake and Oracle but not in
# Databricks or BigQuery, so it is left out -- quoting a segment that did not need it
# only costs case sensitivity on that one segment.
_BARE_SOURCE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Words every engine reserves, which need quoting even though their characters are fine.
# Deliberately short: each engine reserves far more, but a catalog item named `JOIN` or
# `ORDER` is the realistic case, and over-quoting a non-reserved word costs case
# sensitivity.
_RESERVED_SOURCE_PARTS = frozenset(
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


def _unique_name(desired: str, used: set[str], *, fallback: str) -> str:
    """Return a name unique within ``used`` (Ossie requires unique dataset/field names)."""
    base = desired.strip() or fallback
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _sql_identifier(name: str) -> str:
    """Quote a column name as an ANSI SQL identifier (names may contain spaces/punctuation)."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _quote_source_part(part: str) -> str:
    """Quote a source segment unless it is already a bare SQL identifier.

    Segments are joined with ``.`` to form a dotted source, so anything a SQL parser
    would not read as one unquoted identifier has to be quoted: a dot would add levels
    (``orders.csv``), a dash would read as subtraction (``TX_2022-07``), a space or
    newline would end the identifier.

    Only segments that read as a plain unquoted identifier are left bare, since consumers
    case-fold unquoted identifiers but preserve quoted ones -- quoting everything would
    make the common case case-sensitive and stop it matching the physical table.
    """
    if _BARE_SOURCE_PART.fullmatch(part) and part.lower() not in _RESERVED_SOURCE_PARTS:
        return part
    escaped = part.replace('"', '""')
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
    return ".".join(_quote_source_part(part) for part in parts)


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
    """Build the dataset-level DQ block from a raw DqResults payload.

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
    if dq_results.get("processingUrn"):
        dq["processing_urn"] = dq_results["processingUrn"]
    if dq_results.get("dqResultsLink"):
        dq["results_link"] = dq_results["dqResultsLink"]

    return dq or None


def _attribute_dq_index(dq_results: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Map attributeUrn -> quality summary from a raw DqResults payload."""
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

    # Keys (from the Generic Metadata Entities API). primary_key = the key's columns;
    # unique_keys = the unique indexes that are not the primary key.
    primary_key_columns = (bundle.primary_key or {}).get("columns") or None
    unique_key_sets = _secondary_unique_key_sets(primary_key_columns, bundle.unique_indexes)

    dataset_dq = _dataset_dq(bundle.dq_results, bundle.dq_threshold_pct)

    ai_context = _terms_ai_context([ta.term_urn for ta in item.term_assignments], bundle.terms)
    if dq_ai_warnings:
        ai_context = _inject_warning(ai_context, _dataset_dq_warning(dataset_dq))

    dq_by_attr = _attribute_dq_index(bundle.dq_results)
    used_field_names: set[str] = set()
    fields = [attribute_to_field(attr, bundle.terms, used_field_names, dq_by_attr) for attr in bundle.attributes]

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


def _build_relationships(bundles: list[CatalogItemBundle], datasets: list[OssieDataset]) -> list[OssieRelationship]:
    """Derive Ossie relationships from foreign keys.

    Only relationships whose target table is also in the converted set are emitted —
    Ossie requires both endpoints of a relationship to exist as datasets, so an FK to a
    table the caller didn't include is skipped (rather than producing an invalid model).
    """
    dataset_names = {d.name for d in datasets}
    relationships: list[OssieRelationship] = []
    used_names: set[str] = set()
    for bundle, dataset in zip(bundles, datasets):
        from_name = dataset.name
        for fk in bundle.foreign_keys:
            to_name = fk.get("referenced_table")
            from_cols = fk.get("columns") or []
            to_cols = fk.get("referenced_columns") or []
            if not to_name or to_name not in dataset_names:
                continue  # target not in the converted set
            if not from_cols or len(from_cols) != len(to_cols):
                continue  # need a well-formed, equal-cardinality column pairing
            rel_name = _unique_name(fk.get("name") or f"{from_name}_to_{to_name}", used_names, fallback="relationship")
            relationships.append(
                OssieRelationship(
                    name=rel_name,
                    from_dataset=from_name,
                    to=to_name,
                    from_columns=from_cols,
                    to_columns=to_cols,
                )
            )
    return relationships


# --- top level -----------------------------------------------------------


def ataccama_to_ossie(
    bundles: list[CatalogItemBundle],
    model_name: str = "ataccama_model",
    model_description: str | None = None,
    tenant: str | None = None,
    dq_ai_warnings: bool = False,
) -> OssieDocument:
    """Convert a list of catalog-item bundles into an Ossie document.

    When ``dq_ai_warnings`` is set, datasets that Ataccama flags as below quality (below
    the configured threshold, or with active findings) get a natural-language warning
    appended to their ``ai_context.instructions`` (see ``_dataset_dq_warning``).
    """
    used_dataset_names: set[str] = set()
    datasets = [bundle_to_dataset(b, used_dataset_names, dq_ai_warnings) for b in bundles]

    model_ext: dict[str, Any] = {"source": "ataccama-one-catalog"}
    if tenant:
        model_ext["tenant"] = tenant

    return OssieDocument(
        name=model_name,
        description=model_description,
        datasets=datasets,
        relationships=_build_relationships(bundles, datasets) or None,
        custom_extensions=[_ataccama_extension(model_ext)],
    )
