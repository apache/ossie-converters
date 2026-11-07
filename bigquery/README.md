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

# Ossie <> LookML (BigQuery) Converter

Converts Apache Ossie YAML semantic models to [LookML](https://cloud.google.com/looker/docs/what-is-lookml), the semantic modeling language for Looker — Google Cloud's semantic layer over BigQuery, and LookML projects back to Ossie. Pure offline conversion — no BigQuery or Looker connection required.

> **Note:** This converter is under active development. It handles common cases but has not been thoroughly tested against all edge cases — use with caution in production.

## Setup

```bash
uv sync
```

## Usage

```bash
# Ossie -> LookML
uv run ossie-bigquery export -i input.yaml -o output.lkml

# LookML -> Ossie (a project directory or one .lkml file; stdout without -o)
uv run ossie-bigquery import -i lookml_project/ -o model.yaml --explore orders
```

`ossie-bigquery -i input.yaml -o output.lkml`, without a command, still exports.

## Tests

```bash
uv run pytest
```

## Mapping

| Ossie construct | LookML output |
|-----------------|---------------|
| Ossie document (root `name`, `description`, `datasets`, …) | One LookML document: a header comment, the views, then the explores (split into `.view.lkml` / `.model.lkml` files when checking into a Looker project) |
| `dataset` | `view` with `sql_table_name` (3-part sources are backtick-quoted as `` `project.dataset.table` ``, sources that already carry backticks are emitted as-is; subquery sources become a `derived_table`). The dataset `description` (plus `ai_context` synonyms) has no view-level parameter in LookML, so it is kept as a comment in the view and emitted as the explore `description` |
| `dataset.primary_key` | `primary_key: yes` on the key dimension; composite keys (and keys over a `dimension_group`) become a hidden `primary_key` dimension that `CONCAT`s each key field's expression as `CAST(... AS STRING)`, the pattern Looker documents for composite keys |
| `field` | `dimension` typed from `datatype` (see below), with `label` and `description` |
| `field` with a temporal `datatype`, or `is_time: true` and no `datatype` | `dimension_group` with `type: time`, `datatype: date \| datetime \| timestamp`, and standard timeframes. A trailing `_date` / `_time` / `_datetime` / `_timestamp` is dropped from the group name (so `created_time` becomes `dimension_group: created`, exposing `created_date`, `created_month`, …; `created_at` keeps its name) unless that would collide with another field or a dataset-scoped metric |
| `metric` (model root) | `measure` in the view of the dataset it aggregates (see below) |
| `dataset.metrics` (dataset-scoped) | `measure` in that dataset's view, its single-part names resolved against the dataset's fields (a name that matches none is warned about). Referencing another dataset is an error, as is a metric named like a field of the same dataset (the two share one namespace) |
| `relationship` | `explore` on the many-side (`from`) dataset with `join`s (`relationship: many_to_one`, `type: left_outer`); `sql_on` joins `${view.field}` references, composite keys are `AND`-ed |

### Data types

| Ossie `datatype` | LookML |
|------------------|--------|
| `String`, `Time`, omitted | `dimension` `type: string` (LookML has no time-of-day type) |
| `Integer`, `Decimal`, `Float` | `dimension` `type: number` |
| `Boolean` | `dimension` `type: yesno` |
| `Date` | `dimension_group` `datatype: date`, timeframes `[raw, date, week, month, quarter, year]` |
| `DateTime` | `dimension_group` `datatype: datetime` with `convert_tz: no` (civil time carries no zone, so Looker must not shift it), timeframes `[raw, time, date, week, month, quarter, year]` |
| `DateTimeTz` | `dimension_group` `datatype: timestamp`, same timeframes |
| `Opaque`, unrecognized | `type: string` with a warning |

`is_time` is a role marker in Ossie, so an `is_time` field with a scalar `datatype` (TPC-DS marks the integer `d_year` this way) stays a plain dimension; only a temporal `datatype`, or `is_time` with no `datatype` at all, produces a `dimension_group`. The latter is reported per field, because Looker will assume a `TIMESTAMP` column.

### Names

LookML needs every view, field, measure and join name to be a valid identifier and unique within its scope, so the converter checks that up front instead of emitting a project that fails to load:

- dataset, field, metric and relationship names must be valid LookML names, and duplicates within their scope are an error;
- a `dimension_group` reserves the names of the fields it expands to (`created_date`, `created_raw`, …), so the shortened group name is only used when none of those collide with another field. A group that keeps its full name drops any timeframe another field already uses (`created` next to an integer `created_year` loses `year`) with a warning; a clash on `_raw`, which joins and measures reference, is an error. No two fields of a view can expose the same name;
- a model-root metric named like a field of its home view is emitted as `<name>_measure` with a warning; the hidden helper measures and the synthetic composite `primary_key` dimension pick names no field or declared metric uses.

### Expressions

Every field and metric expression is parsed with [sqlglot](https://github.com/tobymao/sqlglot) in its own dialect (see [Dialect selection](#dialect-selection)), its column references are rewritten to LookML substitutions, and it is written out as BigQuery SQL:

- in a field expression a bare `column` is a source column and becomes `${TABLE}.column`; in a metric a bare name refers to a field (Ossie spec, Name Resolution), so a computed field becomes `${field}` (`SUM(revenue)` over `revenue = price * quantity` is `${revenue}`) while a field that only selects the column of the same name stays `${TABLE}.column`; a quoted identifier keeps its backticks (`` ${TABLE}.`Order ID` ``), and a bare literal or keyword (`NULL`, `TRUE`, `CURRENT_DATE`) is left alone rather than treated as a column;
- `dataset.column` becomes `${dataset.field}`, resolved through the fields the dataset exposes, so a join or metric on a time column lands on the `dimension_group`'s `_raw` timeframe rather than a name that no longer exists;
- a reference to a dataset that is not in the model, or to a column no field exposes, is emitted as-is with a warning naming the field or metric.

Expressions sqlglot cannot read, including tokenizer errors such as an unterminated string, are emitted verbatim with a warning.

### Measures

A metric is placed in the view of the first dataset it qualifies a column with (`SUM(store_sales.ss_ext_sales_price)` lands in `store_sales`); a metric that only uses bare columns is placed in the one dataset that exposes them all. Anything else falls back to the first view with a warning that names the metric.

- A single `SUM` / `AVG` / `MIN` / `MAX` over a row-level expression becomes a native `type: sum` / `average` / `min` / `max` measure; `COUNT(DISTINCT x)` becomes `count_distinct`; `COUNT(*)` becomes `type: count`.
- An aggregate `FILTER (WHERE c)` is rewritten to the equivalent `IF`, so `SUM(amount) FILTER (WHERE status = 'complete')` stays a native `type: sum` over `IF(status = 'complete', amount, NULL)`. `COUNT(*) FILTER (WHERE c)` becomes a native `type: sum` over `IF(c, 1, 0)`, which stays correct across fanning joins (it is NULL rather than 0 when there are no rows at all).
- Arithmetic over several aggregates (`SUM(a) / NULLIF(COUNT(DISTINCT b), 0)`) hoists each aggregate into a hidden native measure (`<metric>_sum_1`, `<metric>_count_distinct_2`, …) and emits the metric as `type: number` arithmetic over them, which keeps Looker's symmetric aggregates in play across joins.
- Aggregates Looker cannot type natively (`COUNT(column)`, nested aggregates) and window functions are emitted as `type: number` with the qualified expression inline, with a warning; window functions will not run as a measure and need to be rewritten by hand.
- A metric that references several datasets is emitted in the first one and warned about: it is only valid in explores that join the others.

### Explores

- Every dataset that is the `from` side of a relationship gets an explore. Joins follow the relationship graph transitively, breadth first: with `orders -> customers -> regions`, `explore: orders` joins both `customers` and `regions` (on `${customers.region_id} = ${regions.id}`).
- Two relationships from the same dataset to the same target (`orders.billing_addr` and `orders.shipping_addr` both to `addresses`) are aliased with `join: <relationship name> { from: addresses … }`, so the relationship name must be a valid LookML name. Joins downstream of an alias are aliased as `<alias>_<dataset>` so each path keeps its own copy.
- A dataset reachable by two different paths (a diamond) is joined once; the second path is skipped with a warning. Cycles are cut with a warning.
- A dataset that takes part in no relationship gets a bare `explore: name {}` so it can still be queried; datasets that are only join targets do not get their own explore.
- A join target with no `primary_key` is reported, since Looker needs one for symmetric aggregates on `many_to_one` joins.

## Dialect selection

For each field and metric the converter prefers the `BIGQUERY` dialect expression, then `ANSI_SQL`, then `OSSIE_SQL_2026` (the portable Ossie dialect, which is ANSI-based), consistent with the dialect-selection contract in [`../README.md`](../README.md). `BIGQUERY` expressions are read as BigQuery; `ANSI_SQL` and `OSSIE_SQL_2026` are read with sqlglot's default ANSI dialect and transpiled, because the two disagree: `"Customer ID"` is an identifier in ANSI and a string literal in BigQuery, and `DATE_TRUNC('month', d)` becomes `DATE_TRUNC(d, MONTH)`. If none is present, the field/metric is skipped with a warning.

The converter also runs on `ANSI_SQL`-only models — see the canonical [TPC-DS example](../ossie/examples/tpcds_semantic_model.yaml), which the test suite converts and parses back with [lkml](https://github.com/joshtemple/lkml) to check the output is well-formed LookML.

## Input validation

Malformed input is reported as an `OssieConversionError` naming the entity (`Relationship 'o2c': 'from_columns' must contain only non-empty strings`), which the CLI prints as `Error: …` with exit status 1. That covers unparseable YAML, a container of the wrong type (a dataset, field, metric, relationship or dialect entry that is not a mapping; `datasets`, `fields`, `primary_key` or `from_columns` that are not lists), missing names, a name or expression that is not a string (YAML reads `name: 2024` as an int and `name: yes` as a boolean), a dataset without a `source` (Looker would silently use the view name as the table), and unreadable input or output paths.

## Limitations

- **Looker project layout**: the output is one document. Looker expects views in `.view.lkml` files and explores in a `.model.lkml` file that also declares `connection` and `include`; the generated sections are labelled accordingly.
- **Join type**: all joins are `left_outer` / `many_to_one`, which is what an Ossie relationship (many-side `from`, one-side `to`) declares. One-to-one and one-to-many joins would need extra information the spec does not carry.
- **Window functions** in metrics cannot be LookML measures (see above).
- **Dropped constructs**: `custom_extensions`, `unique_keys`, metric `datatype` (a LookML measure's type comes from its aggregate), relationship-level `ai_context`, and `ai_context` keys other than `synonyms` and `instructions` (such as `examples`) have no LookML counterpart and are dropped with a warning naming the entity. Field `label` is emitted; `description` plus `ai_context.instructions` and synonyms form the LookML description; dataset descriptions are kept as a comment and on the explore.

## LookML to Ossie

`import` turns one explore into one Ossie model (`--explore` picks it when the project has several; without any explores, every view becomes a dataset and there are no relationships). `--all-explores` merges every explore into one model instead, which reads back a whole model written by `export` (it makes one explore per dataset on the many side of a relationship).

Views are read the way the Looker model sees them: the files its `include:` statements reach, transitively (`/` starts at the project root, other paths at the including file, `**` crosses folders, `.lkml` may be left off). Two files may define the same view as long as no model includes both. When an explore is in several models, `--model` picks one. Dashboard includes and includes from other projects (`//`) are not read.

| LookML | Ossie |
|--------|-------|
| `view` | `dataset`; `sql_table_name` (backticks removed) or `derived_table.sql` is the `source`, and a view with neither uses its own name, as Looker does |
| `dimension` | `field` marked as a dimension (`dimension: {is_time: ...}`, which fields added for measure columns don't get). `${TABLE}.col` becomes `col`; a reference to another dimension of the view is replaced by that dimension's SQL, since field expressions reference columns. `primary_key: yes` sets the dataset's `primary_key` |
| `dimension_group` (`type: time`) | One field holding the raw value. Ossie has no time grain, so timeframes are not carried over. The field is named after the column it reads (`created_at`, not `created`), which also round-trips with `export`; computed SQL or a taken name falls back to `<group>_raw` |
| `measure` | Dataset-scoped `metric` over field names. A column no dimension exposes gets a field of its own. `type: number` measures inline the measures they reference, since metrics cannot reference metrics. `filters:` become `FILTER (WHERE ...)`. A measure that reads another view of the explore becomes a model-scoped metric over `dataset.field`; there, `type: count` counts the view's primary key (`COUNT(DISTINCT users.id)`), since `COUNT(*)` would count joined rows |
| `explore` / `join` | The model, and one `relationship` per join from the many side to the one side, keyed by `sql_on` equalities or `foreign_key`; `from:` aliases become datasets of their own |

| LookML type | Ossie `datatype` |
|-------------|------------------|
| `string`, `zipcode`, no type | `String` |
| `yesno` | `Boolean` |
| `number` | none (LookML does not say integer or decimal) |
| `date_time`; group `datatype: timestamp` (default) | `DateTimeTz` |
| group `datatype: date` / `datetime` | `Date` / `DateTime` |
| `count`, `count_distinct` measures | `Integer` |

Every expression is given in two dialects: `BIGQUERY`, as written in LookML, and `ANSI_SQL`, transpiled by sqlglot (`SAFE_DIVIDE(a, b)` becomes a `CASE`, `DATE_TRUNC(d, MONTH)` becomes `DATE_TRUNC('MONTH', d)`), which is what most Ossie consumers read. A BigQuery function sqlglot cannot translate stays as written in the `ANSI_SQL` expression. Metrics with `FILTER (WHERE ...)` are `ANSI_SQL` and `OSSIE_SQL_2026` instead, since BigQuery has no `FILTER` clause.

Anything without an exact Ossie form is skipped with a warning naming it, rather than translated approximately:

- dimensions that read other views (an Ossie field belongs to one dataset), measures that do so without an explore to join them, filters on a different view than the aggregate's, Liquid, `${field._sql}`, timeframes other than `_raw`, and dimension `type: date` (Looker converts the timezone before truncating);
- dimension types that compute their value (`tier`, `duration`, `location`, ...), `epoch` / `yyyymmdd` groups;
- measure types other than `count`, `sum`, `average`, `min`, `max`, `count_distinct` and `number` (`percent_of_total`, `running_total`, `median`, `sum_distinct`, ...);
- filters other than exact values, lists, numeric comparisons, `%` / `_` wildcards, yes/no and `NULL`. Negation (`-cancelled`) is skipped because Looker does not document whether it matches NULL;
- `many_to_many` joins and `sql_on` that is not equalities between two views. Join types other than `left_outer`, and explore filters (`sql_always_where`, `access_filter`, ...), warn because Ossie relationships carry only keys;
- native derived tables (`explore_source`).

Refinements (`view: +orders`, `explore: +orders`) are merged into what they refine, in include order: they add fields and joins, and override parameters. `extends:` is resolved after refinements, as in Looker: a view or explore starts as a copy of those it extends and its own definition merges on top. `extension: required` views and explores get no dataset or model of their own.

## Roadmap

Things this converter does not do yet, in rough order of value:

- **LookML import follow-ups**: `percent_of_total` and `running_total` in `custom_extensions`.
- **`OSSIE_SQL_2026` as the primary source**: once the Ossie expression language settles, translating its portable functions (`DATE_TRUNC('month', d)` → `DATE_TRUNC(d, MONTH)`, `DATEADD` → `DATE_ADD`, …) to BigQuery with sqlglot would let models written only in the Ossie dialect convert without a hand-written `BIGQUERY` expression; sqlglot already does most of that transpilation.
- **Explore-level descriptions and `ai_context`**: relationship and model `ai_context` could feed Looker's explore `description` or `sql_always_where`-style hints once Looker exposes a stable place for agent context.
- **Vendor extension passthrough**: a `LOOKER` (or `BIGQUERY`) `custom_extensions` vendor could carry LookML-only settings such as `value_format`, `drill_fields`, `group_label` and `view_label` on round trips.
- **Join cardinality**: if the spec grows a cardinality or join-type hint on relationships, map it to `relationship:` and `type:` instead of assuming `many_to_one` / `left_outer`.
