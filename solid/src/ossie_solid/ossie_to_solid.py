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
    string_list,
    warn,
    warn_unknown_keys,
)
from .expressions import (
    AMBIGUOUS,
    UNPARSED,
    is_column_reference,
    referenced_datasets,
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
    "fields", "custom_extensions",
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
    solid["business_context"] = business_context
    if entries:
        solid["custom_instructions"] = entries
    if llm_description:
        solid["model_llm_description"] = llm_description

    # Import derived each field's `datatype` from its raw type in the dialect it recorded,
    # so that is the dialect to check the two still agree in (see _recorded_type).
    stashed_dialect = clean_text(stash.get("dialect"))
    recorded_dialect = (datatypes.normalize_dialect(stashed_dialect)
                        if stashed_dialect else resolved_dialect)
    solid["tables"] = [
        _convert_dataset(d, resolved_dialect, recorded_dialect, marks_dimensions)
        for d in datasets
    ]

    metrics = _convert_metrics(
        as_list(ossie.get("metrics"), "model: metrics"), by_name, resolved_dialect)
    if metrics:
        solid["metrics"] = metrics
    relationships = _convert_relationships(
        as_list(ossie.get("relationships"), "model: relationships"), by_name)
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

    primary_key = string_list(dataset.get("primary_key"), f"dataset '{name}': primary_key")
    if primary_key:
        # Solid stores a composite key as one comma-joined scalar.
        table["primary_key"] = PK_SEPARATOR.join(primary_key)
    unique_keys = as_list(dataset.get("unique_keys"), f"dataset '{name}': unique_keys")
    if unique_keys and unique_keys != [primary_key]:
        warn("dataset", f"'{name}': unique_keys have no Solid equivalent and were "
                        f"dropped")
    # Solid emits `quality_rank` on every table, empty when the rank is unset.
    table["quality_rank"] = clean_text(stash.get("quality_rank")) or ""
    indexes = string_list(stash.get("indexes"), f"dataset '{name}': stashed indexes")
    if indexes:
        table["indexes"] = indexes

    dimensions, facts = [], []
    for field in as_list(dataset.get("fields"), f"dataset '{name}': fields"):
        if not isinstance(field, dict):
            raise ConversionError(f"Dataset '{name}': each field must be a mapping")
        column, is_dimension = _convert_field(
            field, dialect, recorded_dialect, name, marks_dimensions)
        (dimensions if is_dimension else facts).append(column)
    if dimensions:
        table["dimensions"] = dimensions
    # Unlike `dimensions`, Solid always emits `facts` -- as `[]` when a table has none.
    table["facts"] = facts
    return table


def _convert_field(field, dialect, recorded_dialect, dataset_name, marks_dimensions):
    name = require_str(field, "name", f"field in '{dataset_name}'")
    warn_unknown_keys(field, _FIELD_KEYS, "field", f"'{dataset_name}.{name}'", _OSSIE)
    stash = read_stash(field, f"field '{dataset_name}.{name}'")
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
    is_metric_fact = role == METRIC or (
        role == FACT and stash.get("role") is None and raw_type is None and computed)
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
    return column, is_dimension


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


def _convert_relationships(relationships, by_name):
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
            "join_keys": {"left": left_columns, "right": right_columns},
        })
    return converted


def _convert_metrics(metrics, by_name, dialect):
    dataset_names = list(by_name)
    # Each dataset's field names, so unqualifying can tell when a bare name would
    # belong to more than one of the datasets a metric reads.
    fields = {
        name: [f["name"] for f in dataset.get("fields") or []
               if isinstance(f, dict) and isinstance(f.get("name"), str)]
        for name, dataset in by_name.items()
    }
    converted = []
    for metric in metrics:
        if not isinstance(metric, dict):
            raise ConversionError("Each entry of 'metrics' must be a mapping")
        name = require_str(metric, "name", "metric")
        warn_unknown_keys(metric, _METRIC_KEYS, "metric", f"'{name}'", _OSSIE)
        stash = read_stash(metric, f"metric '{name}'")
        _warn_foreign_extensions(metric, "metric", name)
        solid_name = _solid_name(name, stash, "metric", name)
        # Solid types a metric by evaluating its formula against the warehouse, so its
        # YAML has no slot for a declared result type.
        if metric.get("datatype"):
            warn("metric", f"'{name}': datatype '{metric['datatype']}' has no Solid "
                           f"equivalent and was dropped")

        expression, fallback = pick_expression(
            metric.get("expression"), dialect, f"Metric '{name}'")
        if expression is None:
            raise ConversionError(
                f"Metric '{name}' has no {readable_dialects(dialect)} expression")
        if fallback and dialect != DIALECT_ANSI:
            warn("metric", f"'{name}' has no {dialect} expression; the {fallback} "
                           f"one was used")

        # Solid stores formulas against bare columns and records the owning tables
        # separately, so the dataset qualifier Apache Ossie carries is stripped back out.
        referenced = [
            by_name[d]["source"]
            for d in referenced_datasets(expression, dialect, dataset_names, fields)
        ]
        tables = stash.get("tables")
        if tables is not None:
            tables = string_list(tables, f"metric '{name}': stashed tables")
        if tables is None:
            tables = referenced
        elif not ({t.lower() for t in referenced} <= {t.lower() for t in tables}):
            # Import stashes `tables` only for an expression that does not name them
            # all, so the stash may list more than the expression reads, never other
            # tables. An expression reading one the stash lacks was edited in Apache
            # Ossie, and binding it to the stashed tables would join the wrong one.
            warn("metric", f"'{name}' reads {', '.join(referenced)}, which the tables "
                           f"recorded at import ({', '.join(tables)}) do "
                           f"not include, "
                           f"so its expression was edited in Apache Ossie; the tables "
                           f"were taken from the expression")
            tables = referenced
        bare, status = unqualify_metric(expression, dialect, dataset_names, fields)
        if status in (UNPARSED, AMBIGUOUS):
            warn("metric", f"'{name}': the dataset qualifiers could not be removed "
                           f"safely ({status}), so the expression was left as written; "
                           f"Solid stores formulas with bare column names")
        if not tables:
            warn("metric", f"'{name}' names no table; Solid needs at least one to "
                           f"resolve its columns")

        solid_metric = {"name": solid_name}
        description = clean_text(metric.get("description"), f"metric '{name}': description")
        if description:
            solid_metric["description"] = description
        solid_metric["expression"] = bare
        instructions, synonyms, examples = ai_context_parts(
            metric.get("ai_context"), f"metric '{name}'")
        if instructions:
            warn("metric", f"'{name}': ai_context.instructions has no Solid equivalent "
                           f"and was dropped")
        if synonyms:
            solid_metric["synonyms"] = synonyms
        if examples:
            warn("metric", f"'{name}': ai_context.examples has no Solid equivalent and "
                           f"was dropped")
        solid_metric["tables"] = list(tables)
        converted.append(solid_metric)
    return converted


__all__ = ["convert_ossie_to_solid"]
