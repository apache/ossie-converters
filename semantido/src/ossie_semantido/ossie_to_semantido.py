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

"""Apache Ossie document -> generated semantido model code.

This direction is code generation: it emits a Python module of
``@semantic_table``-decorated SQLAlchemy models. Constructs with no
semantido equivalent (metrics, computed/non-ANSI expressions) are dropped
with a recorded ConverterIssue and listed in a TODO comment block at the
top of the generated file.

Every value that reaches the generated source is emitted through ``repr()``
and every Python identifier is sanitized, so a hostile or merely awkward
name (a quote, a hyphen, a keyword) cannot break or inject into the output.
When an identifier has to be rewritten, the original column name is still
carried to the database through ``Column("original", ...)``.
"""

import json
import keyword
import re
from collections import defaultdict
from typing import List, Optional

from ossie import (
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieDocument,
    OssieField,
)
from semantido.generators.semantic_layer import PrivacyLevel, TimeGrain

from ossie_semantido.constants import VENDOR_NAME
from ossie_semantido.converter_issues import (
    ConverterIssue,
    ConverterIssueType,
    ConverterResult,
)

_TYPE_MAP = {
    "VARCHAR": "String",
    "CHAR": "String",
    "TEXT": "String",
    "INT": "Integer",
    "INTEGER": "Integer",
    "BIGINT": "BigInteger",
    "SMALLINT": "SmallInteger",
    "NUMERIC": "Numeric",
    "DECIMAL": "Numeric",
    "FLOAT": "Float",
    "REAL": "Float",
    "DOUBLE": "Double",
    "DATE": "Date",
    "TIME": "Time",
    "DATETIME": "DateTime",
    "TIMESTAMP": "DateTime",
    "BOOLEAN": "Boolean",
    "BOOL": "Boolean",
}

# Fallback for documents from other tools that carry the portable Ossie
# ``datatype`` but no SEMANTIDO extension with an exact SQL type. The portable
# vocabulary has no width, so the integer/decimal entries are the base types.
_OSSIE_TO_SA = {
    OssieDataType.STRING: "String",
    OssieDataType.INTEGER: "Integer",
    OssieDataType.DECIMAL: "Numeric",
    OssieDataType.FLOAT: "Float",
    OssieDataType.BOOLEAN: "Boolean",
    OssieDataType.DATE: "Date",
    OssieDataType.TIME: "Time",
    OssieDataType.DATE_TIME: "DateTime",
    OssieDataType.DATE_TIME_TZ: "DateTime",
    OssieDataType.OPAQUE: "String",
}

# All SQLAlchemy type names the mappings above can emit; imported unconditionally
# into the generated module (an unused import is harmless in generated code).
_SA_BASE_TYPES = [
    "BigInteger",
    "Boolean",
    "Column",
    "Date",
    "DateTime",
    "Double",
    "Float",
    "Integer",
    "Numeric",
    "SmallInteger",
    "String",
    "Time",
]

_PRIVACY_LEVELS = set(PrivacyLevel.__members__)
_TIME_GRAINS = set(TimeGrain.__members__)
# Ontology-mapping helpers semantido exposes and the generated module imports.
_CONCEPT_HELPERS = {
    "exact_match",
    "close_match",
    "narrow_match",
    "broad_match",
    "related_match",
}

_IDENT_SUB_RE = re.compile(r"[^0-9a-zA-Z_]")


def _safe_identifier(name: str) -> str:
    """Return a valid, non-keyword Python identifier derived from ``name``."""
    ident = _IDENT_SUB_RE.sub("_", name)
    if not ident or ident[0].isdigit():
        ident = "_" + ident
    if keyword.iskeyword(ident) or keyword.issoftkeyword(ident):
        ident = ident + "_"
    return ident


def _class_name(dataset_name: str, used: set) -> str:
    """Derive a collision-free class name, preserving the existing casing.

    ``.capitalize()`` would lowercase the tail (``LEI_data`` -> ``LeiData``)
    and could collapse ``a_b`` and ``A_b`` into one name; this keeps the
    original casing and disambiguates any remaining clash with a suffix.
    """
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", dataset_name) if p]
    base = "".join(p[:1].upper() + p[1:] for p in parts) or "Table"
    if base[0].isdigit():
        base = "_" + base
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _parse_source(source: str) -> tuple[str, Optional[str]]:
    """Split an Ossie ``source`` (``[database.]schema.table``) into (table, schema)."""
    parts = source.split(".")
    table = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else None
    return table, schema


def _is_plain_column_ref(field: OssieField) -> bool:
    """True when the field's ANSI expression is just a reference to its column.

    semantido models a field as a bare SQLAlchemy ``Column``; it cannot carry a
    computed expression. A field whose expression is anything other than its own
    (optionally qualified) column name loses that logic, and a field with no
    ANSI_SQL dialect at all cannot be represented portably either.
    """
    ansi = next(
        (
            d.expression
            for d in field.expression.dialects
            if d.dialect == OssieDialect.ANSI_SQL
        ),
        None,
    )
    if ansi is None:
        return False
    token = ansi.strip().split(".")[-1].strip().strip('"').strip("`")
    return token == field.name


def _vendor_payload(entity) -> dict:
    for ext in entity.custom_extensions or []:
        if ext.vendor_name == VENDOR_NAME:
            try:
                return json.loads(ext.data)
            except (TypeError, ValueError):
                return {}
    return {}


def _sqlalchemy_type(field: OssieField, issues: List[ConverterIssue]) -> str:
    # Prefer the exact SQL type captured in the SEMANTIDO extension, so a
    # semantido -> Ossie -> semantido round-trip keeps the original type.
    data_type = _vendor_payload(field).get("data_type", "")
    base = data_type.split("(")[0].strip().upper()
    if base in _TYPE_MAP:
        return _TYPE_MAP[base]
    # No usable vendor type: fall back to the portable Ossie datatype so a
    # document from another tool still produces a typed column.
    if field.datatype is not None:
        if base:
            # A vendor type was present but unmapped; record the gap even
            # though the portable datatype gives us a usable column.
            issues.append(
                ConverterIssue(
                    ConverterIssueType.UNMAPPED_DATA_TYPE, f"{field.name}:{base}"
                )
            )
        return _OSSIE_TO_SA[field.datatype]
    # Neither a mappable vendor type nor a portable datatype is available.
    issues.append(
        ConverterIssue(
            ConverterIssueType.UNMAPPED_DATA_TYPE,
            f"{field.name}:{base}" if base else field.name,
        )
    )
    return "String"


def _ai(entity):
    ctx = entity.ai_context
    if ctx is None or isinstance(ctx, str):
        return None
    return ctx


def _emit_dataset(
    dataset: OssieDataset,
    issues: List[ConverterIssue],
    class_names: set,
    single_fks: dict,
    composite_fks: list,
) -> str:
    payload = _vendor_payload(dataset)
    ctx = _ai(dataset)

    decorator_kwargs = []
    if dataset.description:
        decorator_kwargs.append(f"    description={dataset.description!r},")
    # Prefer the vendor payload's authored split (v0.5.2 converter);
    # fall back to ai_context.instructions for documents produced by
    # other tools or older converter versions.
    if payload.get("business_context"):
        decorator_kwargs.append(
            f"    business_context={payload['business_context']!r},"
        )
    elif ctx and ctx.instructions:
        decorator_kwargs.append(f"    business_context={ctx.instructions!r},")
    if payload.get("application_context"):
        decorator_kwargs.append(
            f"    application_context={payload['application_context']!r},"
        )
    if ctx and ctx.synonyms:
        decorator_kwargs.append(f"    synonyms={list(ctx.synonyms)!r},")
    if payload.get("concept"):  # semantido v0.4.0+
        decorator_kwargs.append(f"    concept={payload['concept']!r},")
    if payload.get("sql_filters"):
        decorator_kwargs.append(f"    sql_filters={payload['sql_filters']!r},")
    if payload.get("time_dimension"):
        decorator_kwargs.append(f"    time_dimension={payload['time_dimension']!r},")

    table_name, schema = _parse_source(dataset.source)
    lines = ["@semantic_table("] + decorator_kwargs + [")"]
    lines.append(f"class {_class_name(dataset.name, class_names)}(SemanticDeclarativeBase):")
    lines.append(f"    __tablename__ = {table_name!r}")
    lines.append("")

    primary_keys = set(dataset.primary_key or [])
    field_list = list(dataset.fields or [])
    if not primary_keys:
        # SQLAlchemy refuses to map a class with no primary key, so the
        # generated module would fail to import. Synthesize one on the first
        # column (nothing to fall back to if there are no columns) and record it.
        if field_list:
            primary_keys = {field_list[0].name}
            issues.append(
                ConverterIssue(
                    ConverterIssueType.MISSING_PRIMARY_KEY,
                    f"{dataset.name}:{field_list[0].name}",
                )
            )
        else:
            issues.append(
                ConverterIssue(ConverterIssueType.MISSING_PRIMARY_KEY, dataset.name)
            )

    for field in field_list:
        field_payload = _vendor_payload(field)
        field_ctx = _ai(field)
        sa_type = _sqlalchemy_type(field, issues)

        attr = _safe_identifier(field.name)
        if attr != field.name:
            issues.append(
                ConverterIssue(
                    ConverterIssueType.IDENTIFIER_SANITIZED,
                    f"{dataset.name}.{field.name} -> {attr}",
                )
            )
        # Carry the real column name whenever the attribute had to change.
        name_arg = f"{field.name!r}, " if attr != field.name else ""

        if not _is_plain_column_ref(field):
            issues.append(
                ConverterIssue(
                    ConverterIssueType.NON_ANSI_EXPRESSION_DROPPED,
                    f"{dataset.name}.{field.name}",
                )
            )

        pk = ", primary_key=True" if field.name in primary_keys else ""
        # An authored FK in the extension wins; otherwise take a single-column
        # relationship targeting this column. Composite FKs go in __table_args__.
        reference = field_payload.get("references") or single_fks.get(field.name)
        fk = f", ForeignKey({reference!r})" if reference else ""
        lines.append(f"    {attr} = Column({name_arg}{sa_type}{fk}{pk})")

        if field.description:
            lines.append(f"    {attr}_description = {field.description!r}")
        if field_ctx and field_ctx.synonyms:
            lines.append(f"    {attr}_synonyms = {list(field_ctx.synonyms)!r}")
        if field_payload.get("concept"):  # semantido v0.4.0+
            lines.append(f"    {attr}_concept = {field_payload['concept']!r}")
        if field_payload.get("application_rules"):
            lines.append(
                f"    {attr}_application_rules = "
                f"{list(field_payload['application_rules'])!r}"
            )
        if field_payload.get("privacy_level"):
            level = field_payload["privacy_level"].upper()
            if level in _PRIVACY_LEVELS:
                lines.append(f"    {attr}_privacy_level = PrivacyLevel.{level}")
            else:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.UNKNOWN_PRIVACY_LEVEL,
                        f"{dataset.name}.{field.name}:{level}",
                    )
                )
        if field_ctx and field_ctx.examples:
            lines.append(
                f"    {attr}_sample_values = {[str(e) for e in field_ctx.examples]!r}"
            )
        if field_payload.get("time_grain"):
            grain = field_payload["time_grain"].upper()
            if grain in _TIME_GRAINS:
                lines.append(f"    {attr}_time_grain = TimeGrain.{grain}")
            else:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.UNKNOWN_TIME_GRAIN,
                        f"{dataset.name}.{field.name}:{grain}",
                    )
                )

    table_args = []
    for key in payload.get("unique_keys", []):  # semantido v0.5.0+
        table_args.append("UniqueConstraint(" + ", ".join(repr(c) for c in key) + ")")
    for from_cols, targets in composite_fks:
        cols = ", ".join(repr(c) for c in from_cols)
        refs = ", ".join(repr(t) for t in targets)
        table_args.append(f"ForeignKeyConstraint([{cols}], [{refs}])")
    if schema:
        table_args.append(f'{{"schema": {schema!r}}}')
    if table_args:
        lines.append("")
        lines.append(f"    __table_args__ = ({', '.join(table_args)},)")
    return "\n".join(lines)


_SYMMETRIC_RELATIONS = {"same_as", "related", "distinct_from"}
_ASYMMETRIC_RELATIONS = {"broader", "narrower"}


def _emit_registry(registry: dict, issues: List[ConverterIssue]) -> str:
    """Generate ``build_registry()`` authoring code from a serialized registry.

    Emission rules follow semantido's authoring semantics:

    - Symmetric relations (``same_as`` / ``related`` / ``distinct_from``)
      auto-reciprocate and appear on both concepts in ``to_dict()``, so
      each edge is emitted exactly once — on whichever concept is
      declared later, when its partner handle already exists.
    - Asymmetric relations (``broader`` / ``narrower``) are recorded on
      one side only; declaration order is chosen so targets precede
      sources. Unresolvable orderings (cycles) drop the relation with a
      recorded issue rather than generating code that cannot run.
    - A relation whose kind is neither symmetric nor asymmetric, or whose
      target is unknown or is the concept itself (a self-relation cannot
      reference the handle being assigned on the same line), is dropped
      with a recorded issue rather than emitted as a bogus argument.
    - ``definition_checksum`` is computed by semantido and never emitted.
    """
    concepts = registry.get("concepts", {})
    sources = registry.get("sources", {})
    namespace = registry.get("namespace")

    # Order: satisfy asymmetric-relation dependencies, alphabetical tiebreak.
    remaining = dict(sorted(concepts.items()))
    ordered: List[str] = []
    emitted = set()
    while remaining:
        progressed = False
        for cid, spec in list(remaining.items()):
            deps = {
                r["concept"]
                for r in spec.get("relations", [])
                if r.get("relation") in _ASYMMETRIC_RELATIONS
                and r.get("concept") in concepts
            }
            if deps <= emitted:
                ordered.append(cid)
                emitted.add(cid)
                del remaining[cid]
                progressed = True
        if not progressed:  # cycle: emit rest, relations to later ids drop
            ordered.extend(remaining)
            emitted.update(remaining)
            remaining.clear()

    handles = {cid: f"c_{i}" for i, cid in enumerate(ordered)}
    lines = ["def build_registry() -> ConceptRegistry:"]
    ns = f"namespace={namespace!r}" if namespace else ""
    lines.append(f"    registry = ConceptRegistry({ns})")
    for name, src in sorted(sources.items()):
        kwargs = [
            f"name={name!r}",
            f"namespace={src.get('namespace')!r}",
            f"version={src.get('version')!r}",
        ]
        for opt in ("location", "profile"):
            if src.get(opt):
                kwargs.append(f"{opt}={src[opt]!r}")
        lines.append(
            "    registry.add_source(OntologySource(" + ", ".join(kwargs) + "))"
        )

    declared = set()
    for cid in ordered:
        spec = concepts[cid]
        args = [repr(cid), repr(spec.get("definition", ""))]
        if spec.get("label") and spec["label"] != cid:
            args.append(f"label={spec['label']!r}")
        if spec.get("synonyms"):
            args.append(f"synonyms={list(spec['synonyms'])!r}")
        if spec.get("grain"):  # semantido v0.5.0+
            args.append(f"grain={spec['grain']!r}")

        rel_kwargs: dict = {}
        for rel in spec.get("relations", []):
            kind, target = rel.get("relation"), rel.get("concept")
            if kind not in _SYMMETRIC_RELATIONS and kind not in _ASYMMETRIC_RELATIONS:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.REGISTRY_RELATION_UNKNOWN_KIND,
                        f"{cid}:{kind}:{target}",
                    )
                )
                continue
            if target not in concepts or target == cid:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.REGISTRY_RELATION_UNRESOLVED,
                        f"{cid}:{kind}:{target}",
                    )
                )
                continue
            if kind in _SYMMETRIC_RELATIONS and target not in declared:
                continue  # the reciprocal entry on the later concept emits it
            if kind in _ASYMMETRIC_RELATIONS and target not in declared:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.REGISTRY_RELATION_UNRESOLVED,
                        f"{cid}:{kind}:{target}",
                    )
                )
                continue
            rel_kwargs.setdefault(kind, []).append(handles[target])
        for kind, targets in rel_kwargs.items():
            value = targets[0] if len(targets) == 1 else "[" + ", ".join(targets) + "]"
            args.append(f"{kind}={value}")

        mappings = []
        for m in spec.get("mappings", []):
            helper = m.get("relation", "exact_match")
            if helper not in _CONCEPT_HELPERS:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.REGISTRY_MAPPING_UNKNOWN, f"{cid}:{helper}"
                    )
                )
                continue
            mappings.append(f"{helper}({m.get('source')!r}, {m.get('target')!r})")
        if len(mappings) == 1:
            args.append(f"external={mappings[0]}")
        elif mappings:
            args.append("external=[" + ", ".join(mappings) + "]")

        lines.append(f"    {handles[cid]} = registry.concept(" + ", ".join(args) + ")")
        declared.add(cid)
    lines.append("    return registry")
    lines.append("")
    lines.append("")
    lines.append("CONCEPT_REGISTRY = build_registry()")
    return "\n".join(lines)


def _relationship_fks(document: OssieDocument, issues: List[ConverterIssue]):
    """Index the document's relationships by the dataset that owns the FK.

    Returns ``(single_by_dataset, composite_by_dataset)``: single-column joins
    become inline ``ForeignKey`` targets keyed by column; composite joins become
    ``ForeignKeyConstraint`` argument tuples. Honoring these keeps joins from
    being silently dropped for a document that carries no SEMANTIDO extension.
    """
    table_by_dataset = {ds.name: _parse_source(ds.source)[0] for ds in document.datasets}
    single: dict = defaultdict(dict)
    composite: dict = defaultdict(list)
    for rel in document.relationships or []:
        if rel.from_dataset not in table_by_dataset:
            # No dataset to attach the FK to; record rather than discard.
            issues.append(
                ConverterIssue(ConverterIssueType.RELATIONSHIP_DROPPED, rel.name)
            )
            continue
        to_table = table_by_dataset.get(rel.to, rel.to)
        targets = [f"{to_table}.{col}" for col in rel.to_columns]
        if len(rel.from_columns) == 1 and len(targets) == 1:
            single[rel.from_dataset][rel.from_columns[0]] = targets[0]
        else:
            composite[rel.from_dataset].append((list(rel.from_columns), targets))
    return single, composite


def ossie_to_semantido_source(document: OssieDocument) -> ConverterResult[str]:
    """Generate semantido model source code from an Ossie document."""
    issues: List[ConverterIssue] = []
    blocks = []

    # OssieDocument is itself a single semantic model: datasets, metrics,
    # relationships and custom_extensions are read directly from the document
    # root. semantido has no metric concept, so metrics at either scope are
    # dropped with a recorded issue (model-scoped unqualified; dataset-scoped
    # qualified by dataset name).
    for metric in document.metrics or []:
        issues.append(ConverterIssue(ConverterIssueType.METRIC_DROPPED, metric.name))

    single_fks, composite_fks = _relationship_fks(document, issues)

    model_payload = _vendor_payload(document)
    registry_block = None
    if model_payload.get("concept_registry"):
        registry_block = _emit_registry(model_payload["concept_registry"], issues)

    class_names: set = set()
    for dataset in document.datasets:
        for metric in dataset.metrics or []:
            issues.append(
                ConverterIssue(
                    ConverterIssueType.METRIC_DROPPED, f"{dataset.name}.{metric.name}"
                )
            )
        blocks.append(
            _emit_dataset(
                dataset,
                issues,
                class_names,
                single_fks.get(dataset.name, {}),
                composite_fks.get(dataset.name, []),
            )
        )
    if registry_block is not None:
        blocks.insert(0, registry_block)

    todo_block = ""
    if issues:
        todo_lines = "\n".join(
            f"#   {i.issue_type.value}: {i.element_name}" for i in issues
        )
        todo_block = (
            "# TODO(ossie-semantido): the following Ossie constructs have no\n"
            "# semantido equivalent yet and were not converted:\n" + todo_lines + "\n\n"
        )

    body = "\n\n\n".join(blocks) + "\n"
    sa_imports = list(_SA_BASE_TYPES)
    if "ForeignKeyConstraint(" in body:
        sa_imports.append("ForeignKeyConstraint")
    if "ForeignKey(" in body:
        sa_imports.append("ForeignKey")
    if "UniqueConstraint(" in body:
        sa_imports.append("UniqueConstraint")
    header_lines = [
        '"""semantido models generated by ossie-semantido. Review before use."""',
        "",
        "from sqlalchemy import " + ", ".join(sorted(sa_imports)),
        "",
        "from semantido import SemanticDeclarativeBase, semantic_table",
        "from semantido.generators.semantic_layer import PrivacyLevel, TimeGrain",
    ]
    if registry_block is not None:
        header_lines.append(
            "from semantido.concepts import ("
            "ConceptRegistry, OntologySource, "
            "exact_match, close_match, narrow_match, broad_match, related_match)"
        )
    header = "\n".join(header_lines) + "\n\n\n"
    source = header + todo_block + body
    return ConverterResult(output=source, issues=issues)
