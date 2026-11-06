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

# Ossie to LookML (BigQuery) Converter

Converts Apache Ossie YAML semantic models to [LookML](https://cloud.google.com/looker/docs/what-is-lookml), the semantic modeling language for Looker — Google Cloud's semantic layer over BigQuery. Pure offline conversion — no BigQuery or Looker connection required.

> **Note:** This converter is under active development. It handles common cases but has not been thoroughly tested against all edge cases — use with caution in production.

## Setup

```bash
uv sync
```

## Usage

```bash
uv run ossie-bigquery -i input.yaml -o output.lkml
```

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
| `field` with a temporal `datatype` (unless it sets `is_time: false`) | `dimension_group` with `type: time`, `datatype: date \| datetime \| timestamp`, and standard timeframes. A trailing `_date` / `_time` / `_datetime` / `_timestamp` is dropped from the group name (so `created_time` becomes `dimension_group: created`, exposing `created_date`, `created_month`, …; `created_at` keeps its name) unless that would collide with another field or a dataset-scoped metric |
| `metric` (model root) | `measure` in the view of the dataset it aggregates (see below) |
| `dataset.metrics` (dataset-scoped) | `measure` in that dataset's view, its single-part names resolved against the dataset's fields (a name that matches none is warned about). Referencing another dataset is an error, as is a metric named like a field of the same dataset (the two share one namespace) |
| `relationship` | `explore` on the many-side (`from`) dataset with `join`s (`relationship: many_to_one`, `type: left_outer`); `sql_on` joins `${view.field}` references, composite keys are `AND`-ed |

### Data types

| Ossie `datatype` | LookML |
|------------------|--------|
| `String`, `Time`, omitted | `dimension` `type: string` (LookML has no time-of-day type) |
| `Date`, `DateTime`, `DateTimeTz` with `is_time: false` | `dimension` `type: string` (kept off the time axis, without Looker's date casts or timezone conversion) |
| `Integer`, `Decimal`, `Float` | `dimension` `type: number` |
| `Boolean` | `dimension` `type: yesno` |
| `Date` | `dimension_group` `datatype: date`, timeframes `[raw, date, week, month, quarter, year]` |
| `DateTime` | `dimension_group` `datatype: datetime` with `convert_tz: no` (civil time carries no zone, so Looker must not shift it), timeframes `[raw, time, date, week, month, quarter, year]` |
| `DateTimeTz` | `dimension_group` `datatype: timestamp`, same timeframes |
| `Opaque`, unrecognized | `type: string` with a warning |

`is_time` is a role marker in Ossie, not a type, so only a temporal `datatype` produces a `dimension_group`. An `is_time` field with a scalar `datatype` (TPC-DS marks the integer `d_year` this way) stays a plain dimension. So does `is_time: true` with no `datatype` (TPC-DS marks the integer `d_moy` and the string `d_quarter_name` this way): the spec says not to infer a type from it, and a `dimension_group` would make Looker treat the column as a `TIMESTAMP`. That case is emitted as `type: string` and reported per field. An explicit `is_time: false` always wins, so a temporal field that sets it (an audit `created_at`) is a plain dimension too.

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

A metric is placed in the view of the first dataset it qualifies a column with (`SUM(store_sales.ss_ext_sales_price)` lands in `store_sales`); a metric that only uses bare columns is placed in the dataset that exposes them all (the first such dataset, with a warning, when several do). A metric whose bare columns no dataset exposes falls back to the first view with a warning that names the metric.

- A single `SUM` / `AVG` / `MIN` / `MAX` over a row-level expression becomes a native `type: sum` / `average` / `min` / `max` measure; `COUNT(DISTINCT x)` becomes `count_distinct`; `COUNT(*)` and `COUNT(1)` become `type: count`; `COUNT(x)` becomes a native `type: sum` over `IF(x IS NOT NULL, 1, 0)`, so it is not inflated by fanning joins (it is NULL rather than 0 when there are no rows at all).
- An aggregate `FILTER (WHERE c)` is rewritten to the equivalent `IF`, so `SUM(amount) FILTER (WHERE status = 'complete')` stays a native `type: sum` over `IF(status = 'complete', amount, NULL)`. `COUNT(*) FILTER (WHERE c)` becomes a native `type: sum` over `IF(c, 1, 0)`, which stays correct across fanning joins (it is NULL rather than 0 when there are no rows at all).
- Arithmetic over several aggregates (`SUM(a) / NULLIF(COUNT(DISTINCT b), 0)`) hoists each aggregate into a hidden native measure (`<metric>_sum_1`, `<metric>_count_distinct_2`, …) and emits the metric as `type: number` arithmetic over them, which keeps Looker's symmetric aggregates in play across joins. Looker counts each row of a measure's own view once, so a helper over another dataset's field goes in that dataset's view: in TPC-DS `store_productivity`, `SUM(store.s_number_employees)` becomes a hidden measure of `store` (each store counted once, not once per sales row), referenced as `${store.store_productivity_sum_2}`. A helper over fields of several datasets stays in the metric's view.
- Aggregates Looker cannot type natively (`STDDEV`, nested aggregates) and window functions are emitted as `type: number` with the qualified expression inline, with a warning; window functions will not run as a measure and need to be rewritten by hand.
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

## Roadmap

Things this converter does not do yet, in rough order of value:

- **LookML to Ossie import**, so a Looker project can be exchanged like the Snowflake and dbt models can. The module is named `ossie_to_lookml.py` and the CLI split out into `cli.py` for that reason; an importer would live beside it as `lookml_to_ossie.py` and reuse `lkml` for parsing.
- **`OSSIE_SQL_2026` as the primary source**: once the Ossie expression language settles, translating its portable functions (`DATE_TRUNC('month', d)` → `DATE_TRUNC(d, MONTH)`, `DATEADD` → `DATE_ADD`, …) to BigQuery with sqlglot would let models written only in the Ossie dialect convert without a hand-written `BIGQUERY` expression; sqlglot already does most of that transpilation.
- **Explore-level descriptions and `ai_context`**: relationship and model `ai_context` could feed Looker's explore `description` or `sql_always_where`-style hints once Looker exposes a stable place for agent context.
- **Vendor extension passthrough**: a `LOOKER` (or `BIGQUERY`) `custom_extensions` vendor could carry LookML-only settings such as `value_format`, `drill_fields`, `group_label` and `view_label` on round trips.
- **Join cardinality**: if the spec grows a cardinality or join-type hint on relationships, map it to `relationship:` and `type:` instead of assuming `many_to_one` / `left_outer`.
