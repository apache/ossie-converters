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

"""Convert a Solid semantic model YAML export to an Apache Ossie semantic model.

Pure offline conversion. Solid features Apache Ossie has no native field for --
`example_queries`, `benchmark_questions`, `quality_rank`, `indexes`, `sample_values`,
and each column's raw warehouse type -- are preserved in `custom_extensions[SOLID]`, so
converting back reproduces the original export.

Usage (CLI):
    ossie-solid import -i solid_model.yaml [-o model.yaml] [--dialect SNOWFLAKE]
"""

from . import datatypes
from ._common import (
    ONE_TO_ONE_NOTE,
    OSSIE_VERSION,
    ConversionError,
    as_list,
    as_mapping,
    build_ai_context,
    clean_text,
    dataset_name_for,
    dump_yaml,
    has_asset_links,
    instruction_entries,
    load_yaml,
    render_custom_instructions,
    require_str,
    string_list,
    unique_name,
    warn,
    warn_unknown_keys,
    write_stash,
)

# Every key solid-server's YAML export writes, per object (see
# semantic_layer_yaml_export_template.py). Anything else is reported, not dropped
# silently.
_MODEL_KEYS = frozenset({
    "name", "business_context", "custom_instructions", "model_llm_description",
    "tables", "metrics", "relationships", "example_queries", "benchmark_questions",
})
_BUSINESS_CONTEXT_KEYS = frozenset(
    {"business_questions", "model_description", "custom_instructions"})
_TABLE_KEYS = frozenset({
    "name", "description", "manual_description", "synonyms", "primary_key",
    "quality_rank", "indexes", "dimensions", "facts",
})
_COLUMN_KEYS = frozenset({
    "name", "type", "description", "manual_description", "synonyms", "sample_values",
    "expression",
})
_METRIC_KEYS = frozenset({"name", "description", "expression", "synonyms", "tables"})
_RELATIONSHIP_KEYS = frozenset({"left_table", "right_table", "join_keys"})
_SOLID = "a Solid semantic model"
from .expressions import (
    AMBIGUOUS,
    SHARED,
    UNCHANGED,
    UNPARSED,
    column_reference,
    qualify_by_owner,
    referenced_datasets,
)


def convert_solid_to_ossie(solid_yaml_str, dialect=None, model_name=None):
    """Parse a Solid semantic model export and return Apache Ossie YAML (a string).

    `dialect` forces the Apache Ossie expression dialect; when omitted it is inferred
    from the column type vocabulary (see datatypes.infer_dialect). `model_name`
    overrides the Apache Ossie model name.
    """
    document = load_yaml(solid_yaml_str)
    if not isinstance(document, dict):
        raise ConversionError(
            "Invalid Solid semantic model: expected a mapping at the root")
    solid = document.get("semantic_model")
    # An Apache Ossie document puts `version` and `datasets` at its root (or, in the
    # legacy layout, keys `semantic_model` to a list). Naming the confusion is more
    # useful than a generic missing-key or type error.
    if isinstance(solid, list) or (
        solid is None and ("version" in document or "datasets" in document)
    ):
        raise ConversionError(
            "This looks like an Apache Ossie document, not a Solid semantic model. "
            "Did you mean `ossie-solid export`?"
        )
    if solid is None:
        raise ConversionError("Solid semantic model is missing the top-level "
                              "'semantic_model' key")
    if not isinstance(solid, dict):
        raise ConversionError("'semantic_model' must be a mapping")
    warn_unknown_keys(document, {"semantic_model"}, "model", "the document", _SOLID)

    model = _convert_model(solid, dialect, model_name)
    return dump_yaml({"version": OSSIE_VERSION, **model})


def _convert_model(solid, explicit_dialect, model_name):
    warn_unknown_keys(solid, _MODEL_KEYS, "model", "semantic_model", _SOLID)
    tables = as_list(solid.get("tables"), "semantic_model: 'tables'")
    if not tables:
        # Apache Ossie requires at least one dataset (schema: datasets.minItems = 1).
        raise ConversionError("Solid semantic model has no 'tables'; an Apache Ossie "
                              "model requires at least one dataset")

    resolved_dialect = datatypes.resolve_dialect(
        explicit_dialect, _all_raw_types(tables), scope="model")

    # Solid names a table by its fully-qualified `catalog.schema.table`, which becomes
    # the Apache Ossie `source`; the dataset name is derived from it and is what
    # relationships and metric expressions refer to.
    datasets, by_source = [], {}
    taken_names = set()
    for table in tables:
        dataset = _convert_table(table, resolved_dialect, taken_names)
        datasets.append(dataset)
        key = dataset["source"].lower()
        if key in by_source:
            # Relationships and metrics name their tables by FQN, matched
            # case-insensitively, so keeping either copy would silently bind them to
            # the wrong one.
            first, second = by_source[key]["source"], dataset["source"]
            listed = (f"table '{first}' more than once" if first == second else
                      f"tables '{first}' and '{second}', which differ only in case")
            raise ConversionError(
                f"Solid model lists {listed}; relationships and metrics could not tell "
                f"them apart")
        by_source[key] = dataset

    relationships = _convert_relationships(
        as_list(solid.get("relationships"), "semantic_model: 'relationships'"),
        by_source)
    metrics = _convert_metrics(
        as_list(solid.get("metrics"), "semantic_model: 'metrics'"),
        by_source, datasets, resolved_dialect)

    business = as_mapping(
        solid.get("business_context"), "semantic_model: 'business_context'")
    warn_unknown_keys(
        business, _BUSINESS_CONTEXT_KEYS, "model", "business_context", _SOLID)
    llm_description = clean_text(
        solid.get("model_llm_description"), "semantic_model: 'model_llm_description'")
    customer_description = clean_text(
        business.get("model_description"), "business_context: 'model_description'")
    raw_instructions = clean_text(
        business.get("custom_instructions"), "business_context: 'custom_instructions'")
    # Solid's structured custom instructions, a root-level list. When Solid writes it,
    # it leaves `business_context.custom_instructions` out.
    entries = instruction_entries(
        solid.get("custom_instructions"), "semantic_model: 'custom_instructions'")

    model = {"name": model_name or require_str(solid, "name", "semantic_model")}
    description = llm_description or customer_description
    if description:
        model["description"] = description
    ai_context = build_ai_context(
        # Solid resolves its asset-link markup to plain display names before the text
        # reaches an LLM; do the same for the Apache Ossie consumer, keeping the tagged
        # original and the structured entries in the stash.
        instructions=render_custom_instructions(raw_instructions, entries),
        examples=string_list(business.get("business_questions"),
                             "business_context: 'business_questions'"),
    )
    if ai_context:
        model["ai_context"] = ai_context

    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics

    stash = {"dialect": resolved_dialect}
    # The model carries three free-text fields but Apache Ossie offers two slots, so
    # both raw descriptions are recorded; export reads them back rather than guessing
    # which one the single `description` came from.
    if llm_description:
        stash["model_llm_description"] = llm_description
    if customer_description:
        stash["model_description"] = customer_description
    if entries:
        stash["custom_instruction_entries"] = entries
    if raw_instructions and (entries or has_asset_links(raw_instructions)):
        stash["custom_instructions"] = raw_instructions
    for key in ("example_queries", "benchmark_questions"):
        value = solid.get(key)
        if value:
            stash[key] = value
    write_stash(model, stash)
    return model


def _all_raw_types(tables):
    """Every raw column type in the model, for dialect inference."""
    types = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for column in _columns_of(table):
            if isinstance(column, dict) and column.get("type"):
                types.append(column["type"])
    return types


def _columns_of(table):
    """Dimensions followed by facts, skipping anything that is not a mapping."""
    columns = []
    for key in ("dimensions", "facts"):
        for column in as_list(table.get(key), f"table '{table.get('name')}': '{key}'"):
            if isinstance(column, dict):
                columns.append(column)
    return columns


def _convert_table(table, dialect, taken_names):
    if not isinstance(table, dict):
        raise ConversionError("Each entry of 'tables' must be a mapping")
    source = require_str(table, "name", "table")
    name = dataset_name_for(source, taken_names)
    warn_unknown_keys(table, _TABLE_KEYS, "dataset", f"table '{source}'", _SOLID)

    dataset = {"name": name, "source": source}

    primary_key = _split_primary_key(
        table.get("primary_key"), f"table '{source}': 'primary_key'")
    if primary_key:
        dataset["primary_key"] = primary_key

    description = clean_text(table.get("description"), f"table '{source}': 'description'")
    if description:
        dataset["description"] = description
    # Solid carries an AI-written `description` and a human-written
    # `manual_description`. Apache Ossie has one description field, so the AI one -- the
    # consistently populated, consumer-facing text -- maps to `description` and the
    # human annotation maps to `ai_context.instructions`. The split is 1:1 in both
    # directions, so neither needs stashing.
    ai_context = build_ai_context(
        instructions=clean_text(
            table.get("manual_description"), f"table '{source}': 'manual_description'"),
        synonyms=string_list(table.get("synonyms"), f"table '{source}': 'synonyms'"),
    )
    if ai_context:
        dataset["ai_context"] = ai_context

    fields, field_names = [], set()
    for column in as_list(table.get("dimensions"), f"table '{source}': 'dimensions'"):
        fields.append(_convert_column(column, dialect, name, field_names, True))
    for column in as_list(table.get("facts"), f"table '{source}': 'facts'"):
        fields.append(_convert_column(column, dialect, name, field_names, False))
    if fields:
        dataset["fields"] = fields

    stash = {}
    quality_rank = clean_text(table.get("quality_rank"), f"table '{source}': 'quality_rank'")
    if quality_rank:
        stash["quality_rank"] = quality_rank
    indexes = string_list(table.get("indexes"), f"table '{source}': 'indexes'")
    if indexes:
        stash["indexes"] = indexes
    write_stash(dataset, stash)
    return dataset


def _split_primary_key(value, what):
    """Split Solid's comma-joined primary key scalar into Apache Ossie's column array.

    solid-server joins a composite key's column names with `", "` into a single scalar
    (`primary_key: 'ORDER_ID, LINE_NO'`), so splitting on the comma is what recovers the
    composite. A non-string value is tolerated: a list is taken as-is, since a
    hand-edited export may already carry one.
    """
    if isinstance(value, list):
        return string_list(value, what)
    text = clean_text(value, what)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _convert_column(column, dialect, dataset_name, taken, is_dimension):
    if not isinstance(column, dict):
        raise ConversionError(
            f"Dataset '{dataset_name}': each column entry must be a mapping")
    raw_name = require_str(column, "name", f"column in '{dataset_name}'")
    warn_unknown_keys(
        column, _COLUMN_KEYS, "field", f"column '{dataset_name}.{raw_name}'", _SOLID)
    name = unique_name(raw_name, taken)
    if name != raw_name:
        warn("field", f"'{dataset_name}' declares '{raw_name}' more than once; the "
                      f"duplicate was renamed to '{name}'")

    raw_type = clean_text(column.get("type"), f"column '{dataset_name}.{raw_name}': 'type'")
    # Solid renders two kinds of fact: a catalog column (which has a `type`) and a
    # table-scoped metric (which has an `expression` and no `type`). The former's
    # expression is just its own name.
    solid_expression = clean_text(
        column.get("expression"), f"column '{dataset_name}.{raw_name}': 'expression'")
    expression = solid_expression or column_reference(raw_name, dialect)

    field = {
        "name": name,
        "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
    }
    description = clean_text(
        column.get("description"), f"column '{dataset_name}.{raw_name}': 'description'")
    if description:
        field["description"] = description

    datatype = datatypes.to_ossie_datatype(raw_type, dialect)
    if datatype:
        field["datatype"] = datatype
    elif raw_type:
        warn("field", f"'{dataset_name}.{name}': warehouse type '{raw_type}' has no "
                      f"Apache Ossie datatype; 'datatype' was omitted")

    if is_dimension:
        # The presence of the `dimension` block is what records that Solid classified
        # this column as a dimension rather than a fact, so it is always emitted for a
        # dimension -- with `is_time` stated explicitly rather than left to the spec's
        # datatype-derived default, so a consumer that does not implement that default
        # still reads the same role.
        field["dimension"] = {"is_time": datatype in datatypes.TEMPORAL_DATATYPES}

    ai_context = build_ai_context(
        instructions=clean_text(
            column.get("manual_description"),
            f"column '{dataset_name}.{raw_name}': 'manual_description'"),
        synonyms=string_list(
            column.get("synonyms"), f"column '{dataset_name}.{raw_name}': 'synonyms'"),
    )
    if ai_context:
        field["ai_context"] = ai_context

    stash = {}
    if name != raw_name:
        # Restored on export, so the Solid model does not gain a column the catalog does
        # not have. `to` lets export tell whether the name was changed again since.
        stash["renamed"] = {"from": raw_name, "to": name}
    if raw_type:
        # Apache Ossie's portable datatype is lossy (NUMBER(38,2), NUMERIC and DECIMAL
        # all collapse to Decimal), so the catalog's own type name is kept verbatim.
        stash["type"] = raw_type
    # Solid's role for the column, so export puts it back in the same list without
    # inferring it. A fact with an expression is a table-scoped metric, recorded even
    # beside a `type`: the stashed type would otherwise read it back as a plain column
    # and drop its expression. A dimension with an expression is likewise recorded as
    # computed, so its expression is restored rather than dropped as a foreign one is.
    if is_dimension:
        stash["role"] = "dimension"
        if solid_expression:
            stash["computed"] = True
    else:
        stash["role"] = "metric" if solid_expression else "fact"
    sample_values = as_list(column.get("sample_values"),
                            f"column '{dataset_name}.{raw_name}': 'sample_values'")
    if sample_values:
        stash["sample_values"] = sample_values
    write_stash(field, stash)
    return field


def _convert_relationships(relationships, by_source):
    converted, taken = [], set()
    for relationship in relationships:
        if not isinstance(relationship, dict):
            raise ConversionError("Each entry of 'relationships' must be a mapping")
        left_source = require_str(relationship, "left_table", "relationship")
        right_source = require_str(relationship, "right_table", "relationship")
        warn_unknown_keys(relationship, _RELATIONSHIP_KEYS, "relationship",
                          f"relationship '{left_source}' -> '{right_source}'", _SOLID)
        left = by_source.get(left_source.lower())
        right = by_source.get(right_source.lower())
        if left is None or right is None:
            missing = left_source if left is None else right_source
            warn("relationship",
                 f"'{left_source}' -> '{right_source}' references table '{missing}', "
                 f"which is not in 'tables'; the relationship was dropped")
            continue

        what = f"relationship '{left_source}' -> '{right_source}'"
        join_keys = as_mapping(relationship.get("join_keys"), f"{what}: 'join_keys'")
        warn_unknown_keys(join_keys, {"left", "right"}, "relationship",
                          f"{what} join_keys", _SOLID)
        left_columns = string_list(join_keys.get("left"), f"{what}: 'join_keys.left'")
        right_columns = string_list(join_keys.get("right"), f"{what}: 'join_keys.right'")
        if not left_columns or not right_columns:
            warn("relationship",
                 f"'{left['name']}' -> '{right['name']}' has no join keys; the "
                 f"relationship was dropped (an Apache Ossie relationship requires at "
                 f"least one column on each side)")
            continue
        if len(left_columns) != len(right_columns):
            raise ConversionError(
                f"Relationship '{left['name']}' -> '{right['name']}' has "
                f"{len(left_columns)} left join key(s) but {len(right_columns)} right; "
                f"the columns must correspond positionally"
            )

        converted.append(_orient(left, right, left_columns, right_columns, taken))
    return converted


def _orient(left, right, left_columns, right_columns, taken):
    """Turn a Solid join into a directed Apache Ossie relationship.

    Apache Ossie encodes cardinality through direction -- `from` is the many side, `to`
    the one side -- but a Solid relationship is an undirected pair of column lists. The
    one side is therefore recovered from the primary keys: whichever end's primary key
    is exactly its join columns is unique on those columns, and so is the one side.
    """
    left_is_key = _covers_primary_key(left, left_columns)
    right_is_key = _covers_primary_key(right, right_columns)

    note = None
    if right_is_key and not left_is_key:
        flipped = False
    elif left_is_key and not right_is_key:
        flipped = True
    elif left_is_key and right_is_key:
        # Both ends are unique on their join columns: a one-to-one. Apache Ossie has no
        # dedicated form, so the direction is arbitrary and recorded in `ai_context`.
        flipped = False
        note = ONE_TO_ONE_NOTE
    else:
        flipped = False
        warn("relationship",
             f"'{left['name']}' -> '{right['name']}': neither side's primary key "
             f"matches its join columns, so the many/one direction could not be "
             f"determined; assumed '{left['name']}' is the many side. Solid does not "
             f"record cardinality.")

    if flipped:
        source, target = right, left
        source_columns, target_columns = right_columns, left_columns
    else:
        source, target = left, right
        source_columns, target_columns = left_columns, right_columns

    name = unique_name(f"{source['name']}_to_{target['name']}", taken)
    relationship = {
        "name": name,
        "from": source["name"],
        "to": target["name"],
        "from_columns": source_columns,
        "to_columns": target_columns,
    }
    if note:
        relationship["ai_context"] = {"instructions": note}
    if flipped:
        # Only the orientation needs recording: relationships keep their list position in
        # both directions, so the ordering restores itself. Export re-emits
        # `left_table`/`right_table` the way Solid wrote them.
        write_stash(relationship, {"flipped": True})
    return relationship


def _covers_primary_key(dataset, columns):
    """True if `columns` is exactly the dataset's primary key (order-insensitive)."""
    primary_key = dataset.get("primary_key")
    if not primary_key:
        return False
    return {c.lower() for c in primary_key} == {c.lower() for c in columns}


def _convert_metrics(metrics, by_source, datasets, dialect):
    dataset_names = [d["name"] for d in datasets]
    fields = {d["name"]: [f["name"] for f in d.get("fields") or []] for d in datasets}
    converted, taken = [], set()
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ConversionError("Each entry of 'metrics' must be a mapping")
        raw_name = require_str(metric, "name", "metric")
        warn_unknown_keys(metric, _METRIC_KEYS, "metric", f"metric '{raw_name}'", _SOLID)
        name = unique_name(raw_name, taken)
        if name != raw_name:
            warn("metric", f"'{raw_name}' is declared more than once; the duplicate "
                           f"was renamed to '{name}'")

        expression = clean_text(metric.get("expression"), f"metric '{raw_name}': 'expression'")
        if not expression:
            warn("metric", f"'{name}' has no expression and was dropped (an Apache "
                           f"Ossie metric requires one)")
            continue

        owners, unknown = [], []
        tables = string_list(metric.get("tables"), f"metric '{raw_name}': 'tables'")
        for source in tables:
            dataset = by_source.get(source.lower())
            (owners if dataset is not None else unknown).append(dataset or source)
        for source in unknown:
            warn("metric", f"'{name}' references table '{source}', which is not in "
                           f"'tables'")

        expression, status = _qualify(name, expression, owners, dialect)

        converted_metric = {
            "name": name,
            "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
        }
        description = clean_text(metric.get("description"), f"metric '{raw_name}': 'description'")
        if description:
            converted_metric["description"] = description
        synonyms = string_list(metric.get("synonyms"), f"metric '{raw_name}': 'synonyms'")
        if synonyms:
            converted_metric["ai_context"] = {"synonyms": synonyms}

        stash = {}
        if name != raw_name:
            stash["renamed"] = {"from": raw_name, "to": name}
        # Solid's `tables` list only needs stashing when the qualified expression does
        # not already name the same datasets -- which is the case for an unqualifiable
        # expression, and for one that references no column at all (`COUNT(*)`).
        expected = [d["name"] for d in owners]
        if (unknown
                or status in (UNPARSED, AMBIGUOUS, SHARED)
                or referenced_datasets(
                    expression, dialect, dataset_names, fields) != expected):
            stash["tables"] = tables
        write_stash(converted_metric, stash)
        converted.append(converted_metric)
    return converted


def _qualify(name, expression, owners, dialect):
    """Qualify a Solid metric's bare column references with their dataset names.

    Solid stores formulas against bare columns and records the owning tables
    separately; Apache Ossie requires a model-scoped metric to qualify every field as
    `dataset.field`. A column only one of the metric's tables has can only mean that
    table's. One that several of them have cannot be attributed from the file:
    solid-server strips alias prefixes when the formula is saved and keeps the
    alias-to-table binding in `metric.column_ids`, which its YAML export does not carry.
    Such an expression is left verbatim.
    """
    if not owners:
        warn("metric", f"'{name}' names no owning table; its column references "
                       f"were left unqualified")
        return expression, UNCHANGED

    owner_of, shared = {}, set()
    for dataset in owners:
        for field in dataset.get("fields") or []:
            column = field["name"].lower()
            if owner_of.setdefault(column, dataset["name"]) != dataset["name"]:
                shared.add(column)
    for column in shared:
        owner_of.pop(column, None)
    qualified, status = qualify_by_owner(expression, dialect, owner_of, shared)
    if status == SHARED:
        warn("metric",
             f"'{name}' spans {len(owners)} tables "
             f"({', '.join(d['name'] for d in owners)}) and reads a column more than one "
             f"of them has, so its column references cannot be attributed to a dataset "
             f"and were left unqualified")
    elif status == UNPARSED:
        warn("metric", f"'{name}': expression could not be parsed as {dialect} SQL and "
                       f"was left unqualified")
    elif status == AMBIGUOUS:
        warn("metric", f"'{name}': a column name in the expression also appears in a "
                       f"non-column position, so the reference could not be qualified "
                       f"safely; the expression was left as written")
    return qualified, status


__all__ = ["convert_solid_to_ossie"]
