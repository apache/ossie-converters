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

# Apache Ossie ↔ Snowflake Converter

Converts between Ossie YAML semantic models and [Snowflake Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst) semantic model YAML. Conversion is offline and does not require a Snowflake connection.

> **Note:** This converter is under active development. It handles common cases but has not been thoroughly tested against all edge cases — use with caution in production.

Ossie documents contain one model directly at the root, with `version`, `name`,
`datasets`, and optional model properties. Legacy `semantic_model` wrappers
(arrays or objects) are rejected.

## Setup

```bash
uv sync
```

## Usage

```bash
# Snowflake Cortex Analyst YAML -> Ossie YAML
uv run ossie-snowflake snowflake-to-ossie -i cortex_analyst.yaml -o semantic_model.yaml

# Ossie YAML -> Snowflake Cortex Analyst YAML
uv run ossie-snowflake ossie-to-snowflake -i semantic_model.yaml -o cortex_analyst.yaml
```

The original bare `-i` / `-o` form remains an alias for `ossie-to-snowflake`.

## Expression dialects

The exporter prefers `SNOWFLAKE`, `ANSI_SQL`, and tagged `OSSIE_SQL_2026`
expressions in that order. It then translates supported `BIGQUERY` field and
metric expressions to Snowflake SQL using SQLGlot. Untranslatable expressions
are skipped with a warning.

## Data Type Mapping

The exporter maps the optional logical `datatype` on Ossie fields to Snowflake
`data_type`; the importer uses the same mappings in reverse where possible:

| Ossie | Snowflake |
|---|---|
| `String` | `VARCHAR` |
| `Integer` | `NUMBER(38,0)` |
| `Decimal` | `NUMBER` |
| `Float` | `FLOAT` |
| `Boolean` | `BOOLEAN` |
| `Date` | `DATE` |
| `Time` | `TIME` |
| `DateTime` | `TIMESTAMP_NTZ` |
| `DateTimeTz` | `TIMESTAMP_TZ` |

An omitted datatype remains unspecified. `Opaque` has no portable Snowflake
mapping, so the exporter omits `data_type` and emits a warning.

On import, Snowflake types without an Ossie equivalent become `Opaque`, with the
concrete Snowflake type retained in a `SNOWFLAKE` `nonportable_extensions` entry.
The exporter currently warns and drops nonportable extensions, so that concrete
type is preserved in the Ossie document but is not restored by a round trip.

## Import mapping

Snowflake `tables[].base_table` becomes `datasets[].source`; dimensions,
time dimensions, and facts become fields; table metrics are lifted to the
model-level metrics list. Synonyms and custom instructions become `ai_context`,
and column uniqueness becomes a dataset `unique_keys` entry. Selected
Snowflake-only properties (`sample_values`, `cortex_search_service`, `is_enum`,
`filters`, `verified_queries`, and `module_custom_instructions`) are retained in
`nonportable_extensions`; known unsupported properties such as `tags`, `labels`,
`access_modifier`, and relationship join types are reported with warnings.

Expressions proven to use standard SQL are labelled `ANSI_SQL`. Expressions
that cannot be verified as portable retain the `SNOWFLAKE` dialect and produce
a warning, so other vendor converters do not mistake them for portable SQL.

## Tests

```bash
uv run pytest
```

## Limitations

Some Ossie concepts (e.g., `ai_context` on relationships) do not have a native counterpart in the Snowflake semantic model. These are dropped during conversion and the converter will emit warnings so you know what was left behind.

Snowflake metric result types are inferred from their expressions, so Ossie
metric `datatype` values are not emitted as `data_type` properties.

Dataset-scoped metrics (`datasets[].metrics`) are emitted as table-level
`tables[].metrics`, scoped to that logical table. Model-root metrics stay on
the view-level `metrics` list. A column qualified by the dataset or source
table name is unqualified; a qualifier that names another table skips the
metric with a warning.

A unique key that repeats the dataset's primary key, with its columns in any
order, is not emitted as a `unique_keys` entry: Snowflake semantic views
declare a column as `PRIMARY KEY` or `UNIQUE`, not both.

Successful BigQuery-to-Snowflake expression translations emit a warning for each
field or metric. A translation that parses successfully can still change results,
including failed-cast behavior and NULL handling; review the generated SQL before use.
Existing Snowflake, ANSI_SQL and OSSIE_SQL_2026 alternatives retain their precedence
and do not produce this translation warning.

On import, table-scoped metrics are lifted to model scope and their column
references are qualified with the source table name. Metrics with duplicate
names after lifting produce a warning. Relationship join types and relationship
types have no core representation and are dropped with a warning.
