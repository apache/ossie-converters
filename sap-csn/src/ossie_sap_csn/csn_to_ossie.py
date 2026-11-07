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

"""Convert a SAP CSN document (Core Schema Notation, JSON) into an Ossie semantic model.

Mapping:

* an ``entity`` (table or view) becomes a dataset, and its elements become fields;
* a ``key`` element becomes part of the primary key;
* an association or composition with a resolvable target becomes a relationship,
  and a managed association's generated foreign-key columns become fields;
* an element annotated ``@Aggregation.default`` (``SUM``, ``MIN``, ``MAX``, ``AVG``,
  ``COUNT``, ``COUNT_DISTINCT``) becomes a metric, model-scoped unless the element
  is marked ``@Ossie.datasetMetric``;
* ``@EndUserText.label`` and ``doc`` become the label and description.

Whatever has no Ossie equivalent travels in ``custom_extensions`` under the
``SAP_CSN`` vendor, so the document can be exported back without loss.
"""

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ossie import (
    OssieCustomExtension,
    OssieDataset,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
    OssieRelationship,
)
from pydantic import ValidationError
from sqlglot import exp

from ossie_sap_csn import _common as c
from ossie_sap_csn.converter_issues import (
    ConverterError,
    ConverterIssue,
    ConverterIssueType,
    ConverterResult,
)
from ossie_sap_csn.cxn import UnsupportedExpression, cxn_to_sql, parse_sql, quote_identifier
from ossie_sap_csn.datatypes import ASSOCIATION_TYPES, builtin_type, ossie_datatype

# Entity keys that map to Ossie properties; every other key travels verbatim.
_MAPPED_ENTITY_KEYS = frozenset({"kind", "elements", "doc"})
# Element keys that map to Ossie properties or are rebuilt on export.
_MAPPED_ELEMENT_KEYS = frozenset({"type", "key", "value", "doc", "target", "on", "keys"})
# Definition kinds worth reporting when they are set aside (structural kinds are not).
_REPORTED_KINDS = frozenset({"aspect", "action", "function", "event", "annotation"})
_STRUCTURAL_KINDS = frozenset({"context", "service", "type"})


def _extensions(own: dict[str, Any], obj: dict[str, Any]) -> Optional[list[Any]]:
    extensions = []
    if own:
        extensions.append(c.vendor_extension(own))
    for foreign in obj.get(c.OSSIE_EXTENSIONS) or []:
        extensions.append(OssieCustomExtension.model_validate(foreign))
    return extensions or None


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> tuple[Optional[str], Optional[str]]:
    """First string-valued annotation among ``keys``, as ``(key, value)``."""
    for key in keys:
        if isinstance(obj.get(key), str):
            return key, obj[key]
    return None, None


@contextmanager
def _representable(where: str) -> Iterator[None]:
    """Report a CSN value the Ossie models reject (a malformed ``@Ossie.*`` payload, say)."""
    try:
        yield
    except ValidationError as exc:
        raise ConverterError(f"{where} cannot be represented in Ossie: {exc}") from exc


def _qualify(tree: exp.Expression, table: str) -> str:
    """Prefix every unqualified column of ``tree`` with ``table``."""
    tree = tree.copy()
    for column in tree.find_all(exp.Column):
        if not column.table:
            column.set("table", exp.to_identifier(table, quoted=quote_identifier(table) != table))
    return tree.sql()


def _measure_target(sql: str, dialect: OssieDialect, dataset: str, dataset_scoped: bool) -> tuple[bool, str]:
    """What a derived measure aggregates, and whether its metric is dataset-scoped.

    A dataset-scoped metric names its own dataset's fields unqualified, so one whose
    expression reaches another dataset is model-scoped instead, every column qualified.
    """
    if dialect is not OssieDialect.ANSI_SQL:
        raise UnsupportedExpression(f"its expression is {dialect.value}, not ANSI_SQL")
    tree = parse_sql(sql)
    if dataset_scoped and not any(column.table for column in tree.find_all(exp.Column)):
        return True, sql
    return False, _qualify(tree, dataset)


class _Entities:
    """Entity names and the datasets they become."""

    def __init__(self, definitions: dict[str, Any]) -> None:
        self.csn_names = [n for n, d in definitions.items() if isinstance(d, dict) and d.get("kind") == "entity"]
        shorts = [n.rsplit(".", 1)[-1] for n in self.csn_names]
        unique = len(set(shorts)) == len(shorts)
        self.dataset_name: dict[str, str] = {}
        used: set[str] = set()
        for n, s in zip(self.csn_names, shorts):
            base = s if unique else n.replace(".", "_")
            candidate, suffix = base, 1
            while candidate in used:  # flattening can collide too (`foo.bar` and `foo_bar`)
                suffix += 1
                candidate = f"{base}_{suffix}"
            used.add(candidate)
            self.dataset_name[n] = candidate

    def __contains__(self, csn_name: object) -> bool:
        return csn_name in self.dataset_name


def _key_elements(definitions: dict[str, Any], entity: str) -> list[tuple[str, Optional[str]]]:
    """Names and built-in types of an entity's non-association key elements."""
    result = []
    for name, element in (definitions[entity].get("elements") or {}).items():
        if isinstance(element, dict) and element.get("key"):
            builtin = builtin_type(element.get("type"), definitions)
            if builtin not in ASSOCIATION_TYPES:
                result.append((name, builtin))
    return result


def _flatten_and(tokens: list[Any]) -> list[Any]:
    """Unwrap single nested ``xpr`` groups so an ``on`` condition reads as one token list."""
    while len(tokens) == 1 and isinstance(tokens[0], dict) and isinstance(tokens[0].get("xpr"), list):
        tokens = tokens[0]["xpr"]
    return tokens


def _column_path(side: Any) -> list[str]:
    """The column path one side of an ``on`` equality names, without ``$self``."""
    ref = side.get("ref") if isinstance(side, dict) else None
    if not (isinstance(ref, list) and ref and all(isinstance(s, str) for s in ref)):
        raise UnsupportedExpression("the on-condition compares something other than columns")
    if ref[0] in ("$self", "$projection"):
        if len(ref) == 1:
            raise UnsupportedExpression("a $self backlink has no column pairs of its own")
        ref = ref[1:]
    if any(s.startswith("$") for s in ref):
        raise UnsupportedExpression(f"{'.'.join(ref)!r} is not a column")
    return ref


def _parse_on(assoc: str, on: Any) -> list[tuple[str, str]]:
    """Return ``(from_column, to_column)`` pairs for an ``on`` condition of equalities.

    Raises :class:`UnsupportedExpression` for anything other than an AND of
    ``assoc.col = col`` terms.
    """
    if not isinstance(on, list):
        raise UnsupportedExpression("the on-condition is not a token list")
    tokens = _flatten_and(on)
    terms: list[list[Any]] = [[]]
    for token in tokens:
        if isinstance(token, str) and token.lower() == "and":
            terms.append([])
        else:
            terms[-1].append(token)
    pairs = []
    for term in terms:
        term = _flatten_and(term)
        if len(term) != 3 or term[1] != "=":
            raise UnsupportedExpression("the on-condition is not a conjunction of equalities")
        refs = [_column_path(term[0]), _column_path(term[2])]
        target_side = [r for r in refs if len(r) == 2 and r[0] == assoc]
        local_side = [r for r in refs if len(r) == 1]
        if len(target_side) != 1 or len(local_side) != 1:
            raise UnsupportedExpression("each equality must pair one association column with one local column")
        pairs.append((local_side[0][0], target_side[0][1]))
    return pairs


class CsnToOssieConverter:
    """Converts a parsed CSN document into an :class:`OssieDocument`."""

    def convert(self, csn: dict[str, Any], name: Optional[str] = None) -> ConverterResult[OssieDocument]:
        if not isinstance(csn, dict) or not isinstance(csn.get("definitions"), dict):
            raise ConverterError("input is not a CSN document: it has no 'definitions' object")
        definitions: dict[str, Any] = csn["definitions"]
        issues: list[ConverterIssue] = []

        entities = _Entities(definitions)
        if not entities.csn_names:
            raise ConverterError("the CSN document defines no entities; an Ossie model needs at least one dataset")
        for csn_name in entities.csn_names:
            if not isinstance(definitions[csn_name].get("elements", {}), dict):
                raise ConverterError(f"entity {csn_name!r}: 'elements' is not an object")

        meta = csn.get("meta") if isinstance(csn.get("meta"), dict) else {}
        ossie_meta = meta.get(c.META_KEY) if isinstance(meta.get(c.META_KEY), dict) else {}

        dataset_args: dict[str, dict[str, Any]] = {}
        relationships: list[OssieRelationship] = []
        metrics: list[OssieMetric] = []
        measure_candidates: list[dict[str, Any]] = []

        for csn_name in entities.csn_names:
            with _representable(f"entity {csn_name!r}"):
                args, entity_relationships, entity_measures = self._entity(
                    csn_name, definitions[csn_name], definitions, entities, issues
                )
            dataset_args[csn_name] = args
            relationships.extend(entity_relationships)
            measure_candidates.extend(entity_measures)

        with _representable("a measure"):
            model_metrics, dataset_metrics = self._metrics(measure_candidates)
        datasets: list[OssieDataset] = []
        for csn_name, args in dataset_args.items():
            local = dataset_metrics.get(args["name"], []) + args.pop("carried_metrics")
            with _representable(f"entity {csn_name!r}"):
                datasets.append(OssieDataset(**args, metrics=local or None))

        # Relationship names are global; prefix the ones two entities both use.
        counts: dict[str, int] = {}
        for r in relationships:
            counts[r.name] = counts.get(r.name, 0) + 1
        if any(n > 1 for n in counts.values()):
            relationships = [
                r.model_copy(update={"name": f"{r.from_dataset}_{r.name}"}) if counts[r.name] > 1 else r
                for r in relationships
            ]

        metrics.extend(model_metrics)
        raw_metrics = ossie_meta.get("metrics") or []
        if not isinstance(raw_metrics, list):
            raise ConverterError("meta.ossie.metrics is not a list")
        with _representable("meta.ossie.metrics"):
            metrics.extend(OssieMetric.model_validate(raw) for raw in raw_metrics)

        other = {n: d for n, d in definitions.items() if n not in entities}
        for def_name, definition in other.items():
            kind = definition.get("kind") if isinstance(definition, dict) else None
            if kind in _REPORTED_KINDS:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.UNSUPPORTED_DEFINITION_KIND,
                        def_name,
                        f"CSN definition kind {kind!r} has no equivalent in an Ossie semantic model; "
                        "preserved verbatim in custom_extensions only.",
                    )
                )

        model_data: dict[str, Any] = {}
        for key, value in csn.items():
            if key == "definitions":
                continue
            if key == "meta":
                if isinstance(value, dict):
                    # Export writes its own creator stamp again, so a second pass stays the same.
                    value = {
                        k: v for k, v in value.items() if k != c.META_KEY and not (k == "creator" and v == c.CREATOR)
                    }
                if not value:
                    continue
            model_data.setdefault("csn", {})[key] = value
        if other:
            model_data["other_definitions"] = other

        model_info = ossie_meta.get("model") if isinstance(ossie_meta.get("model"), dict) else {}
        with _representable("meta.ossie.model"):
            document = OssieDocument(
                name=name or model_info.get("name") or "sap_csn_model",
                description=model_info.get("description"),
                ai_context=model_info.get("ai_context"),
                datasets=datasets,
                relationships=relationships or None,
                metrics=metrics or None,
                custom_extensions=_extensions(model_data, {c.OSSIE_EXTENSIONS: model_info.get("custom_extensions")}),
            )
        return ConverterResult(output=document, issues=issues)

    # ----------------------------------------------------------------------------------

    def _entity(
        self,
        csn_name: str,
        entity: dict[str, Any],
        definitions: dict[str, Any],
        entities: _Entities,
        issues: list[ConverterIssue],
    ) -> tuple[dict[str, Any], list[OssieRelationship], list[dict[str, Any]]]:
        dataset_name = entities.dataset_name[csn_name]
        elements: dict[str, Any] = entity.get("elements") or {}

        def resolve_path(path: list[str]) -> str:
            head = elements.get(path[0])
            target = head.get("target") if isinstance(head, dict) else None
            if len(path) == 2 and target in entities:
                return f"{quote_identifier(entities.dataset_name[target])}.{quote_identifier(path[1])}"
            raise UnsupportedExpression(f"path expression {'.'.join(path)!r} cannot be resolved")

        fields: list[OssieField] = []
        field_names: set[str] = set()
        primary_key: list[str] = []
        unmodeled: dict[str, Any] = {}
        measures: list[dict[str, Any]] = []
        assoc_elements: list[tuple[str, dict[str, Any]]] = []

        for el_name, element in elements.items():
            if not isinstance(element, dict):
                continue
            builtin = builtin_type(element.get("type"), definitions)
            if builtin in ASSOCIATION_TYPES:
                assoc_elements.append((el_name, element))
                continue
            if element.get("virtual"):
                unmodeled[el_name] = element
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.ELEMENT_NOT_MODELED,
                        f"{dataset_name}.{el_name}",
                        "Virtual element is not persisted and so is not a column; preserved in custom_extensions only.",
                    )
                )
                continue
            named_type = element.get("type") if isinstance(element.get("type"), str) else None
            if builtin is None and ("elements" in element or "items" in element or named_type in definitions):
                unmodeled[el_name] = element
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.ELEMENT_NOT_MODELED,
                        f"{dataset_name}.{el_name}",
                        "Structured or array-typed element has no Ossie field equivalent; "
                        "preserved in custom_extensions only.",
                    )
                )
                continue

            where = f"{dataset_name}.{el_name}"
            sql_override = element.get(c.OSSIE_SQL)
            has_override = isinstance(sql_override, dict) and "dialect" in sql_override and "expression" in sql_override
            if has_override:
                expression_sql: Optional[str] = sql_override["expression"]
                try:
                    dialect = OssieDialect(sql_override["dialect"])
                except ValueError as exc:
                    raise ConverterError(f"{where}: @Ossie.sql names an unknown dialect {sql_override['dialect']!r}") from exc
                if not isinstance(expression_sql, str):
                    raise ConverterError(f"{where}: @Ossie.sql has an expression that is not a string")
            elif "value" in element:
                try:
                    expression_sql = cxn_to_sql(element["value"], resolve_path)
                except UnsupportedExpression as exc:
                    unmodeled[el_name] = element
                    issues.append(
                        ConverterIssue(
                            ConverterIssueType.EXPRESSION_NOT_TRANSLATABLE,
                            f"{dataset_name}.{el_name}",
                            f"Calculated element has no SQL rendering ({exc}); "
                            "preserved in custom_extensions only.",
                        )
                    )
                    continue
                dialect = OssieDialect.ANSI_SQL
            else:
                expression_sql = quote_identifier(el_name)
                dialect = OssieDialect.ANSI_SQL

            aggregation = c.enum_value(element.get(c.AGGREGATION_KEY))
            is_measure = aggregation in c.AGGREGATIONS
            # A derived measure is a metric only, over the element's expression. Without
            # one (no `value`, no @Ossie.sql) the element is a stored column, so a field.
            derived = is_measure and bool(element.get(c.OSSIE_METRIC)) and ("value" in element or has_override)
            dataset_scoped = False
            if is_measure:
                try:
                    if derived:
                        dataset_scoped, target = _measure_target(
                            expression_sql, dialect, dataset_name, bool(element.get(c.OSSIE_DATASET_METRIC))
                        )
                    else:
                        target = f"{quote_identifier(dataset_name)}.{quote_identifier(el_name)}"
                except UnsupportedExpression as exc:
                    # Never fall back to aggregating the element itself: it is not a field.
                    unmodeled[el_name] = element
                    issues.append(
                        ConverterIssue(
                            ConverterIssueType.METRIC_NOT_MODELED,
                            where,
                            f"Derived measure has no metric expression ({exc}); preserved in custom_extensions only.",
                        )
                    )
                    continue
                metric_sql = f"COUNT(DISTINCT {target})" if aggregation == "COUNT_DISTINCT" else f"{aggregation}({target})"

            datatype = ossie_datatype(builtin)
            if datatype is None:
                datatype = "Opaque"
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.OPAQUE_DATATYPE,
                        where,
                        f"CSN type {element.get('type')!r} has no portable Ossie datatype.",
                    )
                )

            consumed: set[str] = set()
            label_key, label = _first(element, c.LABEL_KEYS)
            if label_key:
                consumed.add(label_key)
            desc_key, description = ("doc", element["doc"]) if isinstance(element.get("doc"), str) else (None, None)
            if description is None:
                desc_key, description = _first(element, c.QUICKINFO_KEYS)
                if desc_key:
                    consumed.add(desc_key)
            if is_measure:
                consumed.add(c.AGGREGATION_KEY)

            own: dict[str, Any] = {"type": element.get("type")}
            if label_key and label_key != c.LABEL_KEYS[0]:
                own["label_key"] = label_key
            if desc_key and desc_key != "doc":
                own["description_key"] = desc_key
            annotations = c.split_annotations(element, consumed)
            if annotations:
                own["annotations"] = annotations
            native = {k: v for k, v in element.items() if not k.startswith("@") and k not in _MAPPED_ELEMENT_KEYS}
            if native:
                own["native"] = native
            if derived and label:
                own["label"] = label
            if "value" in element and not has_override:
                # SQL cannot tell some CXN apart (cds.Int32 and cds.Integer casts are both
                # INT), so the original travels too; export reuses it while the expression
                # it rendered as is unchanged.
                own["value"] = element["value"]
                own["value_sql"] = metric_sql if derived else expression_sql

            if is_measure:
                measures.append(
                    {
                        "dataset": dataset_name,
                        "entity": csn_name,
                        "element": el_name,
                        "aggregation": aggregation,
                        "aggregation_raw": element[c.AGGREGATION_KEY],
                        "derived": derived,
                        "dataset_scoped": dataset_scoped,
                        "sql": metric_sql,
                        "datatype": datatype,
                        "builtin": builtin,
                        "description": description,
                        "own": own,
                        "element_obj": element,
                    }
                )
                if derived:
                    continue

            is_time = datatype in ("Date", "Time", "DateTime", "DateTimeTz") or any(
                k.startswith(c.TEMPORAL_PREFIXES) for k in element
            )
            if c.OSSIE_IS_TIME in element:
                is_time = bool(element[c.OSSIE_IS_TIME])

            fields.append(
                OssieField(
                    name=el_name,
                    expression=OssieExpression(dialects=[OssieDialectExpression(dialect=dialect, expression=expression_sql)]),
                    dimension=None if is_measure else OssieDimension(is_time=is_time),
                    label=label,
                    description=description,
                    datatype=datatype,
                    ai_context=element.get(c.OSSIE_AI_CONTEXT),
                    custom_extensions=_extensions(own, element),
                )
            )
            field_names.add(el_name)
            if element.get("key"):
                primary_key.append(el_name)

        relationships: list[OssieRelationship] = []
        for el_name, element in assoc_elements:
            self._association(
                dataset_name, csn_name, el_name, element, definitions, entities, fields, field_names,
                primary_key, relationships, unmodeled, issues,
            )

        # Keys the entity declares through `key` on an association were added above;
        # keep primary-key order as declared on the entity.
        declared_order = [n for n in elements if n in primary_key] + [n for n in primary_key if n not in elements]
        primary_key = list(dict.fromkeys(declared_order))

        unique_keys: list[list[str]] = []
        unique_names: list[str] = []
        consumed_unique: set[str] = set()
        for key, value in entity.items():
            if not key.startswith(c.UNIQUE_PREFIX):
                continue
            columns = [i["="] if isinstance(i, dict) and "=" in i else i for i in value] if isinstance(value, list) else []
            if columns and all(isinstance(i, str) and i in field_names for i in columns):
                unique_keys.append(columns)
                unique_names.append(key[len(c.UNIQUE_PREFIX):])
                consumed_unique.add(key)
            else:
                issues.append(
                    ConverterIssue(
                        ConverterIssueType.UNIQUE_KEY_COLUMN_UNRESOLVED,
                        f"{dataset_name}.{key[len(c.UNIQUE_PREFIX):]}",
                        "Unique constraint names something other than modeled columns; "
                        "preserved in custom_extensions only.",
                    )
                )

        # A key that named a column CSN has no element for travels whole; it is the
        # authoritative list, and the constraints derived from it above are redundant.
        if isinstance(entity.get(c.OSSIE_PRIMARY_KEY), list):
            primary_key = list(entity[c.OSSIE_PRIMARY_KEY])
        if isinstance(entity.get(c.OSSIE_UNIQUE_KEYS), list):
            unique_keys = [list(k) if isinstance(k, list) else k for k in entity[c.OSSIE_UNIQUE_KEYS]]

        consumed_entity: set[str] = set(consumed_unique)
        label_key, label = _first(entity, c.LABEL_KEYS)
        description = entity["doc"] if isinstance(entity.get("doc"), str) else None
        desc_key: Optional[str] = "doc" if description is not None else None
        if description is None:
            desc_key, description = _first(entity, c.QUICKINFO_KEYS)
        if description is None and label_key:
            desc_key, description = label_key, label
        if desc_key and desc_key != "doc":
            consumed_entity.add(desc_key)

        own_entity: dict[str, Any] = {"entity": csn_name}
        if desc_key and desc_key != "doc":
            own_entity["description_key"] = desc_key
        if unique_names:
            own_entity["unique_key_names"] = unique_names
        entity_annotations = c.split_annotations(entity, consumed_entity)
        if entity_annotations:
            own_entity["annotations"] = entity_annotations
        native_entity = {k: v for k, v in entity.items() if not k.startswith("@") and k not in _MAPPED_ENTITY_KEYS}
        if native_entity:
            own_entity["native"] = native_entity
        if unmodeled:
            own_entity["unmodeled_elements"] = unmodeled

        carried = entity.get(c.OSSIE_METRICS) or []
        if not isinstance(carried, list):
            raise ConverterError(f"entity {csn_name!r}: {c.OSSIE_METRICS} is not a list")

        source = entity.get("@cds.persistence.name") if isinstance(entity.get("@cds.persistence.name"), str) else csn_name
        # Built into a dataset once the measures of every entity are known.
        dataset_args = {
            "name": dataset_name,
            "source": source,
            "primary_key": primary_key or None,
            "unique_keys": unique_keys or None,
            "description": description,
            "ai_context": entity.get(c.OSSIE_AI_CONTEXT),
            "fields": fields or None,
            "custom_extensions": _extensions(own_entity, entity),
            "carried_metrics": [OssieMetric.model_validate(raw) for raw in carried],
        }
        return dataset_args, relationships, measures

    def _association(
        self,
        dataset_name: str,
        csn_name: str,
        el_name: str,
        element: dict[str, Any],
        definitions: dict[str, Any],
        entities: _Entities,
        fields: list[OssieField],
        field_names: set[str],
        primary_key: list[str],
        relationships: list[OssieRelationship],
        unmodeled: dict[str, Any],
        issues: list[ConverterIssue],
    ) -> None:
        where = f"{dataset_name}.{el_name}"

        def drop(reason: str) -> None:
            unmodeled[el_name] = element
            issues.append(
                ConverterIssue(
                    ConverterIssueType.RELATIONSHIP_DROPPED,
                    where,
                    f"{reason}; preserved in custom_extensions only.",
                )
            )

        target = element.get("target")
        if not isinstance(target, str) or target not in entities:
            drop(f"Association target {target!r} is not an entity of this document")
            return

        managed = "on" not in element
        generated: list[tuple[str, str, Optional[str]]] = []  # (fk_name, to_column, builtin type)
        if not managed:
            try:
                pairs = _parse_on(el_name, element["on"])
            except UnsupportedExpression as exc:
                drop(f"The association's on-condition cannot be expressed as column pairs ({exc})")
                return
            from_columns = [p[0] for p in pairs]
            to_columns = [p[1] for p in pairs]
        else:
            keys = element.get("keys")
            target_keys = dict(_key_elements(definitions, target))
            if keys is None:
                keys = [{"ref": [k]} for k in target_keys]
            if not keys:
                drop("Managed association has no keys to join on")
                return
            from_columns, to_columns = [], []
            for key in keys:
                ref = key.get("ref") if isinstance(key, dict) else None
                if not ref or not all(isinstance(s, str) for s in ref):
                    drop("Managed association key is not a plain column reference")
                    return
                to_column = "_".join(ref)
                fk = key.get("$generatedFieldName") or f"{el_name}_{key.get('as') or to_column}"
                from_columns.append(fk)
                to_columns.append(to_column)
                generated.append((fk, to_column, target_keys.get(to_column)))

        unresolved = [col for col in from_columns if col not in field_names and col not in {g[0] for g in generated}]
        if unresolved:
            drop(f"Join columns {unresolved} are not modeled columns of {dataset_name}")
            return

        for fk, to_column, builtin in generated:
            if fk in field_names:
                continue
            datatype = ossie_datatype(builtin)
            fields.append(
                OssieField(
                    name=fk,
                    expression=OssieExpression(
                        dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=quote_identifier(fk))]
                    ),
                    dimension=OssieDimension(is_time=False),
                    datatype=datatype,
                    custom_extensions=[
                        c.vendor_extension(
                            {"generated_foreign_key": True, "association": el_name}
                        )
                    ],
                )
            )
            field_names.add(fk)
        if element.get("key"):
            primary_key.extend(from_columns)

        consumed: set[str] = set()
        own: dict[str, Any] = {
            "association": el_name,
            "entity": csn_name,
            "managed": managed,
            "type": element.get("type"),
        }
        annotations = c.split_annotations(element, consumed)
        if annotations:
            own["annotations"] = annotations
        native = {k: v for k, v in element.items() if not k.startswith("@") and k not in _MAPPED_ELEMENT_KEYS}
        if native:
            own["native"] = native
        relationships.append(
            OssieRelationship(
                name=el_name,
                **{"from": dataset_name},
                to=entities.dataset_name[target],
                from_columns=from_columns,
                to_columns=to_columns,
                ai_context=element.get(c.OSSIE_AI_CONTEXT),
                custom_extensions=_extensions(own, element),
            )
        )

    def _metrics(
        self, candidates: list[dict[str, Any]]
    ) -> tuple[list[OssieMetric], dict[str, list[OssieMetric]]]:
        """The model-scoped metrics, and the dataset-scoped ones by dataset name."""
        # Model-scoped names are global; a dataset-scoped one only shares its dataset's.
        counts: dict[str, int] = {}
        for m in candidates:
            if not m["dataset_scoped"]:
                counts[m["element"]] = counts.get(m["element"], 0) + 1
        model_metrics: list[OssieMetric] = []
        dataset_metrics: dict[str, list[OssieMetric]] = {}
        for m in candidates:
            unique = m["dataset_scoped"] or counts[m["element"]] == 1
            name = m["element"] if unique else f"{m['dataset']}_{m['element']}"
            agg = m["aggregation"]

            if agg in ("COUNT", "COUNT_DISTINCT"):
                datatype: Optional[str] = "Integer"
            elif agg == "AVG":
                datatype = "Float" if m["builtin"] == "cds.Double" else "Decimal"
            else:
                datatype = m["datatype"]

            own: dict[str, Any] = {
                "entity": m["entity"],
                "element": m["element"],
                "aggregation": m["aggregation_raw"],
                "derived": m["derived"],
            }
            if m["derived"]:
                own["measure"] = m["own"]
            metric = OssieMetric(
                name=name,
                expression=OssieExpression(
                    dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=m["sql"])]
                ),
                description=m["description"] if m["derived"] else None,
                datatype=datatype,
                ai_context=m["element_obj"].get(c.OSSIE_AI_CONTEXT) if m["derived"] else None,
                custom_extensions=_extensions(own, m["element_obj"] if m["derived"] else {}),
            )
            if m["dataset_scoped"]:
                dataset_metrics.setdefault(m["dataset"], []).append(metric)
            else:
                model_metrics.append(metric)
        return model_metrics, dataset_metrics
