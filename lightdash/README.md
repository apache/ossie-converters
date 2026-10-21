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

# Apache Ossie <> Lightdash converter

Converts between Ossie documents and
[Lightdash](https://github.com/lightdash/lightdash) semantic definitions in
both directions. Lightdash reads its semantic layer either from native model
files or from dbt `schema.yml` files, where dimensions and metrics are declared
per column and per model under `meta`.

- **Export** (`ossie_to_lightdash`) reads an Ossie document and writes a
  Lightdash project. By default it writes Lightdash's native model files
  (`type: model`, `sql_from`, typed dimensions), which `lightdash deploy` reads
  without changes. With `--format dbt-meta` it writes one dbt `schema.yml` with
  Lightdash `meta` blocks instead, for projects that keep their definitions in
  dbt.
- **Import** (`lightdash_to_ossie`) reads a Lightdash project and writes an
  Ossie document, for teams that adopt Ossie as the source of truth for
  definitions they maintain in Lightdash. It reads both shapes: dbt
  `schema.yml` files with Lightdash `meta`, and native Lightdash model files.

Ossie input and output use the flat document format: `version`, `name`,
`datasets`, `relationships`, and `metrics` are at the root. The converter
rejects the legacy `semantic_model` wrapper. Migrate each wrapped model into
its own document before exporting.

Lightdash-only attributes are stored in `custom_extensions` under the vendor
name `LIGHTDASH`. A Lightdash to Ossie to Lightdash round trip restores the
project exactly, and every other consumer reads the core vocabulary only.

## Installation

The core `apache-ossie` package isn't published on PyPI, so install both
packages from git. No checkout is needed, and `pipx` works the same way:

```bash
pip install "apache-ossie @ git+https://github.com/apache/ossie@main#subdirectory=python"
pip install "apache-ossie-lightdash @ git+https://github.com/apache/ossie-converters@main#subdirectory=lightdash"
```

From a checkout of this directory, `uv sync` or `pip install -e .` installs
the same packages and resolves the core package from the `ossie` submodule.
Python 3.11 or later is required.

## Usage

```
# Ossie -> a deployable Lightdash project (no dbt needed)
ossie-lightdash export -i semantic_model.yaml -o my-project --dialect BIGQUERY
cd my-project && lightdash deploy

# Ossie -> one dbt schema.yml with Lightdash meta, for a dbt project
ossie-lightdash export -i semantic_model.yaml -o schema.yml --format dbt-meta --dialect BIGQUERY [--meta-under-config]

# Lightdash dbt meta -> Ossie (a schema file, or a whole dbt project directory)
ossie-lightdash import -i path/to/dbt -o semantic_model.json --database analytics_db --schema marts --dialect BIGQUERY \
  --catalog path/to/dbt/target/catalog.json
```

`--catalog` takes the `catalog.json` that `dbt docs generate` writes. For
every column without an authored `type`, the import uses the warehouse column
type from the catalog as the field's `datatype`, reduced to Ossie's vocabulary
(`INT64` becomes `Integer`, `NUMBER(12,2)` becomes `Decimal`, `TIMESTAMP_TZ`
becomes `DateTimeTz`). An authored type takes precedence. Lightdash takes most
column types from the warehouse rather than from YAML, so without a catalog
most fields have no datatype. A model that is missing from the catalog is
reported with `CATALOG_MODEL_MISSING`, which also identifies a stale catalog.

The default export writes `my-project/lightdash/models/<model>.yml`, one file
per dataset, and a starter `my-project/lightdash.config.yml`. The config's
`warehouse.type` is derived from `--dialect` (`BIGQUERY`, `SNOWFLAKE`,
`DATABRICKS`) or set with `--warehouse`. An existing config is not modified.
Each dataset's `source` becomes the model's `sql_from` verbatim, whether it is
a table reference or a query. The model name is the last identifier of a
simple source path, with SQL quoting removed. When a query or a table
identifier can't form a valid model name, the export uses the dataset's `name`
instead. A model name must start with a letter or underscore and contain only
letters, digits, and underscores. Duplicate names, including names that differ
only in case, fail the export before any file is written, in both output
formats. For example, `sales.orders` and `archive.orders` can't both export as
`orders`.

`-i/--input` and `-o/--output` follow the other converters. The positional
form (`export INPUT OUTPUT`) also works. The CLI prints issues (anything lost
or approximated, see [Issues](#issues)) to stderr, grouped by type.

### Python API

```python
from ossie import OssieDialect
from ossie_lightdash import LightdashToOssieConverter, OssieToLightdashConverter

result = LightdashToOssieConverter(OssieDialect.BIGQUERY).convert(
    schema_yml, database="analytics_db", schema="marts"
)
result.output   # OssieDocument
result.issues   # [ConverterIssue(issue_type, element_name), ...]

models = OssieToLightdashConverter(OssieDialect.BIGQUERY).convert_models(document)
models.output    # [{"type": "model", "name": ..., "sql_from": ..., "dimensions": [...]}, ...]

exported = OssieToLightdashConverter(OssieDialect.BIGQUERY).convert(document)
exported.output  # {"version": 2, "models": [...]}  (dbt-meta format)
```

## Mapping

The table describes the dbt-meta format. The model-file format uses the same
mapping with three differences: `dataset.source` is the model's `sql_from`,
every dimension has its own `type` and `sql` (a field without a datatype gets
`string` and a `DIMENSION_TYPE_DEFAULTED` issue), and model meta that the dbt
format stores in the extension (`sql_filter`, `group_details`,
`default_time_dimension`, and others) are top-level keys of the model file.

| Ossie | Lightdash (dbt meta) |
| ----- | -------------------- |
| `dataset` | dbt model. `name` is the unquoted table identifier of a simple `source`, otherwise a valid form of `dataset.name`. Collisions are rejected. |
| `dataset.source` | Assembled on import from `--database`, `--schema`, and the model name. |
| `dataset.primary_key` | Model `meta.primary_key`. A single key exports as a string, a composite key as a list. |
| `ai_context` on datasets, fields, and metrics | `ai_hint` on models, dimensions, and metrics. A multi-line instruction is a list of hints. On export, the synonyms and examples of the structured form become extra hints. |
| `field` with `dimension` | A column. Every dbt column is a Lightdash dimension by default, so a dimension with no other attributes is a plain column entry with no `meta`. |
| `field` without `dimension` (measure-only) | `columns[].meta.dimension.hidden: true`. A hidden column is the closest Lightdash equivalent of a field that is not for grouping. It keeps its `type`, `label`, and `ai_hint`. |
| `field.datatype` | `meta.dimension.type`: `String` to `string`; `Integer`, `Decimal`, and `Float` to `number`; `Date` to `date`; `DateTime` and `DateTimeTz` to `timestamp`; `Boolean` to `boolean`; `Time` and `Opaque` to `string`. On import, `number` becomes `Decimal`. |
| `field.dimension.is_time` | `time_intervals: OFF` corresponds to an explicit `is_time: false` on a temporal column. Otherwise the marker is not carried. A non-temporal time axis, such as a year stored as `Integer`, has no Lightdash equivalent. |
| `field.label` / `.description` | `meta.dimension.label` / column `description`. |
| `field.expression` (when it differs from the column name) | `meta.dimension.sql`. `dataset.col` corresponds to `${TABLE}.col`. |
| `metric.name` | Lightdash scopes metric names per model and Ossie per semantic model, so the Ossie name is Lightdash's field ID, `<model>_<metric>` (`orders_total_amount`). The bare name is stored in the extension and restored on export. An Ossie metric with no extension exports under its name with any `<model>_` prefix removed. |
| `metric.datatype` | Derived on import: `Integer` for counts, `Decimal` for numeric aggregates over a `number` column, the column's type for `min` and `max`, and the declared type for `boolean`, `string`, `date`, and `timestamp` metrics. |
| `metric` that is one aggregation over a column (`SUM(ds.col)`, `COUNT(DISTINCT ds.col)`, `SUM(DISTINCT ds.col)`, `PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ds.col)`) | Column-level `meta.metrics.<name>` with a typed metric (`sum`, `count_distinct`, `sum_distinct`, `percentile` with `percentile: 90`). |
| `metric` that is one aggregation over any other expression (`AVG(CASE WHEN ds.status = 'done' THEN 1 ELSE 0 END)`) | Model-level `meta.metrics.<name>` with the typed metric and the operand as `sql`. |
| `metric` with any other expression | Model-level `meta.metrics.<name>` with `type: number` and `sql`, on the dataset that joins every other dataset the expression references (`${joined_model.col}`). |
| `relationship` | `meta.joins`. `sql_on` is built from, and parsed into, column pairs. `relationship` is `many-to-one` unless the extension holds another value. The `alias` and the other join attributes (`relationship`, `type`, `fields`) are stored in the relationship's `LIGHTDASH` extension. A dataset joined more than once from the same model gets an alias on export. |
| A join that Ossie can't reproduce: chained through another joined model (`${projects.org_id} = ${organizations.org_id}` on the `queries` explore), an expression join (`LOWER(a) = b`), or extra conditions | The column pairs still become relationships. A chained pair produces the edge between the two models it names, unless that model declares the edge itself. The join is stored verbatim in the dataset's `LIGHTDASH` extension and restored on export, replacing the generated join to the same target and alias. Pair order and side order (`${T.y} = ${M.x}`) carry no meaning and are not stored. |
| Model meta without Ossie vocabulary (`label`, `hidden`, `sql_filter`, `group_details`, `default_time_dimension`, `required_filters`, `order_fields_by`) and column meta outside `dimension` and `metrics` (`additional_dimensions`) | Stored in the dataset's or field's `LIGHTDASH` extension (column meta under `column_meta`) and restored on export. Other consumers don't read them. |
| Bare column names in Lightdash SQL (`SUM(budget_use)`) | Qualified with the dataset when they name one of the model's columns, outside string literals and function names. The extension also records the model under `model`, so an expression that names no dataset can be placed on export. |
| `${TABLE}.col`, `${col}`, and `${other_model.col}` in Lightdash SQL | `dataset.col` and `other_model.col`. `${alias.col}` is rewritten to the aliased model. `${metric}` is replaced by that metric's expression. |
| Lightdash presentation attributes (`label`, `format`, `round`, `compact`, `group_label`, `hidden`) | `custom_extensions` with `vendor_name: LIGHTDASH`. The lowercase name from earlier documents is still read. On export, the extension data is merged into the generated definition. Structural keys are protected and can't be overridden: `sql` and `label` on dimensions, `sql` and `description` on metrics, `type` and `percentile` on metrics whose expression is a recognized aggregation, and `join` and `sql_on` on joins. |

## Dialects

Lightdash SQL is written for the project's warehouse, so `import --dialect`
labels the emitted expressions with that warehouse's Ossie dialect
(`BIGQUERY`, `SNOWFLAKE`, `DATABRICKS`). For a warehouse without an Ossie
dialect, such as Postgres or Redshift, keep the default `ANSI_SQL`. Lightdash
records which warehouse a project runs on, not which SQL subset an expression
uses, so the converter doesn't label expressions `OSSIE_SQL_2026` on its own.
For a project whose SQL is written in the portable language, pass
`import --dialect OSSIE_SQL_2026`, and the label survives the round trip.
`tests/test_portability.py` checks this for the M0-M3 measurements of
[apache/ossie#576](https://github.com/apache/ossie/issues/576).

`export --dialect` selects that dialect when an expression offers it, then
`OSSIE_SQL_2026`, then `ANSI_SQL`. When none of these is present, the export
takes the first dialect offered and reports `DIALECT_UNAVAILABLE`.

The import qualifies bare column references with SQLGlot's parser for the
selected warehouse dialect, without reformatting the SQL. Cast types, date
parts, function names, literals, comments, and already-qualified references
are unchanged. A reference inside a nested query keeps its own scope. If
parsing fails, the SQL keeps its bare names and a `COLUMN_QUALIFICATION_SKIPPED`
issue identifies the field or metric. Review that expression manually.

## Input shape

`import` reads two shapes, which can be mixed: dbt schema files (`models:` and
`seeds:` entries; Lightdash treats seeds as tables) and Lightdash model files
(`type: model`, `sql_from`, and a `dimensions:` list, the format `export`
writes). Given a file, the import reads that file. Given a project root, the
import walks `models/`, `seeds/`, `lightdash/models/`, and every other
directory in sorted order, and skips `target/`, `dbt_packages/`, virtual
environments, and `dbt_project.yml`. A model file's `sql_from`, or a dbt
model's `meta.sql_from`, becomes the dataset `source` verbatim, so
`--database` and `--schema` apply only to models that don't name their own
relation. Ossie relationships can reference only datasets in the document, so
a join whose target is not in the input is skipped with a `JOIN_TARGET_UNKNOWN`
issue.

## Where the meta is placed

dbt 1.10 and later place `meta` under `config:`. Lightdash reads both the
top-level `meta` and `config.meta`, and `config.meta` takes precedence. The
import does the same. The export writes top-level `meta` by default. Pass
`--meta-under-config` to write `config.meta` instead.

## Recommended source shape for dbt-native workflows

If dbt's native Ossie parsing also consumes the Ossie documents, import without
`--database`, so that sources take the form `schema.table`. In dbt projects the
database usually depends on the environment, and a source without a database
keeps one document valid across environments. See
[dbt-core#15649](https://github.com/dbt-labs/dbt-core/issues/15649). If you
also omit `--schema`, the import reports `SOURCE_UNQUALIFIED`.

## Fidelity and losses

The two models differ in five places. The converter handles each difference
the same way: it converts what has vocabulary on both sides, keeps the rest in
the `LIGHTDASH` extension, and reports whatever another consumer of the
document won't see.

**Scope of metric names.** Lightdash scopes metric names per model and Ossie
per semantic model, and most Lightdash projects have a `count` on several
models. The Ossie name is therefore always Lightdash's field ID,
`<model>_<metric>`, not only on collision: a metric's name must not change
because a metric was added to a different model, and the field ID is what
Lightdash users see in its API and URLs. The bare name is stored in the
extension and restored, so a Lightdash to Ossie to Lightdash round trip is
exact. An Ossie document that Lightdash didn't write has its metric names
normalized once: `total_sales` on `store_sales` becomes
`store_sales_total_sales`.

**What a dimension is.** Every dbt column is a Lightdash dimension unless it
is hidden. An Ossie field is a dimension only when it declares one.
`hidden: true` and "no `dimension`" are the two ends of one mapping. As a
result, a hidden column can't carry Ossie's `is_time` role, and Lightdash has
no form for a dimension that is neither for grouping nor a measure.

**Joins versus relationships.** Ossie relationships form one graph. A
Lightdash explore is a base model plus an explicit list of joins, which can
reach a table through another one, join on an expression, or add conditions.
The graph part becomes relationships, and a chained join produces the edge
between the two models it names. The explore part is stored verbatim in the
dataset's extension and restored, so the explore comes back exactly and other
consumers read the graph. Lightdash doesn't resolve `${other.column}`
transitively, so a metric that spans datasets is placed on the model that
joins every referenced dataset directly. When no such model exists, the
metric is dropped with an issue.

**Query-time evaluation.** Lightdash evaluates project parameters, user
attributes, and Liquid templating when a query runs. No other consumer can, so
a dimension or metric whose SQL depends on them is skipped on import and
reported, rather than exported as SQL that doesn't run. Metric-to-metric
references are inlined for the same reason: Ossie metrics can't reference each
other.

**Types.** Lightdash's `number` covers Ossie's `Integer`, `Decimal`, and
`Float`, and its `timestamp` covers `DateTime` and `DateTimeTz`, so a datatype
authored in YAML round-trips by category rather than by exact member. A column
with no authored `type` gets no datatype unless `--catalog` supplies the
warehouse type, in which case the exact member is known. Lightdash itself takes
those types from the warehouse, not from the YAML.

### Kept for Lightdash only

The following are stored in the `LIGHTDASH` extension and restored on export
(as `meta` in the dbt format, as top-level keys in the model-file format).
Other consumers don't read them.

- Presentation attributes of dimensions and metrics: `format`, `round`,
  `compact`, `groups`, `urls`, `show_underlying_values`, and others.
- Model meta without Ossie vocabulary: `label`, `hidden`, `group_details`,
  `default_time_dimension`, `order_fields_by`, and others.
- Column meta outside `dimension` and `metrics`: `additional_dimensions`.
- Join attributes: `alias`, `type`, `fields`, and others.
- Joins that Ossie can't reproduce.

Two of these change query results rather than presentation, so the import
reports them: a metric's `filters` (`METRIC_FILTER_NOT_PORTABLE`; the Ossie
expression is the unfiltered aggregate) and a model's `sql_filter`,
`sql_where`, and `required_filters` (`ROW_FILTER_NOT_PORTABLE`; the Ossie
dataset is unrestricted).

### Approximated

- When an expression names a dataset that is joined more than once, the
  reference goes through the first join (`date_dim.year` becomes
  `${date_dim.year}`, not the aliased second join). A `${alias.column}`
  reference is rewritten to the joined dataset with an
  `ALIAS_REFERENCE_FLATTENED` issue.
- A name that still collides after qualification (model `orders` with metric
  `x_total` versus model `orders_x` with metric `total`) gets a suffix and a
  `METRIC_NAME_COLLISION` issue.
- On export, the synonyms and examples of a structured `ai_context` become
  extra `ai_hint` lines. On import, `ai_hint` becomes a plain instruction
  string.
- A single-column `primary_key` exports as a string, a composite one as a
  list.

### Not carried

- `unique_keys`. Lightdash has no corresponding concept.
- `ai_context` and custom extensions on relationships.
- `dataset.name`, when a valid model name can be derived from a simple source.
  The dbt model is named after that source's unquoted table identifier.
- Relationships whose `from_columns` and `to_columns` differ in length
  (`RELATIONSHIP_COLUMNS_MISMATCHED`), and relationships to datasets missing
  from the document.
- Custom extensions of other vendors (`FOREIGN_EXTENSION_IGNORED`). They
  remain unchanged in the Ossie document.
- Documents are written at the in-repo spec version. dbt-core 1.12's native
  Ossie parsing accepts `0.1.0` and `0.1.1` only.

## Issues

The converter distinguishes two kinds of problem. Input it can't read at all
(a model without a name, `joins` written as a string, a metric definition that
is not a mapping, an Ossie document the schema rejects) raises
`ConversionError` with the name of the element. The CLI prints that message
and exits with code 2 without writing anything. Everything else converts, and
whatever was lost or approximated is reported as a `ConverterIssue`.

On import: `JOIN_STASHED` and `JOIN_SQL_UNPARSED` (a join kept for Lightdash
only), `JOIN_TARGET_UNKNOWN`, `METRIC_FILTER_NOT_PORTABLE`,
`ROW_FILTER_NOT_PORTABLE`, `EXPRESSION_NOT_PORTABLE`,
`METRIC_REFERENCE_INLINED`, `ALIAS_REFERENCE_FLATTENED`,
`METRIC_NAME_COLLISION`, `SOURCE_UNQUALIFIED`, `METRIC_SQL_MISSING`,
`COLUMN_QUALIFICATION_SKIPPED`.

On export: `CROSS_DATASET_METRIC_DROPPED`, `FIELD_REFERENCE_UNJOINED`,
`TIME_ROLE_NOT_REPRESENTABLE`, `DIMENSION_TYPE_DEFAULTED`,
`COLUMN_META_NOT_REPRESENTABLE`, `CATALOG_MODEL_MISSING`, `DIALECT_UNAVAILABLE`,
`RELATIONSHIP_COLUMNS_MISMATCHED`, `EXTENSION_DATA_INVALID`,
`FOREIGN_EXTENSION_IGNORED`.

## Development

From the repository root:

```bash
uv sync --project lightdash --locked
uv run --project lightdash pytest lightdash/tests
```

The test suite validates every converted document against
`ossie/core-spec/ossie-schema.json`, so a round trip can't produce a document
that the Python models accept but the spec rejects. It round-trips the M0-M3
portability fixtures, and it passes malformed input to both directions to
check that the converter reports it. One test exports the TPC-DS example and
runs `lightdash compile` over it. That test skips when the Lightdash CLI is
not on `PATH`. CI installs the CLI, so the test runs there. To run it locally:

```bash
npm install --no-package-lock --prefix /tmp/lightdash @lightdash/cli@2.629.4
export PATH="/tmp/lightdash/node_modules/.bin:$PATH"
```

### Local end-to-end check

The following commands export the flat TPC-DS document, compile the generated
project with the Lightdash CLI, and import it back into an Ossie document.
They need no running Lightdash server or warehouse credentials, and they
don't deploy anything. Use an empty output directory so the generated files
are easy to inspect.

```bash
uv run --project lightdash ossie-lightdash export \
  -i ossie/examples/tpcds_semantic_model.yaml -o /tmp/ossie-lightdash-check \
  --dialect SNOWFLAKE
lightdash compile --project-dir /tmp/ossie-lightdash-check \
  --no-warehouse-credentials --no-version-check --no-partial-compilation
uv run --project lightdash ossie-lightdash import \
  -i /tmp/ossie-lightdash-check -o /tmp/ossie-lightdash-roundtrip.yaml \
  --dialect SNOWFLAKE --semantic-model-name tpcds_retail_model
uv run --project ossie/python ossie/validation/validate.py /tmp/ossie-lightdash-roundtrip.yaml
```

Expected result: 5 compiled explores, no compilation errors, and a flat
round-trip document with 5 datasets and 4 relationships. The export reports
`TIME_ROLE_NOT_REPRESENTABLE` for the integer `d_year` field, because Lightdash
has no equivalent of that field's explicit time-axis role. The generated models
and the compilation output stay in the output directory for inspection.

To check the dbt 1.10+ output as well:

```bash
uv run --project lightdash ossie-lightdash export \
  -i /tmp/ossie-lightdash-roundtrip.yaml -o /tmp/ossie-lightdash-schema.yml \
  --format dbt-meta --meta-under-config --dialect SNOWFLAKE
uv run --project lightdash ossie-lightdash import \
  -i /tmp/ossie-lightdash-schema.yml -o /tmp/ossie-lightdash-dbt-roundtrip.json \
  --database tpcds --schema public --dialect SNOWFLAKE
uv run --project ossie/python ossie/validation/validate.py /tmp/ossie-lightdash-dbt-roundtrip.json
```

These checks cover conversion, schema and SQL validation, and Lightdash
compilation. They don't run warehouse queries or check a deployed project in
the browser.
