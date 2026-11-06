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
  has no native field for are preserved in `custom_extensions[SOLID]`, so a model Solid
  exported comes back unchanged from `Solid → Apache Ossie → Solid`. A model edited in
  Apache Ossie along the way is converted as edited, and anything that can't be carried
  back is reported as a warning.
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
instead **preserved** in `custom_extensions[SOLID]`. So is a key this converter has no
mapping for, such as one a later solid-server adds: it is carried unchanged and
restored on export, with a warning, since an Apache Ossie consumer cannot see it. Where
a stashed value stands in for an Apache Ossie field, export uses it only while that
field still holds what import wrote there: **an edit made in Apache Ossie wins**, with a
warning. An input the converter cannot read as a whole **raises a `ConversionError`**;
see [Errors and warnings](#errors-and-warnings). (A dataset's or relationship's `name`
only identifies it within Apache Ossie; Solid has no slot for either, and import
derives them again.)

## Guarantees

Each of these is checked by the test suite.

1. **Solid → Apache Ossie → Solid gives back the model Solid exported**, as data:
   every table, column, key, relationship, metric and model-level field, including keys
   this converter does not map. The only differences are the ones listed under
   [Known differences](#known-differences): surrounding whitespace, empty descriptions,
   and two spellings Solid's export never writes. Checked against every Solid fixture,
   and against 300 generated models shaped like solid-server's export template,
   across every warehouse's type vocabulary (`test_generated_roundtrip.py`).
2. **Every Apache Ossie document import writes is valid**: it passes the spec's JSON
   Schema, and every primary key and join column names a declared field of its dataset,
   which the spec's validator checks (apache/ossie#649).
3. **A metric is never rewritten into something with a different meaning.** Where
   adding or removing a dataset qualifier could rebind a name — a subquery, a lambda, a
   table alias, a column two of the metric's tables share, a name none of them has — the
   expression is left as written, in the warehouse dialect, with a warning.
4. **Nothing is lost silently from Apache Ossie → Solid.** That direction cannot be
   lossless: Solid's format has no slot for unique keys, labels, query-backed datasets
   and more. Every construct dropped for that reason produces a `ConversionWarning`
   naming it; the only exceptions are the ones that carry no information, listed under
   [Known differences](#known-differences).
5. **An edit made in Apache Ossie wins over the stash**, with a warning: a stashed value
   stands in for a field only while that field still holds what import wrote.
6. **What export writes, Solid's YAML import accepts.** solid-server's import rejects a
   whole model unless it names a `source_system`, and unless every table, column, key
   column and join column it names is in the warehouse catalog. Export writes
   `source_system` for a model whose SQL names a warehouse, and names only the columns
   the model's fields read: a computed field, which no catalog has, is dropped with a
   warning. Checked on the 300 generated models with their stash removed, as a model
   built in Apache Ossie would arrive. A model in portable SQL names no warehouse, so
   it is exported without `source_system`, with a warning saying to pass `--dialect`.

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

What a conversion drops or changes is reported as a `ConversionWarning`, one per
object, and an input it cannot convert raises a `ConversionError` (see
[Errors and warnings](#errors-and-warnings)). To collect the report:

```python
import warnings
from ossie_solid import ConversionWarning, convert_ossie_to_solid

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always", ConversionWarning)
    solid_yaml = convert_ossie_to_solid(ossie_yaml_str)
report = [str(w.message) for w in caught if issubclass(w.category, ConversionWarning)]
```

`simplefilter("always", …)` matters in a long-running process: under Python's default
filter, a warning identical to one already shown is not shown again.

## Dialect resolution

Apache Ossie requires a dialect on every expression. Since October 2026 Solid's export
names its warehouse as `source_system`; an older export does not, so the converter
resolves the dialect in this order:

1. **`--dialect`**, when given. Always wins.
2. **`source_system`**, when the export has one: `snowflake`, `databricks` and
   `big_query` map to their dialects. Any other warehouse (`starburst`, `redshift`,
   `postgres`, `athena`, …) has no Apache Ossie dialect this converter writes, so it reads
   as `ANSI_SQL`, with a warning naming it.
3. **Inference from the column type vocabulary**, for an export without
   `source_system`. Solid copies each column's `type` out of the catalog, so among the
   warehouses with an Apache Ossie dialect the names identify it: `NUMBER`/`TEXT` mean
   Snowflake, `LONG`/`MAP` mean Databricks, `INT64`/`FLOAT64`/`BOOL` mean BigQuery. Only
   names one of the three alone reports vote; shared ones (`STRING`, `BOOLEAN`, `DATE`,
   `TIMESTAMP_NTZ`, `VARIANT`, `GEOGRAPHY`) are ignored, and so are `BIGINT`, `SMALLINT`
   and `TINYINT`, which Postgres, Redshift, Trino and Athena report too.
4. **`ANSI_SQL`**, with a warning, when nothing votes, two warehouses tie, or a type name
   only Oracle or SQL Server reports (`VARCHAR2`, `DATETIME2`, `UNIQUEIDENTIFIER` …)
   appears.

For an older export from a warehouse Apache Ossie has no dialect for (Teradata,
Starburst, Fabric, Athena, Vertica), inference can still be misled by a type name that
warehouse shares with one of the three, so pass `--dialect ANSI_SQL` explicitly.

The resolved dialect is recorded in `custom_extensions[SOLID]`, so an export reads
expressions back in the dialect they were written in without being told again.

On export, `--dialect` selects which expression dialect to *read*; without it the
converter uses the dialect recorded at import, then the single vendor dialect the model's
expressions use. A model that did not come from Solid gets the `source_system` of that
dialect (`snowflake`, `databricks`, `big_query`), which Solid's YAML import requires; one
in portable SQL gets none, with a warning. A model that came from Solid keeps what its
export had, even none. When the chosen dialect is missing from an expression, its portable form
is read instead — `ANSI_SQL`, else `OSSIE_SQL_2026`, the spec's portable dialect built on
ANSI SQL:2003 Core — with a warning naming which one was used. The `OSSIE_SQL_2026` form
may be the expression's top-level `sql` (apache/ossie#617), which may stand alone and
which the spec makes authoritative over an `OSSIE_SQL_2026` entry in `dialects`; when
the two differ, the top-level `sql` is read, with a warning. A model written only in
`ANSI_SQL` and/or `OSSIE_SQL_2026` resolves to `ANSI_SQL` and reads either form silently.
A field with neither the chosen dialect nor a portable form raises.

**Only SQL dialects are read.** Apache Ossie's dialects also include expression
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
| `custom_extensions[SOLID]` | `business_context.model_description`, the `custom_instructions` entries, `version_number`, `source_system`, `target_system`, `model_llm_short_description`, `example_queries`, `benchmark_questions` | No Apache Ossie core equivalent. Export writes `source_system` for a model not from Solid from its dialect; see [Dialect resolution](#dialect-resolution). |

### Datasets

| Apache Ossie | Solid | Notes |
|---|---|---|
| `dataset.source` | `tables[].name` | Solid names a table by its `catalog.schema.table`. Import rejects a table listed twice. On export a quoted source (`` `ecommerce`.`orders` ``, `"DB"."S"."T"`) becomes the unquoted name Solid uses, and one with fewer than three parts is reported. Two datasets over one table (a date table joined twice) become one Solid table, the second's columns merged in, with a warning. Apache Ossie also lets `source` be a SQL query; Solid's tables come from the warehouse catalog, so export drops a query-backed dataset with a warning, along with its dataset-scoped metrics and every relationship and metric that reads it. |
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
`computed` and keeps its expression on export too. A field with no stashed role becomes
a table-scoped metric only when its expression aggregates (`SUM(amount)`); any other
computed field (`first_name || ' ' || last_name`) is dropped with a warning, together with
any key or join that names it. A Solid column is a catalog column, and Solid's YAML
import rejects a whole model that names a column the warehouse does not have.

A field that **reads another column** — `name: cust` over `expression: customer_id`, or
`amount` over Snowflake's case-sensitive `"Amount"` — is an alias, and a Solid column is
the catalog column itself. Export names the column (`customer_id`) and keeps the field's
name as one of its synonyms, with a warning, and the primary key, join keys and metric
formulas that name the field read the column instead. If another field already is that
column, the alias is dropped and its references read the column. On Snowflake and in
ANSI SQL a quoted name is case-sensitive, so `"AMOUNT"` is the field `amount` but
`"Amount"` is not.

Apache Ossie **keys and join columns must name declared fields** (enforced by the spec's
validator since apache/ossie#649), matched ignoring case. A Solid table may key or join on
a column it does not list; import declares each such column as a field, with a warning,
and marks it so export leaves it out again. On export, a primary key or relationship that
names an undeclared field is dropped with a warning rather than pointing Solid at a
column that may not exist.

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
| `metrics[]` | `metrics[]` | Solid metrics are already model-level, so import writes model-scoped metrics, which every Apache Ossie consumer reads. |
| `datasets[].metrics` | `metrics[]` with `tables: [that table]` | A dataset-scoped metric reads its dataset's fields unqualified, as a Solid formula reads columns, and is bound to that table on export. Solid holds every metric in one list, so a name it shares with another metric gets the dataset's name as a prefix, with a warning, and a re-import writes it model-scoped. |
| `name` | `name` | A name Solid declares twice is renamed on import and restored on export, as for fields. |
| `expression.dialects[]` | `expression` + `tables[]` | See [Metric expressions](#metric-expressions). |
| `description` | `description` | |
| `datatype` | — | Solid types a metric by evaluating its formula against the warehouse, so there is no slot for a declared result type. Export warns. |
| `ai_context.synonyms` | `synonyms` | |
| `custom_extensions[SOLID]` | `tables[]` | Stashed only when the qualified expression does not already name the same datasets — for a cross-table metric that reads a column more than one of its tables has, or one referencing no column at all (`COUNT(*)`). If the expression was edited in Apache Ossie to read a dataset the stash does not list, export takes the tables from the expression instead. |

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
  `orders.amount` would leave an `id` that `orders` has too. The datasets counted are
  every one the metric ends up bound to, not just those its expression names: a
  dataset-scoped metric's own dataset, and the tables import recorded. Such an
  expression is kept as written, with a warning. In a dataset-scoped metric, a path that
  starts with one of the dataset's own fields is a field access, as Metric Scoping
  requires, even when another dataset has the same name.

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

The same holds wherever adding or removing a qualifier could change what a name binds
to, or leave it unresolvable:

- **A subquery or lambda** declares names of its own. In `customer_id IN (SELECT
  customer_id FROM crm.vip)` the inner `customer_id` is `crm.vip`'s, and qualifying it
  with the metric's table would correlate the subquery to the outer row; stripping a
  qualifier inside one can likewise bind a column to the subquery's table or the
  lambda's parameter. Neither direction rewrites such an expression.
- **A qualifier that is not a dataset** — a table alias (`p.amount`) or the table's full
  name — is something an Apache Ossie reader cannot resolve, so import leaves the
  expression as written and says so, rather than qualifying the rest of it.
- **A bare name none of the metric's tables has** (another metric, or a column the
  model does not list) cannot be qualified, so the expression is not half-qualified.
- **A name only its qualifier made a column**: bare, `current_date` is the CURRENT_DATE
  function. Export checks that its result reads as many columns as the original did,
  quotes such a name the dialect's way when it does not, and otherwise keeps the
  original.

**A cross-table metric is qualified column by column.** Apache Ossie requires a
model-scoped metric to qualify every field as `dataset.field`, and a Solid metric may list
several tables. A column only one of them has can only be that table's, so
`SUM(ss_ext_sales_price) / COUNT(DISTINCT c_customer_sk)` over `store_sales` and
`customer` becomes `SUM(store_sales.ss_ext_sales_price) / COUNT(DISTINCT
customer.c_customer_sk)`. A metric that reads a column more than one of its tables has
cannot be attributed from the file — solid-server keeps that binding in
`metric.column_ids`, which its YAML export does not carry — so it keeps its bare column
names, keeps `tables[]` in the stash, and emits a warning. Of the six cross-table metrics
in the real Solid exports this was checked against, three are qualified; one reads a
shared column, and two (the same metric, in two models) qualify their columns with a
table alias, so both are left as written with a warning.

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

## Errors and warnings

One rule decides between failing and carrying on, in both directions:

- **A `ConversionError`** when the input is not a document the converter can read
  as a whole — it is not well-formed for its format, its names are ambiguous, so any
  reference could bind to the wrong object, or nothing convertible is left. Guessing
  there would produce something wrong.
- **A warning, dropping that one construct**, when a valid document has something
  the other format cannot hold or a reference that leads nowhere. The rest of the
  model still converts, and every such loss is named.

It raises when:

- the input YAML is malformed, or its root is not a mapping; the input file is not
  UTF-8 (CLI); or the YAML is nested too deeply to read;
- an Apache Ossie document has no `version` (export), or a `semantic_model` key that
  is not a non-empty list of mappings — see [Spec versions](#spec-versions);
- a value has the wrong YAML kind — a scalar where a list or mapping belongs, a list
  item that is itself a list or mapping, or a `custom_extensions` entry that is not a
  mapping with string `vendor_name` and `data`. A null is read as empty, since Solid
  writes an empty list as one;
- a required part is missing: a Solid model's `tables`, an Apache Ossie model's
  `datasets`, a dataset's `source`, or a relationship's `from_columns`/`to_columns`;
- names are ambiguous: two Apache Ossie datasets with the same `name`, or names that
  differ only in case (export); a Solid table listed twice, ignoring case (import); a
  field or metric expression listing the same dialect twice with different SQL
  (export);
- nothing convertible is left: every dataset's `source` is a SQL query, or no field
  has SQL Solid can run in the selected dialect (export);
- `--dialect` names a dialect outside `ANSI_SQL`, `SNOWFLAKE`, `DATABRICKS`,
  `BIGQUERY`, or the recorded `custom_extensions[SOLID]` dialect is not one of them,
  which only a hand-edited stash can produce (export).

It warns and drops the one construct when:

- a relationship names a dataset or table that is not declared, has join columns that
  do not pair up, or joins on a column that is not a declared field (both
  directions); a primary key names an undeclared field (export);
- a field or metric has no expression in SQL Solid can run (`MDX`, `TABLEAU`,
  `HOLISTICS_AQL`, …), or a field is computed (`first_name || ' ' || last_name`) and so
  is no warehouse column — and so does a key, join or metric that reads such a field
  (export);
- a dataset's `source` is a SQL query, with what reads it (export);
- an Apache Ossie construct has no Solid slot — see the list at the top (export).

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
(Holistics AQL, ThoughtSpot formulas) is checked to drop just those fields and metrics,
with a warning, and convert the rest, as documented.
That sweep deliberately pins **no** warning counts, because those fixtures belong to other
converters and each converter's CI is path-filtered to its own directory — a pin here would
break on a main-branch push from a PR that never ran this suite. The exact interop gaps are
pinned instead against
[`tests/fixtures/foreign_ossie.yaml`](tests/fixtures/foreign_ossie.yaml), a fixture this
converter owns that reproduces the same constructs.

`tests/test_generated_roundtrip.py` checks the [guarantees](#guarantees) on 300 seeded,
generated Solid exports rather than hand-written ones: each must round-trip unchanged,
and its Apache Ossie form must be schema-valid with every key and join column a
declared field. Tests that pin exact warnings or names read
`tests/fixtures/spec_tpcds_semantic_model.yaml`, a copy of the spec's example, so a
bump of the `ossie` submodule cannot fail them; the live example is checked loosely.

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
- **A field renamed relative to its column becomes that column.** Given
  `name: ticket_number` over `expression: ss_ticket_number`, the Solid column is
  `ss_ticket_number`, with `ticket_number` as a synonym, since a Solid column must be one
  the warehouse has. Downstream Solid consumers see the catalog name, not the alias.
- **A model-scoped metric written against bare column names reaches Solid with
  `tables: []`.** Only a dataset-qualified reference identifies its owner. The spec now
  requires model-scoped metrics to qualify every field, but models written before that
  rule may not; resolving bare names against each dataset's declared fields would fix
  most of them, but it is inference, so it is deliberately not done silently.

And two are **format-level**:

- **Verified queries have no home in the core spec.** Solid's `example_queries` and
  `benchmark_questions` are currently stashed, but the same construct appears across the
  ecosystem — Snowflake Cortex `verified_queries`, WisdomAI reviewed queries — and looks
  like a candidate for a core field rather than N vendor extensions.
- **A single-table Solid metric could be dataset-scoped.** It reads one table's columns
  unqualified, which is what `datasets[].metrics` (added in apache/ossie#591) is for.
  Import still writes model-scoped metrics, which every consumer reads, while support
  for the dataset-scoped form spreads across the other converters.
- **Solid exports from before October 2026 do not record their source warehouse**, so
  for those the dialect is still inferred from the column types. Newer exports carry
  `source_system`, which the converter reads first.
