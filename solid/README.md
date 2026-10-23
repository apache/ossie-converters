<!--
  Licensed to the Apache Software Foundation (ASF) under one
  or more contributor license agreements.  See the NOTICE file
  distributed with this work for additional information
  regarding copyright ownership.  The ASF licenses this file
  to you under the Apache License, Version 2.0 (the
  "License"); you may not use this file except in compliance
  with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing,
  software distributed under the License is distributed on an
  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
  KIND, either express or implied.  See the License for the
  specific language governing permissions and limitations
  under the License.
-->

# Apache Ossie Solid Converter

Bidirectional, offline conversion between an [Apache Ossie](https://github.com/apache/ossie)
semantic model and a [Solid](https://www.getsolid.ai/) semantic model YAML export. No
Solid connection required.

- **Import** (`ossie-solid import`): Solid → Apache Ossie. Solid features Apache Ossie
  has no native field for are preserved in `custom_extensions[SOLID]`, so
  `Solid → Apache Ossie → Solid` is lossless.
- **Export** (`ossie-solid export`): Apache Ossie → Solid, in the key order
  Solid's own exporter uses.

Solid is an AI analytics platform whose semantic layer is authored against a warehouse
catalog and consumed by its text-to-SQL agents. Its export is the YAML you get from
**Download Solid Semantic Model YAML**, and the same document Solid stores as a semantic
layer version.

On **export** (Apache Ossie → Solid), Apache Ossie constructs with no Solid slot —
`unique_keys`, field `label`, computed fields, an explicit `dimension.is_time` the
datatype does not imply, metric `datatype` and `ai_context.instructions`, relationship
`ai_context`, foreign-vendor `custom_extensions`, model-level `ai_context.synonyms`,
`ai_context.examples` below the model level, and any key outside the spec — are
**dropped with a warning**. On **import** (Solid → Apache Ossie), Solid-only features are
instead **preserved** in `custom_extensions[SOLID]`, and a key Solid's export does not
write is reported. Where a stashed value stands in for an Apache Ossie field, export
uses it only while that field still holds what import wrote there: **an edit made in
Apache Ossie wins**, with a warning. Any input that breaks a
[requirement](#requirements) **raises a `ConversionError`** — the converter never drops
information silently or produces an invalid result. (A dataset's or relationship's
`name` only identifies it within Apache Ossie; Solid has no slot for either, and import
derives them again.)

## Installation

```bash
pip install apache-ossie-solid        # once published to PyPI
```

Or, from a checkout of this directory:

```bash
pip install -e .
```

Runtime dependencies are `PyYAML` and `sqlglot`. Python 3.11+.

## Usage

### Command line

```bash
ossie-solid import -i solid_model.yaml -o model.yaml [--dialect SNOWFLAKE]
```

```bash
ossie-solid export -i model.yaml -o solid_model.yaml [--dialect SNOWFLAKE]
```

With no `-o`, output goes to stdout; warnings always go to stderr. `--name` overrides the
model name in either direction. `--dialect` is described under
[Dialect resolution](#dialect-resolution).

### Python API

```python
from ossie_solid import convert_solid_to_ossie, convert_ossie_to_solid

ossie_yaml = convert_solid_to_ossie(solid_yaml_str)                      # or dialect="SNOWFLAKE"
solid_yaml = convert_ossie_to_solid(ossie_yaml_str, model_name="sales")
```

## Dialect resolution

**Solid's YAML does not record which warehouse a model came from.** The source system is
held on the asset row in Solid's own database and is dropped when the YAML is rendered,
but Apache Ossie requires a dialect on every expression. The converter resolves one in
this order:

1. **`--dialect`**, when given. Always wins.
2. **Inference from the column type vocabulary.** Solid copies each column's `type` out
   of the catalog, so among the warehouses with an Apache Ossie dialect the names
   identify it: `NUMBER`/`TEXT` mean Snowflake, `LONG`/`MAP`/`BIGINT` mean Databricks,
   `INT64`/`FLOAT64`/`BOOL` mean BigQuery. Only names one of the three alone reports
   vote; shared ones (`STRING`, `BOOLEAN`, `DATE`, `TIMESTAMP_NTZ`, `VARIANT`,
   `GEOGRAPHY`) are ignored.
3. **`ANSI_SQL`**, with a warning, when nothing votes, two warehouses tie, or a type name
   only Oracle or SQL Server reports (`VARCHAR2`, `DATETIME2`, `UNIQUEIDENTIFIER` …)
   appears.

Solid also reads from warehouses Apache Ossie has no dialect for — Oracle, SQL Server,
Teradata, Starburst, Fabric, Athena and Vertica. Oracle and SQL Server are recognized
and fall back to `ANSI_SQL`; for the others inference may pick a dialect whose type
names they share (Starburst's `BIGINT` reads as Databricks), so pass `--dialect
ANSI_SQL` explicitly.

The resolved dialect is recorded in `custom_extensions[SOLID]`, so an export reads
expressions back in the dialect they were written in without being told again.

On export, `--dialect` selects which expression dialect to *read*; without it the
converter uses the dialect recorded at import, then the single vendor dialect the model's
expressions use. When the chosen dialect is missing from an expression, its portable form
is read instead — `ANSI_SQL`, else `OSSIE_SQL_2026`, the spec's portable dialect built on
ANSI SQL:2003 Core — with a warning naming which one was used. A model written only in
`ANSI_SQL` and/or `OSSIE_SQL_2026` resolves to `ANSI_SQL` and reads either form silently.
A field with neither the chosen dialect nor a portable form raises.

**Only SQL dialects are read.** Apache Ossie's dialect enum also covers expression
languages that are not SQL — `MDX`, `TABLEAU`, `MAQL` — which this converter cannot
parse, qualify or unqualify. One of those never becomes the resolved dialect: the model
is reported, and each expression's portable (`ANSI_SQL` or `OSSIE_SQL_2026`) form is
read instead. An expression offering only a non-SQL dialect raises rather than passing a
formula Solid cannot execute into a SQL field. (This is what lets GoodData's
`MAQL`-and-`ANSI_SQL` models convert on their SQL side.)

## Mapping

Each row maps in both directions; the **Notes** flag where a behavior is specific to
**import** (Solid → Apache Ossie) or **export** (Apache Ossie → Solid).

### Model

| Apache Ossie | Solid | Notes |
|---|---|---|
| `name` (at the document root) | `semantic_model.name` | Both formats hold one model per document. Export also reads the legacy layout that wraps models in a `semantic_model` list (Apache Ossie `0.1.1`), with a warning, and warns if that list holds more than one model. |
| `description` | `model_llm_description` | Import falls back to `business_context.model_description` when the model has no LLM description, and stashes both. An edit made in Apache Ossie replaces whichever one `description` was written from. |
| `ai_context.instructions` | `business_context.custom_instructions` and the root `custom_instructions` list | Solid holds free text in `business_context` and, in its structured form, a list of titled entries, each enabled or not and optionally scoped by a condition and to catalog assets. Import renders the free text and then each enabled entry (title, condition, assets, content) into one text, resolving Solid's `@<assetlink …>` markup to the display names it carries, and stashes the originals, so an export restores both and the live catalog references. If the instructions were edited in Apache Ossie, the edited text is used instead, as free text. |
| `ai_context.examples` | `business_context.business_questions` | |
| `custom_extensions[SOLID]` | `business_context.model_description`, the `custom_instructions` entries, `example_queries`, `benchmark_questions` | No Apache Ossie core equivalent. |

### Datasets

| Apache Ossie | Solid | Notes |
|---|---|---|
| `dataset.source` | `tables[].name` | Solid names a table by its `catalog.schema.table`. Import rejects a table listed twice. |
| `dataset.name` | — | Derived on import from the last part of the FQN, widening to `schema_table` and then the whole FQN if a shorter form is taken. Apache Ossie dataset names double as expression qualifiers, so a dotted name is not usable, and names are kept unique ignoring case, as an unquoted SQL qualifier is. |
| `description` | `description` | Solid's AI-written description. |
| `ai_context.instructions` | `manual_description` | Solid's human-written annotation. |
| `ai_context.synonyms` | `synonyms` | |
| `primary_key` (array) | `primary_key` (scalar) | Solid joins a composite key with `", "` into one scalar; import splits it, export rejoins it. |
| `custom_extensions[SOLID]` | `quality_rank`, `indexes` | No Apache Ossie core equivalent. Export emits `quality_rank` even when unknown, as Solid does. |

### Fields

| Apache Ossie | Solid | Notes |
|---|---|---|
| `fields[]` | `dimensions[]` + `facts[]` | Solid splits columns two ways; Apache Ossie has one list. See [Dimensions and facts](#dimensions-and-facts). |
| `dimension.is_time` | — | Set explicitly on import from the resolved `datatype`, rather than left to the spec's default, so a consumer that does not implement the default still reads the same role. |
| `expression.dialects[]` | the column's own name | A Solid column's expression is a bare reference to itself. See [Identifier quoting](#identifier-quoting). |
| `datatype` | `type` | See [Data types](#data-types). |
| `description` | `description` | |
| `ai_context.instructions` | `manual_description` | |
| `ai_context.synonyms` | `synonyms` | |
| `custom_extensions[SOLID]` | `type` (raw), `sample_values`, role | The raw warehouse type is always kept, since the portable `datatype` is lossy; the role is described under [Dimensions and facts](#dimensions-and-facts). |

### Dimensions and facts

Import stashes each column's Solid role — `dimension`, `fact`, or `metric` — and also
writes a `dimension` block on every dimension, so other tools read the same split.
Export decides each field's role in this order:

1. **A `dimension` block, or the stashed role.** A block makes the field a dimension, and
   wins over a stashed `fact` or `metric` with a warning, since adding one is an edit
   made in Apache Ossie. A missing block does not override a stashed `dimension`: the
   block is optional, and a tool that does not read it may drop it.
2. **The `datatype`**, by Solid's type rule: numeric types are facts, anything else a
   dimension. This is the type-prefix part of solid-server's split, which is a prefix
   match, so `BIGINT`, `SMALLINT` and `BIGNUMERIC` are dimensions there too. Solid also
   weighs column names and cardinality, which an offline converter cannot see.
3. **With no `datatype` either, the producer's convention.** Some converters (GoodData,
   OrionBelt, Cube) mark every dimension, so their unmarked fields are measures and
   become facts. Others (Omni, Microsoft) mark only time fields; there an unmarked,
   untyped field cannot be shown to be numeric and becomes a dimension, as an untyped
   column is in Solid. A model marks its dimensions when any field that is not a time
   dimension carries a block.

A Solid **fact may be a table-scoped metric** rather than a catalog column — it carries an
`expression` and no `type`. Its role is `metric`, and export re-emits it as an
expression-only fact. A hand-edited fact carrying both a `type` and an `expression` is
marked the same way and keeps both on export. A **dimension with an `expression`**,
which Solid's own export never writes but a hand-edited model can, is stashed as
`computed` and keeps its expression on export too. Any other computed field is reported
and exported as a plain column, since a Solid column is a catalog column.

A Solid table **may declare a column name twice**, where Apache Ossie needs field names
unique within a dataset. Import renames the duplicate (`status` → `status_2`) with a
warning and stashes both names; export restores `status`, so the Solid model names a
column the catalog has. A field renamed again in Apache Ossie keeps its new name.

### Relationships

| Apache Ossie | Solid | Notes |
|---|---|---|
| `from` / `to` | `left_table` / `right_table` | See [Cardinality](#cardinality). |
| `from_columns` / `to_columns` | `join_keys.left` / `join_keys.right` | Positional; import rejects a length mismatch. |
| `name` | — | Solid relationships are unnamed. Import generates `{from}_to_{to}`, suffixed if that collides. |
| `ai_context` | — | A Solid relationship is a table pair and its join keys, with no free text. Export warns — except for the one-to-one note import itself writes, which is this converter's own marker and means nothing to Solid. |
| `custom_extensions[SOLID]` | — | Records when import flipped a pair to put the many side on `from`, so an export restores Solid's left/right orientation. Relationships keep their list position in both directions. |

### Metrics

| Apache Ossie | Solid | Notes |
|---|---|---|
| `metrics[]` | `metrics[]` | Solid metrics are already model-level. |
| `name` | `name` | A name Solid declares twice is renamed on import and restored on export, as for fields. |
| `expression.dialects[]` | `expression` + `tables[]` | See [Metric expressions](#metric-expressions). |
| `description` | `description` | |
| `datatype` | — | Solid types a metric by evaluating its formula against the warehouse, so there is no slot for a declared result type. Export warns. |
| `ai_context.synonyms` | `synonyms` | |
| `custom_extensions[SOLID]` | `tables[]` | Stashed only when the qualified expression does not already name the same datasets — for a cross-table metric, or one referencing no column at all (`COUNT(*)`). If the expression was edited in Apache Ossie to read a dataset the stash does not list, export takes the tables from the expression instead. |

## Cardinality

Apache Ossie encodes cardinality through direction — `from` is the many side, `to` the one
side — but a Solid relationship is an undirected pair of column lists. Import recovers the
direction from the primary keys:

| Condition | Result |
|---|---|
| The right table's primary key is exactly its join columns | `from` = left, `to` = right |
| The left table's primary key is exactly its join columns | `from` = right, `to` = left (recorded as `flipped`) |
| Both | One-to-one: direction is arbitrary, and a note is added to the relationship's `ai_context` |
| Neither | `from` = left, with a warning — Solid records no cardinality and its `unique_keys` are not exported |

## Metric expressions

Solid stores a metric formula against **bare** column names and records the owning table
separately; `is_valid_metric_formula` in solid-server actively strips alias prefixes
before saving. Apache Ossie expects a metric to be self-contained, qualifying each column
with its dataset. So:

- **Import** prefixes each bare column with its dataset when `tables[]` names exactly one
  table (`SUM(ss_ext_sales_price)` → `SUM(store_sales.ss_ext_sales_price)`). A path into a
  STRUCT column is qualified at its head — `COUNT(DISTINCT customer.id)` becomes
  `COUNT(DISTINCT orders.customer.id)` when `customer` is a column of `orders` — since
  left as it was, an Apache Ossie reader would take `customer` for a dataset.
- **Export** strips those qualifiers back out — unless that would leave a bare name more
  than one of the metric's datasets owns. `SUM(orders.amount) - SUM(returns.amount)`
  would become `SUM(amount) - SUM(amount)`, and `COUNT(customers.id)` beside
  `orders.amount` would leave an `id` that `orders` has too. Such an expression is kept
  as written, with a warning.

Both edits are a **surgical splice**, not a re-render. Each column is located by
sqlglot's tokenizer, which reports byte offsets and keeps string literals and quoted
identifiers in token types of their own, and the qualifier is inserted or removed at those
offsets. Everything else is left byte-for-byte intact — round-tripping a parsed tree
through `Expression.sql()` would canonicalize as it generates, turning
`CAST(x AS FLOAT)` into `CAST(x AS DOUBLE)` and `EXTRACT(year FROM d)` into
`DATE_PART(YEAR, d)`, and a converter has no business rewriting SQL it was only asked to
qualify.

The parser is still used as a cross-check: it decides which names are genuinely bare
column references, and a token scan that disagrees with it means some occurrence is
something else. On any disagreement — an unparseable expression, or a column also named
`year` in `EXTRACT(year FROM …)` — the expression is left **exactly as written** and a
warning is emitted. An unqualified metric is a far smaller problem than a corrupted one.

**A cross-table metric cannot be qualified.** Solid's `tables[]` may hold more than one
table, and the alias-to-table binding lives in `metric.column_ids`, which its YAML export
does not carry. Those metrics keep their bare column names, keep `tables[]` in the stash,
and emit a warning.

## Identifier quoting

A column name, or a dataset name used as a metric qualifier, is written bare unless it
is not a regular identifier (`odd name`) or is a reserved word in the resolved dialect,
in which case it is quoted with that dialect's rules: `"order"` on Snowflake and
`ANSI_SQL`, `` `order` `` on Databricks and BigQuery. The reserved words come from each
warehouse's documentation, since sqlglot reads `order` and `group` back as ordinary
columns; Snowflake's is solid-server's list less the `SHOW`-only words.

On Snowflake a quoted name is case-sensitive while a bare one folds to upper case. So
quoting is kept to the words Snowflake's documentation says cannot be a column or a
table alias (not the eight it restricts only in `SHOW` commands, such as `ACCOUNT` and
`SCHEMA`), and a reserved name is quoted in upper case: `order` and `ORDER` are both
written `"ORDER"`, the column the name would mean written bare, as every other name in
the model and Solid's own formulas are. `COMMENT`, which Snowflake does not reserve,
stays bare. Solid reports names as the catalog stores them, upper case for a column
created unquoted, so for its own exports this is the name exactly as reported. Databricks
and BigQuery match a quoted name case-insensitively, so their lists also take in the SQL
standard's reserved words.

## Data types

Solid stores the **raw** warehouse type string verbatim (`NUMBER(38,0)`, `TEXT`, `INT64`,
`MAP<STRING, STRING>`), so mapping onto Apache Ossie's portable `datatype` is lossy —
`NUMBER(38,2)`, `NUMERIC` and `DECIMAL` all collapse to `Decimal`. The raw string is
therefore always kept in the stash and is what an export re-emits; the derived table below
is only used for an Apache Ossie model that has no stash to read, and for a field whose
`datatype` was changed in Apache Ossie, which no longer matches its stashed type. A
removed `datatype` is not read as a change: the field is optional, and a tool that does
not know it may drop it.

| Warehouse type | Apache Ossie `datatype` |
|---|---|
| `STRING`, `TEXT`, `VARCHAR`, `CHAR`, `CHARACTER VARYING`, `VARCHAR2` | `String` |
| `INT`, `BIGINT`, `LONG`, `INT64`, `TINYINT` | `Integer` |
| `NUMBER`, `NUMERIC`, `DECIMAL`, `DEC`, `BIGNUMERIC`, `BIGDECIMAL` | `Integer` when the declared scale is 0, else `Decimal` — see below |
| `FLOAT`, `FLOAT64`, `DOUBLE`, `REAL` | `Float` |
| `BOOLEAN`, `BOOL`, `BIT` | `Boolean` |
| `DATE` / `TIME` | `Date` / `Time` |
| `TIMESTAMP_NTZ`, `DATETIME`, `DATETIME2` | `DateTime` |
| `TIMESTAMP_TZ`, `TIMESTAMP_LTZ`, `DATETIMEOFFSET` | `DateTimeTz` |
| `TIMESTAMP` | `DateTime` on Snowflake, `DateTimeTz` on Databricks and BigQuery |
| `VARIANT`, `OBJECT`, `ARRAY`, `MAP`, `STRUCT`, `BINARY`, `GEOGRAPHY`, `INTERVAL …` | `Opaque` |
| anything else | omitted, with a warning |

**An exact-decimal type is an `Integer` only when its scale is known to be 0** — a
declared scale of 0 (`NUMBER(38,0)`), or a precision alone (`NUMERIC(10)`), which
declares a scale of 0 in every warehouse. A bare `NUMBER`, `NUMERIC` or `DECIMAL` is a
`Decimal`, which the spec defines as exact base-10 with unspecified precision and scale,
in every dialect, even where the warehouse's own default scale is 0. Solid copies a
column's type from the catalog's `DATA_TYPE`, which on Snowflake and Databricks never
carries the precision or scale, so a bare `NUMBER` in a Solid export is as likely
`NUMBER(38,2)` as `NUMBER(38,0)`: in the real Solid exports this was checked against,
every `NUMBER` and `DECIMAL` column is bare, rates and dollar amounts included. (BigQuery's
bare `NUMERIC` is `(38,9)` and Oracle's bare `NUMBER` a floating decimal, so they are
`Decimal` either way.)

**Going the other way**, a field with no stashed type is written as a type of the
resolved warehouse that reads back as the same `datatype`, since Solid takes it as the
column's catalog type. Exact numerics get their scale written out — `NUMBER(38,0)` for an
`Integer` on Snowflake, `NUMBER(38,9)` or `DECIMAL(38,9)` for a `Decimal` — rather than
relying on a warehouse default. The one
type that does not survive is `Time` on Databricks, which has no time-of-day type: it is
written as `STRING`. An unknown `datatype` name is ignored with a warning.

## Requirements

Conversion raises a `ConversionError` (rather than guessing or emitting something invalid)
when an input breaks one of these:

- an Apache Ossie document has no `version` (export) — see
  [Spec versions](#spec-versions);
- the input YAML is malformed, or its root is not a mapping;
- a value has the wrong YAML kind — a scalar where a list or mapping belongs, a list
  item that is itself a list or mapping, or a `custom_extensions` entry that is not a
  mapping with string `vendor_name` and `data`. A null is read as empty, since Solid
  writes an empty list as one;
- the input file is not UTF-8 (CLI);
- a Solid model has no `tables`, or an Apache Ossie model has no `datasets` — the spec
  requires at least one dataset;
- an Apache Ossie model declares two datasets with the same `name`, or names that differ
  only in case, which relationships and metrics could then resolve to the wrong table
  (export);
- a Solid model lists the same table twice, compared ignoring case, for the same reason
  (import);
- a field or metric expression lists the same dialect twice with different SQL (export);
- a relationship's `from_columns`/`to_columns` differ in length, or name a dataset that
  is not declared;
- `--dialect` names a dialect outside `ANSI_SQL`, `SNOWFLAKE`, `DATABRICKS`, `BIGQUERY`;
- an Apache Ossie document has a `semantic_model` key that is not a non-empty list of
  mappings (export);
- a field or metric has neither the selected dialect nor `ANSI_SQL`/`OSSIE_SQL_2026`
  (export);
- the recorded `custom_extensions[SOLID]` dialect is not one of the four above — which
  only a hand-edited stash can produce (export).

## Spec versions

Output is always written as **`0.2.0.dev0`**, the current draft. On export, a document
declaring `0.2.0.dev0` or **`0.1.1`** is read as such. A document declaring any other
version is read as `0.2.0.dev0`, with a warning, rather than rejected, so a spec release
does not make every document fail; a real incompatibility still surfaces when the content
is read.

`0.1.1` is accepted because it is the only *released* spec version, so it is what models
in the wild declare. It differs from `0.2.0.dev0` in one structural way: a `0.1.1`
document wraps its model in a `semantic_model` list, where a `0.2.0.dev0` document puts
one model's properties directly at the root beside `version`. Export reads both layouts,
warning when it meets the wrapper (early `0.2.0.dev0` snapshots used it too). The other
differences are additive — `datatype` on `Field` and `Metric`, `BIGQUERY` and
`OSSIE_SQL_2026` in the dialect enum, and a free-form `vendor_name` — and only the
absence of `datatype` is visible to this converter; a `0.2.0.dev0` model that simply
omits it — as most do — is handled the same way. Reading a `0.1.1` document warns,
naming that gap.

## Known differences

- **An empty description is normalized away.** solid-server renders a whitespace-only
  `manual_description` as an empty block scalar, which parses back as `''`. That carries
  no information, so the converter drops it rather than round-tripping the emptiness.
- **Leading and trailing whitespace on free text is trimmed.** solid-server writes
  descriptions as block scalars, which end in a newline; the text inside is unchanged.
- **Hand-edited spellings Solid's own export never writes come back in the form it
  does write.** A metric formula that qualifies its columns with its own table
  (`SUM(orders.amount)` on `orders`) comes back bare, as solid-server strips such
  qualifiers when a formula is saved; a composite primary key written `'id,amount'`
  comes back joined with `", "`.
- **`unique_keys` cannot survive an export.** Solid's YAML has no unique-key field; a
  single unique key identical to the primary key is dropped silently, any other is
  dropped with a warning.
- **A Solid model carries no `unique_keys` to import**, so a re-imported model relies on
  `primary_key` alone to determine relationship cardinality.

## Development

```bash
uv sync && uv run pytest
```

The suite covers both directions against three Solid fixtures — Apache Ossie's
[TPC-DS model](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml) expressed as a Solid export, plus
a Databricks and a BigQuery model — and validates every converted document against the
[Apache Ossie JSON Schema](https://github.com/apache/ossie/blob/main/core-spec/ossie-schema.json),
both read from the `ossie` submodule.

`tests/test_cross_vendor.py` covers the case the round-trip tests cannot: an Apache Ossie
model produced by *another vendor's* converter, which carries no `custom_extensions[SOLID]`
stash, no `dimension` blocks, usually no `datatype`, and metrics written against bare
column names. It sweeps every Apache Ossie document in the other converters' test data — YAML or
JSON, at any depth under their test directories — asserting that each converts to a
well-formed Solid model that re-imports schema-valid, and that every field whose
`dimension` block or `datatype` settles its role lands in `dimensions` or `facts`
accordingly. A document with an expression written only in a language that is not SQL
(Holistics AQL, ThoughtSpot formulas) is checked to raise, as documented.
That sweep deliberately pins **no** warning counts, because those fixtures belong to other
converters and each converter's CI is path-filtered to its own directory — a pin here would
break on a main-branch push from a PR that never ran this suite. The exact interop gaps are
pinned instead against
[`tests/fixtures/foreign_ossie.yaml`](tests/fixtures/foreign_ossie.yaml), a fixture this
converter owns that reproduces the same constructs.

To regenerate the expected-output fixture after an intentional change:

```bash
uv run ossie-solid import -i tests/fixtures/tpcds_solid.yaml -o tests/fixtures/tpcds_ossie.yaml
```

then re-add the license header at the top of the file.

## Future effort

Both the Apache Ossie specification and Solid's semantic model are still evolving, and
this converter will be updated to track them. Several gaps are worth naming.

Three of these are **open interop decisions**, pinned as such in
`tests/test_cross_vendor.py` rather than resolved. Each is a judgement about what Solid
should do with a foreign model, not a defect in the transform:

- **A column with no `datatype` gets an empty Solid `type`.** `datatype` is optional in
  the spec and most converters omit it, so this is the largest single gap when importing
  a foreign model — see the counts in `test_the_documented_interop_gaps_are_exactly_these`.
  It cannot be closed offline: Solid types its columns from the warehouse catalog, and
  the honest fix is to reconcile against the live catalog at import time rather than to
  guess here. What the converter owes that flow is a machine-readable list of what needs
  reconciling, which today it only reports as warnings.
- **A field renamed relative to its column loses the column it reads.** Given
  `name: ticket_number` over `expression: ss_ticket_number`, the alias survives and the
  real column does not, so the Solid model names a column the warehouse does not have.
  Emitting the underlying name and keeping the alias as a synonym would be truer, but it
  changes which identifier downstream Solid consumers see and so needs a product call.
- **A metric written against bare column names reaches Solid with `tables: []`.** Only a
  dataset-qualified reference identifies its owner. Resolving bare names against each
  dataset's declared fields would fix most cases; it is inference, so it is deliberately
  not done silently.

And two are **format-level**:

- **Verified queries have no home in the core spec.** Solid's `example_queries` and
  `benchmark_questions` are currently stashed, but the same construct appears across the
  ecosystem — Snowflake Cortex `verified_queries`, WisdomAI reviewed queries — and looks
  like a candidate for a core field rather than N vendor extensions.
- **Solid's export does not record its source warehouse**, which is why the dialect has to
  be inferred. Adding it to the export would make the format self-describing; the
  converter would read it first and keep inference only as a fallback for existing files.
