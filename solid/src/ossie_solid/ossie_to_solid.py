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

"""Convert an Apache Ossie semantic model to a Solid semantic model YAML export.

Pure offline conversion. A model that came from `convert_solid_to_ossie` restores its
Solid-only fields from `custom_extensions[SOLID]`; a hand-authored Apache Ossie model is
converted on its core fields alone, with anything Solid's format cannot hold reported as
a warning rather than dropped silently.

Key order in the output follows solid-server's own export template, so a converted model
diffs cleanly against one Solid produced itself.

Usage (CLI):
    ossie-solid export -i model.yaml [-o solid_model.yaml] [--dialect SNOWFLAKE]
"""

from . import datatypes
from ._common import (
    DIALECT_ANSI,
    ONE_TO_ONE_NOTE,
    OSSIE_VERSION,
    OSSIE_VERSION_NOTES,
    PK_SEPARATOR,
    PORTABLE_DIALECTS,
    READABLE_OSSIE_VERSIONS,
    SUPPORTED_DIALECTS,
    ConversionError,
    ai_context_parts,
    as_list,
    as_mapping,
    as_str,
    clean_text,
    dump_yaml,
    foreign_vendor_extensions,
    instruction_entries,
    load_yaml,
    pick_expression,
    read_stash,
    readable_dialects,
    render_custom_instructions,
    require_str,
    split_source,
    string_list,
    warn,
    warn_unknown_keys,
)
from .expressions import (
    AMBIGUOUS,
    SCOPED,
    UNPARSED,
    column_read,
    is_aggregate,
    is_column_reference,
    is_query,
    referenced_datasets,
    rename_fields,
    unqualify_metric,
)

# The properties of each Apache Ossie object (core-spec/ossie-schema.json). The schema
# closes every object but `ai_context`, so anything else is a property of a spec
# version this converter does not know, and is reported rather than dropped silently.
_MODEL_KEYS = frozenset({
    "name", "description", "ai_context", "datasets", "relationships", "metrics",
    "custom_extensions",
})
_DATASET_KEYS = frozenset({
    "name", "source", "primary_key", "unique_keys", "description", "ai_context",
    "fields", "metrics", "custom_extensions",
})
_FIELD_KEYS = frozenset({
    "name", "expression", "dimension", "label", "description", "datatype", "ai_context",
    "custom_extensions",
})
_RELATIONSHIP_KEYS = frozenset({
    "name", "from", "to", "from_columns", "to_columns", "ai_context", "custom_extensions",
})
_METRIC_KEYS = frozenset({
    "name", "expression", "description", "datatype", "ai_context", "custom_extensions",
})
_OSSIE = f"Apache Ossie v{OSSIE_VERSION}"


def convert_ossie_to_solid(ossie_yaml_str, dialect=None, model_name=None):
    """Parse an Apache Ossie semantic model and return Solid YAML (a string).

    `dialect` chooses which expression dialect to read; when omitted it comes from the
    SOLID stash, else from whichever non-ANSI dialect the model's expressions use.
    `model_name` overrides the Solid model name.
    """
    document = load_yaml(ossie_yaml_str)
    if not isinstance(document, dict):
        raise ConversionError(
            "Invalid Apache Ossie model: expected a mapping at the root")

    if document.get("version") is None or not str(document["version"]).strip():
        if isinstance(document.get("semantic_model"), dict):
            raise ConversionError(
                "This looks like a Solid semantic model, not an Apache Ossie document. "
                "Did you mean `ossie-solid import`?")
        raise ConversionError("Apache Ossie document is missing required 'version'")
    version = str(document["version"]).strip()
    if version not in READABLE_OSSIE_VERSIONS:
        # Read rather than rejected: a spec release would otherwise make every document
        # fail. A real incompatibility still surfaces as a ConversionError or a warning
        # when the content is read.
        warn("model", f"the document declares Apache Ossie v{version}, which this "
                      f"converter does not know; it reads "
                      f"{' and '.join('v' + v for v in READABLE_OSSIE_VERSIONS)}, and "
                      f"read this one as v{OSSIE_VERSION}")
    elif version != OSSIE_VERSION:
        note = OSSIE_VERSION_NOTES.get(version)
        warn("model", f"the document declares Apache Ossie v{version}, not the current "
                      f"v{OSSIE_VERSION}"
                      + (f"; {note}" if note else ""))

    return dump_yaml({"semantic_model": _convert_model(
        _document_model(document), dialect, model_name)})


def _document_model(document):
    """The one semantic model an Apache Ossie document holds.

    A 0.2.0.dev0 document is the model: its properties sit at the root beside `version`.
    Apache Ossie 0.1.1 (and early 0.2.0.dev0 snapshots) wrapped models in a
    `semantic_model` list instead; that shape is still read, with a warning.
    """
    if "semantic_model" not in document:
        model = {k: v for k, v in document.items() if k != "version"}
        warn_unknown_keys(model, _MODEL_KEYS, "model", "the document", _OSSIE)
        return model

    models = document["semantic_model"]
    if not isinstance(models, list) or not models:
        raise ConversionError(
            "Apache Ossie document has a 'semantic_model' key that is not a non-empty "
            "list; a current document puts the model's properties at the root")
    warn("model", "the document uses the legacy wrapped 'semantic_model' format; the "
                  "current format puts the model's properties at the document root")
    if not all(isinstance(m, dict) for m in models):
        raise ConversionError("Each entry of 'semantic_model' must be a mapping")
    if len(models) > 1:
        warn("model", f"the document holds {len(models)} semantic models but Solid's "
                      f"format holds one; only '{models[0].get('name')}' was converted")
    warn_unknown_keys(models[0], _MODEL_KEYS, "model", "the model", _OSSIE)
    return models[0]


def _convert_model(ossie, explicit_dialect, model_name):
    datasets = ossie.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise ConversionError("Apache Ossie model has no 'datasets'")

    stash = read_stash(ossie, "model")
    resolved_dialect = _resolve_dialect(explicit_dialect, stash, ossie)
    _warn_foreign_extensions(ossie, "model", ossie.get("name"))

    by_name, by_folded_name = {}, {}
    for dataset in datasets:
        if not isinstance(dataset, dict):
            raise ConversionError("Each entry of 'datasets' must be a mapping")
        name = require_str(dataset, "name", "dataset")
        if name in by_name:
            # Relationships and metrics resolve datasets by name, so keeping either one
            # would silently join against the wrong table.
            raise ConversionError(
                f"Apache Ossie model declares dataset '{name}' more than once; dataset "
                f"names must be unique")
        if name.lower() in by_folded_name:
            # A metric names its datasets as SQL qualifiers, which match ignoring case
            # unless quoted, so `orders.amount` and `ORDERS.amount` could not be told
            # apart. Import keeps dataset names unique ignoring case for the same reason.
            raise ConversionError(
                f"Apache Ossie model declares datasets '{by_folded_name[name.lower()]}' "
                f"and '{name}', which differ only in case; metric expressions could not "
                f"tell them apart")
        by_name[name] = dataset
        by_folded_name[name.lower()] = name
    # Whether the producer marks its dimensions explicitly, putting a `dimension` block
    # on fields that are not time dimensions too. GoodData, OrionBelt and Cube do; Omni
    # and Microsoft mark only time fields. It only settles a field that has neither a
    # role nor a datatype to go on (see _field_role).
    marks_dimensions = any(
        isinstance(field, dict) and _dimension_block(field) is not None
        and not _is_time(field)
        for dataset in datasets
        for field in as_list(dataset.get("fields"), f"dataset '{dataset['name']}': fields")
    )

    instructions, synonyms, examples = ai_context_parts(ossie.get("ai_context"), "model")
    if synonyms:
        warn("model", "ai_context.synonyms has no Solid equivalent at the model level "
                      "and was dropped")
    llm_description, model_description = _model_descriptions(
        stash, clean_text(ossie.get("description"), "model: description"))
    business_context = {}
    if examples:
        business_context["business_questions"] = examples
    if model_description:
        business_context["model_description"] = model_description
    custom_instructions, entries = _custom_instructions(stash, instructions)
    if custom_instructions:
        business_context["custom_instructions"] = custom_instructions

    solid = {"name": model_name or require_str(ossie, "name", "Apache Ossie model")}
    # Model fields Apache Ossie has no slot for, which import stashed as Solid wrote
    # them, in the order solid-server's template writes them.
    for key in ("version_number", "source_system", "target_system"):
        if stash.get(key) is not None:
            solid[key] = stash[key]
    solid["business_context"] = business_context
    if entries:
        solid["custom_instructions"] = entries
    if llm_description:
        solid["model_llm_description"] = llm_description
    short_description = clean_text(stash.get("model_llm_short_description"),
                                   "model: stashed model_llm_short_description")
    if short_description:
        solid["model_llm_short_description"] = short_description

    # Import derived each field's `datatype` from its raw type in the dialect it recorded,
    # so that is the dialect to check the two still agree in (see _recorded_type).
    stashed_dialect = clean_text(stash.get("dialect"))
    recorded_dialect = (datatypes.normalize_dialect(stashed_dialect)
                        if stashed_dialect else resolved_dialect)
    dropped = _query_datasets(datasets, resolved_dialect)
    if len(dropped) == len(datasets):
        raise ConversionError(
            "Every dataset's source is a SQL query, and Solid's tables come from the "
            "warehouse catalog, so there is nothing to convert")
    # Per dataset, the fields that are aliases for another column, as {field: (the
    # column's name, its SQL text)}: keys, joins and metrics name the field, Solid the
    # column.
    aliases = {}
    solid["tables"], by_table = [], {}
    for dataset in datasets:
        if dataset["name"] in dropped:
            continue
        # Relationships and metrics name a dataset's table by this too, through by_name.
        dataset["source"] = _solid_table_name(dataset)
        table, aliases[dataset["name"]] = _convert_dataset(
            dataset, resolved_dialect, recorded_dialect, marks_dimensions)
        first = by_table.get(table["name"].lower())
        if first is None:
            by_table[table["name"].lower()] = (dataset["name"], table)
            solid["tables"].append(table)
        else:
            _merge_table(first, dataset["name"], table)

    # Solid holds every metric in one model-level list, binding each to its tables, so a
    # dataset-scoped metric joins it bound to the dataset that declares it.
    taken = set()
    metrics = _convert_metrics(as_list(ossie.get("metrics"), "model: metrics"),
                               by_name, resolved_dialect, taken=taken, dropped=dropped,
                               aliases=aliases)
    for dataset in datasets:
        if dataset["name"] in dropped:
            continue
        metrics += _convert_metrics(
            as_list(dataset.get("metrics"), f"dataset '{dataset['name']}': metrics"),
            by_name, resolved_dialect, scope=dataset["name"], taken=taken,
            dropped=dropped, aliases=aliases)
    if metrics:
        solid["metrics"] = metrics
    relationships = _convert_relationships(
        as_list(ossie.get("relationships"), "model: relationships"), by_name, dropped,
        aliases)
    if relationships:
        solid["relationships"] = relationships
    for key in ("example_queries", "benchmark_questions"):
        if stash.get(key):
            solid[key] = stash[key]
    return solid


# The stash records what import wrote so an export can restore what Apache Ossie cannot
# hold. A stashed value that stands in for a live Apache Ossie field is used only while
# that field still holds what import put there: once it is edited in Apache Ossie, the
# stash describes a value that is gone, and the edit wins, with a warning.


def _model_descriptions(stash, description):
    """Solid's (`model_llm_description`, `model_description`) for the model.

    Solid carries two model descriptions and Apache Ossie one, so import writes the LLM
    one to `description` -- or the customer one, when there is no LLM one -- and stashes
    both. An edit replaces whichever of the two `description` was written from; the
    other, which Apache Ossie never showed, is restored as is. With no stash,
    `description` is the model's primary description, the role `model_llm_description`
    plays.
    """
    stashed_llm = clean_text(stash.get("model_llm_description"))
    stashed_customer = clean_text(stash.get("model_description"))
    if stashed_llm is None and stashed_customer is None:
        return description, None
    if description == (stashed_llm or stashed_customer):
        return stashed_llm, stashed_customer

    replaced = "model_llm_description" if stashed_llm else "model_description"
    if description:
        warn("model", f"description was edited in Apache Ossie; the edited text "
                      f"replaces Solid's {replaced}")
    else:
        warn("model", f"description was removed in Apache Ossie, so Solid's {replaced} "
                      f"was left out")
    if stashed_llm:
        return description, stashed_customer
    return None, description


def _custom_instructions(stash, instructions):
    """Solid's (`business_context.custom_instructions`, root `custom_instructions`
    entries): what import stashed, while it still renders to `ai_context.instructions`,
    else the instructions as they now stand.

    Import renders Solid's free-text instructions and its enabled structured entries
    into one text, resolving `@<assetlink ...>` markup to display names for the Apache
    Ossie consumer, and stashes the originals, so a re-import into Solid keeps its
    catalog references live and its entries structured.
    """
    stashed = clean_text(stash.get("custom_instructions"))
    entries = instruction_entries(
        stash.get("custom_instruction_entries"), "model: stashed custom instructions")
    if not stashed and not entries:
        return instructions, None
    if clean_text(render_custom_instructions(stashed, entries)) == instructions:
        return stashed, entries or None
    lost = ("structured custom instructions recorded at import" if entries else
            "Solid asset links in the original instructions")
    if instructions:
        warn("model", f"ai_context.instructions was edited in Apache Ossie; the edited "
                      f"text was used, so the {lost} were not restored")
    else:
        warn("model", "ai_context.instructions was removed in Apache Ossie, so Solid's "
                      "custom_instructions were left out")
    return instructions, None


def _resolve_dialect(explicit, stash, ossie):
    """Pick which Apache Ossie expression dialect to read expressions from.

    An explicit choice wins, then the dialect the SOLID stash recorded at import time,
    then the single non-portable dialect the model's expressions use. A model written
    purely in ANSI_SQL or OSSIE_SQL_2026 resolves to ANSI_SQL, which also reads
    OSSIE_SQL_2026 (see pick_expression).
    """
    if explicit:
        return datatypes.normalize_dialect(explicit)
    stashed = clean_text(stash.get("dialect"))
    if stashed:
        # Written by this converter's own import, so it is always one of the supported
        # names; validating it means a hand-edited stash fails loudly rather than
        # feeding an unknown dialect to the expression rewriter.
        return datatypes.normalize_dialect(stashed)

    # Apache Ossie's dialect enum includes expression languages that are not SQL (MDX,
    # TABLEAU, MAQL). Those cannot be parsed, qualified or unqualified by this
    # converter, so they never become the resolved dialect -- naming one here would
    # hand a non-SQL formula to sqlglot and to Solid as though it were SQL.
    found, unreadable = set(), set()
    for expression in _all_expressions(ossie):
        entries = expression.get("dialects") if isinstance(expression, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            dialect = entry.get("dialect") if isinstance(entry, dict) else None
            if not isinstance(dialect, str) or dialect in PORTABLE_DIALECTS:
                continue
            (found if dialect in SUPPORTED_DIALECTS else unreadable).add(dialect)
    if unreadable:
        warn("model", f"the model carries {', '.join(sorted(unreadable))} expressions, "
                      f"which are not SQL this converter can read; only the "
                      f"{' or '.join(PORTABLE_DIALECTS)} form of each expression will "
                      f"be used")
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        warn("model", f"expressions mix the {', '.join(sorted(found))} dialects; "
                      f"reading {DIALECT_ANSI}. Pass --dialect to choose one.")
    return DIALECT_ANSI


def _all_expressions(ossie):
    """Every field and metric expression, skipping anything malformed -- the per-object
    conversion reports those, naming the object."""
    def entries(value):
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    for dataset in entries(ossie.get("datasets")):
        for field in entries(dataset.get("fields")):
            yield field.get("expression")
        for metric in entries(dataset.get("metrics")):
            yield metric.get("expression")
    for metric in entries(ossie.get("metrics")):
        yield metric.get("expression")


def _warn_foreign_extensions(obj, scope, name):
    """Warn about custom_extensions belonging to other vendors.

    Solid's format has no slot for vendor metadata, so these cannot be carried across.
    """
    foreign = foreign_vendor_extensions(obj, f"{scope} '{name}'")
    if foreign:
        vendors = ", ".join(sorted({e.get("vendor_name") or "?" for e in foreign}))
        warn(scope, f"'{name}': custom_extensions for {vendors} have no Solid "
                    f"equivalent and were dropped")


def _solid_table_name(dataset):
    """The Solid name of a dataset's table: its `source` as an unquoted
    `catalog.schema.table`, which is how Solid names a table in its catalog. A source
    with fewer parts cannot be found there, and is reported."""
    source = dataset["source"]
    parts = split_source(source)
    if len(parts) < 3:
        warn("dataset", f"'{dataset['name']}': source {source} is not a "
                        f"catalog.schema.table name, which Solid needs to find the "
                        f"table in the warehouse")
    return ".".join(parts)


def _merge_table(first, name, table):
    """Fold `table`, converted from dataset `name`, into `first`, a (dataset name,
    table) already converted for the same Solid table.

    Apache Ossie may declare one table as two datasets, as with a date table joined
    twice under different names; Solid lists a table once. Columns the second adds
    are kept; its table-level description, key and synonyms are the first's.
    """
    first_name, kept = first
    columns = {c["name"].lower() for c in (kept.get("dimensions") or []) + kept["facts"]}
    for kind in ("dimensions", "facts"):
        extra = [c for c in table.get(kind) or [] if c["name"].lower() not in columns]
        columns.update(c["name"].lower() for c in extra)
        if extra:
            kept.setdefault(kind, []).extend(extra)
    # Solid writes `dimensions` before `facts`.
    kept["facts"] = kept.pop("facts")
    differs = [key for key in ("description", "manual_description", "synonyms",
                               "primary_key") if table.get(key) != kept.get(key)]
    warn("dataset", f"'{name}' reads the same table as '{first_name}', "
                    f"{table['name']}; Solid lists a table once, so its columns were "
                    f"merged into it"
                    + (f", and its own {', '.join(differs)} dropped" if differs else ""))


def _query_datasets(datasets, dialect):
    """The names of the datasets whose `source` is a SQL query, not a table.

    Apache Ossie allows either, but Solid's tables come from the warehouse catalog, so a
    query-backed dataset has no Solid slot: naming a table after the SQL would point at
    a table that does not exist. It is dropped, and the relationships and metrics that
    read it are dropped with it (see _convert_relationships, _convert_metrics).
    """
    dropped = set()
    for dataset in datasets:
        name = dataset["name"]
        if not is_query(require_str(dataset, "source", f"dataset '{name}'"), dialect):
            continue
        dropped.add(name)
        scoped = len(as_list(dataset.get("metrics"), f"dataset '{name}': metrics"))
        what = "the dataset was"
        if scoped:
            what = (f"the dataset and its {scoped} dataset-scoped "
                    f"metric{'s' if scoped > 1 else ''} were")
        warn("dataset", f"'{name}': source is a SQL query, not a table; Solid's tables "
                        f"come from the warehouse catalog, so {what} dropped")
    return dropped


def _convert_dataset(dataset, dialect, recorded_dialect, marks_dimensions):
    name = require_str(dataset, "name", "dataset")
    warn_unknown_keys(dataset, _DATASET_KEYS, "dataset", f"'{name}'", _OSSIE)
    stash = read_stash(dataset, f"dataset '{name}'")
    _warn_foreign_extensions(dataset, "dataset", name)

    # Solid identifies a table by its fully-qualified name, which is the Apache Ossie
    # `source`; the Apache Ossie dataset name is a local alias with no Solid slot.
    table = {"name": require_str(dataset, "source", f"dataset '{name}'")}

    description = clean_text(dataset.get("description"), f"dataset '{name}': description")
    if description:
        table["description"] = description
    instructions, synonyms, examples = ai_context_parts(
        dataset.get("ai_context"), f"dataset '{name}'")
    if instructions:
        table["manual_description"] = instructions
    if synonyms:
        table["synonyms"] = synonyms
    if examples:
        warn("dataset", f"'{name}': ai_context.examples has no Solid equivalent at the "
                        f"table level and was dropped")

    fields = as_list(dataset.get("fields"), f"dataset '{name}': fields")
    # Which field each column name belongs to, starting with every field's own name: an
    # alias for a column another field already is would list it twice.
    taken = {f["name"].lower(): f["name"] for f in fields
             if isinstance(f, dict) and isinstance(f.get("name"), str)}
    dimensions, facts, aliases = [], [], {}
    for field in fields:
        if not isinstance(field, dict):
            raise ConversionError(f"Dataset '{name}': each field must be a mapping")
        column, is_dimension, alias = _convert_field(
            field, dialect, recorded_dialect, name, marks_dimensions, taken)
        if alias:
            aliases[field["name"]] = alias
        if column is not None:
            (dimensions if is_dimension else facts).append(column)

    primary_key = string_list(dataset.get("primary_key"), f"dataset '{name}': primary_key")
    undeclared = _undeclared(primary_key, fields)
    if undeclared:
        warn("dataset", f"'{name}': primary_key names {', '.join(undeclared)}, which "
                        f"{'is not a declared field' if len(undeclared) == 1 else 'are not declared fields'}"
                        f" of the dataset, so the key was dropped")
    elif primary_key:
        # Solid stores a composite key as one comma-joined scalar.
        table["primary_key"] = PK_SEPARATOR.join(
            _column_names(primary_key, aliases))
    unique_keys = as_list(dataset.get("unique_keys"), f"dataset '{name}': unique_keys")
    if unique_keys and unique_keys != [primary_key]:
        warn("dataset", f"'{name}': unique_keys have no Solid equivalent and were "
                        f"dropped")
    # Solid emits `quality_rank` on every table, empty when the rank is unset.
    table["quality_rank"] = clean_text(stash.get("quality_rank")) or ""
    indexes = string_list(stash.get("indexes"), f"dataset '{name}': stashed indexes")
    if indexes:
        table["indexes"] = indexes

    if dimensions:
        table["dimensions"] = dimensions
    # Unlike `dimensions`, Solid always emits `facts` -- as `[]` when a table has none.
    table["facts"] = facts
    return table, aliases


def _undeclared(names, fields):
    """The names in `names` that are not one of `fields`, a dataset's field mappings.
    Keys and join columns name fields; like the spec's validator, ignoring case."""
    declared = {f["name"].casefold() for f in fields
                if isinstance(f, dict) and isinstance(f.get("name"), str)}
    return [n for n in dict.fromkeys(names) if n.casefold() not in declared]


def _column_names(fields, aliases):
    """The Solid column names of `fields`, a list of field names: a field that is an
    alias for another column (see _convert_field) names that column. Names match
    ignoring case, as keys and join columns are checked (see _undeclared)."""
    by_folded = {name.casefold(): alias for name, alias in aliases.items()}
    return [by_folded[f.casefold()][0] if f.casefold() in by_folded else f
            for f in fields]


def _convert_field(field, dialect, recorded_dialect, dataset_name, marks_dimensions,
                   taken):
    name = require_str(field, "name", f"field in '{dataset_name}'")
    warn_unknown_keys(field, _FIELD_KEYS, "field", f"'{dataset_name}.{name}'", _OSSIE)
    stash = read_stash(field, f"field '{dataset_name}.{name}'")
    if stash.get("undeclared") is True:
        # A key or join column the Solid table did not list, declared on import only so
        # the Apache Ossie model resolves (see solid_to_ossie._declare_key_columns). It
        # is left out again only while it is still what import wrote: once it is edited
        # in Apache Ossie, it is a column someone described, and is exported as one.
        if _is_unedited_key_column(field, name, dialect):
            return None, False, None
        warn("field", f"'{dataset_name}.{name}' was declared at import only because a "
                      f"key or join names it, but it was edited in Apache Ossie, so it "
                      f"is exported as a column")
    _warn_foreign_extensions(field, "field", f"{dataset_name}.{name}")
    solid_name = _solid_name(name, stash, "field", f"{dataset_name}.{name}")

    expression, fallback = pick_expression(
        field.get("expression"), dialect, f"Field '{dataset_name}.{name}'")
    if expression is None:
        raise ConversionError(
            f"Field '{dataset_name}.{name}' has no "
            f"{readable_dialects(dialect)} expression")
    if fallback and dialect != DIALECT_ANSI:
        warn("field", f"'{dataset_name}.{name}' has no {dialect} expression; the "
                      f"{fallback} one was used")

    # A field over another column (`cust` reading `customer_id`, or `amount` reading the
    # case-sensitive `"Amount"`) is an alias Solid has no slot for: a Solid column is a
    # catalog column, so it takes the column's name, keeping the field's as a synonym. A
    # duplicate import renamed is not one: its stash restores Solid's name.
    alias = None
    read = column_read(expression, dialect) if stash.get("renamed") is None else None
    if read is not None and not is_column_reference(expression, name, dialect):
        alias = (read, expression.strip())
        if taken.get(read.lower(), name) != name:
            warn("field", f"'{dataset_name}.{name}' reads the column {read}, which "
                          f"another field already exports; Solid lists a column once, "
                          f"so it was dropped, and references to it read {read}")
            return None, False, alias
        taken[read.lower()] = name
        warn("field", f"'{dataset_name}.{name}' reads the column {read}; Solid names a "
                      f"column by its catalog name, so it was exported as {read}, with "
                      f"'{name}' as a synonym")
        solid_name = read

    datatype = _declared_datatype(field, f"{dataset_name}.{name}")
    recorded_type = _recorded_type(
        datatype, stash, recorded_dialect, f"{dataset_name}.{name}")
    raw_type = recorded_type or datatypes.to_raw_type(datatype, dialect)
    role = _field_role(field, stash, datatype, raw_type, marks_dimensions,
                       f"{dataset_name}.{name}")
    computed = not is_column_reference(expression, solid_name, dialect)
    if role == METRIC and not computed:
        warn("field", f"'{dataset_name}.{name}' was a table-scoped metric in Solid, but "
                      f"its expression is now a plain reference to the column, so it is "
                      f"exported as a column")
        role = FACT
    is_dimension = role == DIMENSION
    # A dropped `datatype` with a stashed type still implies the same one.
    implied = datatype or (datatypes.to_ossie_datatype(recorded_type, recorded_dialect)
                           if recorded_type else None)
    _warn_time_role(field, implied, f"{dataset_name}.{name}")

    column = {"name": solid_name}
    # A Solid fact may be a table-scoped metric instead of a catalog column, in which
    # case it carries an expression and no type. Everything else is a column, whose
    # expression is just its own name.
    # Without a stashed role, only an aggregate is one: a scalar expression (`first_name
    # || ' ' || last_name`) is a computed column, whatever role the field takes.
    is_metric_fact = role == METRIC or (
        role == FACT and stash.get("role") is None and raw_type is None and computed
        and is_aggregate(expression, dialect))
    if is_metric_fact:
        # A metric has no catalog type to derive one from, so it keeps one only if
        # Solid typed it or a datatype was given to it in Apache Ossie.
        if raw_type and (stash.get("type") or datatype):
            column["type"] = raw_type
    elif raw_type:
        column["type"] = raw_type
    elif not (is_dimension and stash.get("computed") is True):
        # A computed dimension that had no type in Solid gets none back.
        warn("field", f"'{dataset_name}.{name}' has no datatype; Solid's 'type' was "
                      f"left empty")
        column["type"] = ""

    description = clean_text(
        field.get("description"), f"field '{dataset_name}.{name}': description")
    if description:
        column["description"] = description
    instructions, synonyms, examples = ai_context_parts(
        field.get("ai_context"), f"field '{dataset_name}.{name}'")
    if instructions:
        column["manual_description"] = instructions
    if is_metric_fact:
        column["expression"] = expression
    elif is_dimension and stash.get("computed") and computed:
        # A Solid dimension that carried an expression. Solid's own export never writes
        # one, but a hand-edited model can, and the round trip keeps it. The live
        # expression is used, so an edit in Apache Ossie wins.
        column["expression"] = expression
    elif computed:
        warn("field", f"'{dataset_name}.{name}' is a computed field "
                      f"(`{expression}`); Solid columns map to catalog columns, so the "
                      f"expression was dropped")
    if alias and name not in (synonyms or []):
        synonyms = [*(synonyms or []), name]
    if synonyms:
        column["synonyms"] = synonyms
    if examples:
        warn("field", f"'{dataset_name}.{name}': ai_context.examples has no Solid "
                      f"equivalent and was dropped")
    if field.get("label"):
        warn("field", f"'{dataset_name}.{name}': label '{field['label']}' has no Solid "
                      f"equivalent and was dropped")
    sample_values = as_list(stash.get("sample_values"),
                            f"field '{dataset_name}.{name}': stashed sample_values")
    if sample_values:
        column["sample_values"] = list(sample_values)
    return column, is_dimension, alias


def _is_unedited_key_column(field, name, dialect):
    """True while a field import declared for a key or join column (see
    solid_to_ossie._declare_key_columns) is still exactly what import wrote: a name,
    an expression reading its own column, and the SOLID stash, nothing more."""
    if set(field) - {"name", "expression", "custom_extensions"}:
        return False
    if foreign_vendor_extensions(field, f"field '{name}'"):
        return False
    expression, _ = pick_expression(field.get("expression"), dialect, f"Field '{name}'")
    return expression is not None and is_column_reference(expression, name, dialect)


# Solid's roles for a column: a dimension, a fact that is a catalog column, or a fact
# that is a table-scoped metric (an expression, usually with no type).
DIMENSION, FACT, METRIC = "dimension", "fact", "metric"


def _dimension_block(field):
    """The field's `dimension` block, or None when it has none."""
    if field.get("dimension") is None:
        return None
    return as_mapping(field["dimension"], f"Field '{field.get('name')}': dimension")


def _warn_time_role(field, datatype, label):
    """Report an explicit `is_time` that differs from what the datatype implies.

    Solid has no time-dimension marker, so a re-import derives `is_time` from the
    datatype again; an explicit one that says otherwise -- an Integer year grain marked
    as time, an audit timestamp opted out -- is lost, and says so.
    """
    block = _dimension_block(field)
    is_time = block.get("is_time") if block else None
    if is_time is None or is_time is (datatype in datatypes.TEMPORAL_DATATYPES):
        return
    warn("field", f"'{label}': dimension.is_time: {str(is_time).lower()} has no Solid "
                  f"equivalent and was dropped; a re-import derives it from the datatype")


def _is_time(field):
    """Whether a field is a time dimension: an explicit `is_time`, else the spec's
    default, which is true for a temporal datatype."""
    is_time = (_dimension_block(field) or {}).get("is_time")
    if is_time is not None:
        return is_time is True
    datatype = field.get("datatype")
    return isinstance(datatype, str) and datatype in datatypes.TEMPORAL_DATATYPES


def _field_role(field, stash, datatype, raw_type, marks_dimensions, label):
    """Decide which of Solid's roles a field takes: DIMENSION, FACT or METRIC.

    In order:

    1. A `dimension` block, or a role import stashed. The block wins over a stashed
       fact or metric, since adding one is an edit made in Apache Ossie. A missing block
       does not override a stashed dimension: the block is optional, and a tool that
       does not read it may drop it, as a missing `datatype` does not wipe a stashed type.
    2. The datatype, by Solid's own rule: numeric types are facts, everything else a
       dimension (datatypes.is_solid_fact_type).
    3. With no datatype either, the producer's convention. A model that marks its
       dimensions explicitly leaves the block off the fields that are not dimensions,
       so an unmarked field is a fact; otherwise it is a dimension, as an untyped column
       is in Solid.
    """
    stashed = stash.get("role")
    if stashed not in (DIMENSION, FACT, METRIC):
        stashed = None
    if _dimension_block(field) is not None:
        if stashed in (FACT, METRIC):
            warn("field", f"'{label}' was given a dimension block in Apache Ossie, so it "
                          f"is exported as a dimension rather than Solid's original "
                          f"{stashed}")
        return DIMENSION
    if stashed:
        return stashed
    if datatype is not None:
        return FACT if datatypes.is_solid_fact_type(raw_type) else DIMENSION
    return FACT if marks_dimensions else DIMENSION


def _declared_datatype(field, label):
    """The field's `datatype`, or None when it is absent or not one this converter knows.

    An unknown name is reported and ignored rather than rejected, so a model written
    against a later spec version that adds a datatype still converts.
    """
    datatype = as_str(field.get("datatype"), f"Field '{label}': datatype")
    if datatype is not None and datatype not in datatypes.OSSIE_DATATYPES:
        warn("field", f"'{label}': datatype '{datatype}' is not one this converter "
                      f"knows, so it was ignored")
        return None
    return datatype


def _recorded_type(datatype, stash, recorded_dialect, label):
    """The raw warehouse type import stashed, while the field's `datatype` still agrees.

    The portable `datatype` is lossy, so the stashed type is what Solid gets back. If
    the `datatype` was changed in Apache Ossie, the stashed type describes a column that
    is no longer there, and the type is derived from the edit instead. A missing
    `datatype` is not read as an edit: the field is optional, and a tool that does not
    know it may drop it, which should not wipe every Solid type.
    """
    stashed = clean_text(stash.get("type"))
    if not stashed or datatype is None:
        return stashed
    if datatypes.to_ossie_datatype(stashed, recorded_dialect) == datatype:
        return stashed
    warn("field", f"'{label}': datatype was changed to {datatype} in Apache Ossie, so "
                  f"the recorded warehouse type '{stashed}' no longer applies and the "
                  f"type was derived from the new datatype")
    return None


def _solid_name(name, stash, scope, label):
    """The Solid name of a column or metric that import renamed.

    Solid can declare a name twice where Apache Ossie needs it unique, so import renames
    the duplicate (`status` -> `status_2`) and stashes both names. The original is
    restored while the object still carries the name import gave it; a name changed
    again in Apache Ossie is kept.
    """
    if stash.get("renamed") is None:
        return name
    renamed = as_mapping(stash["renamed"], f"{label}: stashed renamed")
    original = as_str(renamed.get("from"), f"{label}: stashed renamed.from")
    if not clean_text(original):
        return name
    if as_str(renamed.get("to"), f"{label}: stashed renamed.to") == name:
        return original
    warn(scope, f"'{label}' was renamed in Apache Ossie; the new name was kept rather "
                f"than Solid's original '{renamed['from']}'")
    return name


def _convert_relationships(relationships, by_name, dropped=frozenset(), aliases=None):
    aliases = aliases or {}
    converted = []
    for relationship in relationships:
        if not isinstance(relationship, dict):
            raise ConversionError("Each entry of 'relationships' must be a mapping")
        name = relationship.get("name") or "<unnamed>"
        warn_unknown_keys(
            relationship, _RELATIONSHIP_KEYS, "relationship", f"'{name}'", _OSSIE)
        source = require_str(relationship, "from", f"relationship '{name}'")
        target = require_str(relationship, "to", f"relationship '{name}'")
        for end in (source, target):
            if end not in by_name:
                raise ConversionError(
                    f"Relationship '{name}' references dataset '{end}', which is not "
                    f"declared in 'datasets'")
        source_columns = string_list(
            relationship.get("from_columns"), f"relationship '{name}': from_columns")
        target_columns = string_list(
            relationship.get("to_columns"), f"relationship '{name}': to_columns")
        if not source_columns or not target_columns:
            raise ConversionError(
                f"Relationship '{name}' is missing from_columns/to_columns")
        if len(source_columns) != len(target_columns):
            raise ConversionError(
                f"Relationship '{name}' has {len(source_columns)} from_columns but "
                f"{len(target_columns)} to_columns; they must correspond positionally")
        gone = [end for end in dict.fromkeys((source, target)) if end in dropped]
        if gone:
            warn("relationship", f"'{name}' joins {' and '.join(gone)}, whose source is "
                                 f"a SQL query Solid cannot hold, so it was dropped")
            continue
        undeclared = [
            f"{end}.{column}"
            for end, columns in ((source, source_columns), (target, target_columns))
            for column in _undeclared(columns, by_name[end].get("fields") or [])
        ]
        if undeclared:
            warn("relationship", f"'{name}' joins on {', '.join(undeclared)}, which "
                                 f"{'is not a declared field' if len(undeclared) == 1 else 'are not declared fields'}"
                                 f", so it was dropped")
            continue

        stash = read_stash(relationship, f"relationship '{name}'")
        _warn_foreign_extensions(relationship, "relationship", name)
        # A Solid relationship is just a table pair and its join keys -- it carries no
        # free text -- so any annotation on the Apache Ossie side is dropped. The
        # exception is the note import writes for a one-to-one, which is this
        # converter's own marker and means nothing to Solid.
        instructions, synonyms, examples = ai_context_parts(
            relationship.get("ai_context"), f"relationship '{name}'")
        if instructions == ONE_TO_ONE_NOTE:
            instructions = None
        dropped = [
            label
            for label, value in (("instructions", instructions),
                                 ("synonyms", synonyms),
                                 ("examples", examples))
            if value
        ]
        if dropped:
            warn("relationship",
                 f"'{name}': ai_context.{', ai_context.'.join(dropped)} "
                 f"{'have' if len(dropped) > 1 else 'has'} no Solid equivalent -- a "
                 f"Solid relationship carries only its tables and join keys -- and "
                 f"{'were' if len(dropped) > 1 else 'was'} dropped")
        if stash.get("flipped") is True:
            # Import flipped this pair to put the many side on `from`; restore the
            # left/right order Solid wrote.
            left, right = target, source
            left_columns, right_columns = target_columns, source_columns
        else:
            left, right = source, target
            left_columns, right_columns = source_columns, target_columns

        converted.append({
            "left_table": by_name[left]["source"],
            "right_table": by_name[right]["source"],
            "join_keys": {
                "left": _column_names(left_columns, aliases.get(left, {})),
                "right": _column_names(right_columns, aliases.get(right, {})),
            },
        })
    return converted


def _convert_metrics(metrics, by_name, dialect, scope=None, taken=None,
                     dropped=frozenset(), aliases=None):
    """Convert model-scoped metrics, or with `scope` (a dataset name), the
    dataset-scoped metrics that dataset declares. `taken` collects the Solid metric
    names used so far, across both. A metric reading a dataset in `dropped` (see
    _query_datasets) is dropped too. `aliases` holds each dataset's fields that are
    aliases for another column (see _convert_model); a metric reads the column."""
    aliases = aliases or {}
    renames = {d: {f: text for f, (_, text) in fields.items()}
               for d, fields in aliases.items()}
    dataset_names = list(by_name)
    # Every dataset sharing a source, so a table a metric is bound to maps back to the
    # datasets whose fields it could read.
    by_source = {}
    for dataset_name, dataset in by_name.items():
        by_source.setdefault(dataset["source"].lower(), []).append(dataset_name)
    dropped_sources = {by_name[d]["source"].lower() for d in dropped}
    taken = set() if taken is None else taken
    # Each dataset's field names, so unqualifying can tell when a bare name would
    # belong to more than one of the datasets a metric reads, plus the columns its
    # alias fields read, which is what an expression names once they are renamed.
    fields = {
        name: [f["name"] for f in dataset.get("fields") or []
               if isinstance(f, dict) and isinstance(f.get("name"), str)]
        + [column for column, _ in aliases.get(name, {}).values()]
        for name, dataset in by_name.items()
    }
    converted = []
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ConversionError("Each entry of 'metrics' must be a mapping")
        name = require_str(metric, "name", "metric")
        label = f"{scope}.{name}" if scope else name
        warn_unknown_keys(metric, _METRIC_KEYS, "metric", f"'{label}'", _OSSIE)
        stash = read_stash(metric, f"metric '{label}'")
        _warn_foreign_extensions(metric, "metric", label)
        solid_name = _solid_name(name, stash, "metric", label)
        if scope and solid_name.lower() in taken:
            # Apache Ossie names a dataset-scoped metric `dataset.metric`, so it may
            # share a name with another dataset's metric or a model-scoped one; Solid's
            # one list cannot hold both under one name.
            base = renamed = f"{scope}_{solid_name}"
            suffix = 2
            while renamed.lower() in taken:
                renamed, suffix = f"{base}_{suffix}", suffix + 1
            warn("metric", f"'{label}' shares its name with another metric, so it was "
                           f"renamed to '{renamed}'")
            solid_name = renamed
        taken.add(solid_name.lower())
        # Solid types a metric by evaluating its formula against the warehouse, so its
        # YAML has no slot for a declared result type.
        if metric.get("datatype"):
            warn("metric", f"'{label}': datatype '{metric['datatype']}' has no Solid "
                           f"equivalent and was dropped")

        expression, fallback = pick_expression(
            metric.get("expression"), dialect, f"Metric '{label}'")
        if expression is None:
            raise ConversionError(
                f"Metric '{label}' has no {readable_dialects(dialect)} expression")
        if fallback and dialect != DIALECT_ANSI:
            warn("metric", f"'{label}' has no {dialect} expression; the {fallback} "
                           f"one was used")

        renamed_expression, status = rename_fields(expression, dialect, renames, scope)
        if status in (UNPARSED, AMBIGUOUS, SCOPED):
            warn("metric", f"'{label}' reads a field that is an alias for another "
                           f"column, and the reference could not be rewritten safely "
                           f"({status}); it was left naming the field")
        expression = renamed_expression

        # Solid stores formulas against bare columns and records the owning tables
        # separately, so the dataset qualifier Apache Ossie carries is stripped back out.
        read = referenced_datasets(expression, dialect, dataset_names, fields, scope)
        referenced = [by_name[d]["source"] for d in read]
        tables = stash.get("tables")
        if tables is not None:
            tables = string_list(tables, f"metric '{label}': stashed tables")
        gone = [d for d in read if d in dropped] + [
            t for t in tables or [] if t.lower() in dropped_sources]
        if gone:
            warn("metric", f"'{label}' reads {', '.join(gone)}, whose source is a SQL "
                           f"query Solid cannot hold, so the metric was dropped")
            taken.discard(solid_name.lower())
            continue
        if scope:
            # Its own dataset, whether or not the expression names it: a dataset-scoped
            # expression reads its fields unqualified.
            own = by_name[scope]["source"]
            others = [t for t in referenced if t.lower() != own.lower()]
            if others:
                warn("metric", f"'{label}' is declared on '{scope}' but reads "
                               f"{', '.join(others)}; a dataset-scoped metric may read "
                               f"only its own dataset's fields")
            tables = [own, *others]
        elif tables is None:
            tables = referenced
        elif not ({t.lower() for t in referenced} <= {t.lower() for t in tables}):
            # Import stashes `tables` only for an expression that does not name them
            # all, so the stash may list more than the expression reads, never other
            # tables. An expression reading one the stash lacks was edited in Apache
            # Ossie, and binding it to the stashed tables would join the wrong one.
            warn("metric", f"'{label}' reads {', '.join(referenced)}, which the tables "
                           f"recorded at import ({', '.join(tables)}) do "
                           f"not include, "
                           f"so its expression was edited in Apache Ossie; the tables "
                           f"were taken from the expression")
            tables = referenced
        # The datasets behind every table the metric ends up bound to, not just those
        # its expression names: a stripped qualifier must not leave a bare name that
        # one of them also has.
        bound = [d for t in tables for d in by_source.get(t.lower(), ())]
        bare, status = unqualify_metric(
            expression, dialect, dataset_names, fields, scope, bound)
        if status in (UNPARSED, AMBIGUOUS):
            warn("metric", f"'{label}': the dataset qualifiers could not be removed "
                           f"safely ({status}), so the expression was left as written; "
                           f"Solid stores formulas with bare column names")
        elif status == SCOPED:
            warn("metric", f"'{label}' has a subquery or lambda, where a column stripped "
                           f"of its dataset qualifier could bind to the subquery's table "
                           f"or the lambda's parameter, so the expression was left as "
                           f"written; Solid stores formulas with bare column names")
        if not tables:
            warn("metric", f"'{label}' names no table; Solid needs at least one to "
                           f"resolve its columns")

        solid_metric = {"name": solid_name}
        description = clean_text(metric.get("description"), f"metric '{label}': description")
        if description:
            solid_metric["description"] = description
        solid_metric["expression"] = bare
        instructions, synonyms, examples = ai_context_parts(
            metric.get("ai_context"), f"metric '{label}'")
        if instructions:
            warn("metric", f"'{label}': ai_context.instructions has no Solid equivalent "
                           f"and was dropped")
        if synonyms:
            solid_metric["synonyms"] = synonyms
        if examples:
            warn("metric", f"'{label}': ai_context.examples has no Solid equivalent and "
                           f"was dropped")
        solid_metric["tables"] = list(tables)
        converted.append(solid_metric)
    return converted


__all__ = ["convert_ossie_to_solid"]
