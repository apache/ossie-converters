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

# The Atlan metric registry record

The converter reads metric records as the registry API returns them (`GET
/artifacts/metric/<id>`, or a list under `{"artifacts": [...]}`). The authoritative shape is
the registry's metric kind, metric spec `1.0-draft.4`; the JSON Schema of its `definition`
is in [`../tests/fixtures/metric_kind.json`](../tests/fixtures/metric_kind.json)
(the `definition` sub-schema of the registry's metric kind, contributed by Atlan under the
Apache License 2.0). A complete record is
[`../tests/fixtures/ratio_joins_dims_window/input.registry.json`](../tests/fixtures/ratio_joins_dims_window/input.registry.json).

## Artifact fields (set by the registry)

| Field | Read for |
|---|---|
| `id` | carried to Ossie as `custom_extensions[ATLAN].registry.id`, the reference for write-back |
| `name` | the Ossie metric name, slugged to an identifier (`Store Sale Errors` -> `store_sale_errors`) |
| `description` | the Ossie metric description |
| `workspace_id` | one Ossie file per workspace; carried on the semantic model |
| `metadata.definition` | the metric definition below |
| `display_name`, `artifact_status`, `owner_id`, `source_type`, `source_id`, `source_version`, `version_ordinal` | not carried |

## `metadata.definition`

| Field | Meaning | Converter |
|---|---|---|
| `spec_version` | `1.0-draft.4` | written on export; not checked on import |
| `version` | definition version, bumped by every change | `registry.definition_version`; a write-back refuses when it moved |
| `dialect` | SQL dialect of the formula and examples (`snowflake`, `bigquery`, `postgres`, ...); some producers write non-SQL dialects (`promql`) | parsed in this dialect; non-SQL refused |
| `formula` | an aggregate expression over `source` + `joins` (`COUNT(DISTINCT m.ID)`), or a whole `SELECT` | the metric expression; a whole query is split into one metric per aggregate output |
| `aggregation.type` | `count`, `sum`, `ratio`, `custom`, ... | not carried (the expression shows it) |
| `aggregation.reaggregation` | how to combine across groups (`sum`, `none`, or prose) | `agent_context.reaggregation` + instructions |
| `aggregation.null_handling` | how empty values count | `agent_context.null_handling` + instructions |
| `aggregation.zero_denominator` | `null` or `zero` | `zero` wraps the expression in `COALESCE(..., 0)`; on write-back that wrapper is read as the setting (never copied into `formula`), and only for a record whose setting is `zero` |
| `output.alias`, `output.datatype`, `output.unit` | the output column, its type and unit | `datatype`; unit to `agent_context.unit` + instructions |
| `source.table` | the base table, fully qualified | the base dataset's `source`, parsed in `dialect`: quoted parts exactly as written (case and quotes), unquoted parts folded as the dialect folds them. Two spellings of one table (quoted and unquoted) match when a join is resolved |
| `source.sql` | a filtered row set (`SELECT * FROM t WHERE ...`) or a `FROM` fragment with joins | the WHERE moves into the aggregates; a join in it needs a matching `joins[]` entry |
| `source.alias` | the alias the formula uses for the base table | resolved, not carried |
| `source.timeframe_column` | the metric's time column | a time field; `agent_context.timeframe_field` |
| `source.parameters[]` | `{name, datatype, default}`; referenced as `:name` or `{{name}}` | bound to the default; a no-default parameter that picks one value of a column becomes a dimension; otherwise refused |
| `joins[]` | `{name, to, sql, cardinality, origin}`; `sql` is an ON predicate or a full `JOIN t a ON ...` | a relationship; `cardinality: many_to_one` is required |
| (convention) | in a record's structured fields (`formula`, `filters`, `mandatory_filters`, `dimensions[].sql`, `source.timeframe_column`), a column with no table prefix belongs to `source.table`; `dimensions[].required_join` names one of the record's joins instead | followed as stated when `source.table` is a plain table. When the record's SQL joins tables (a whole-query `formula` with a JOIN, or a `source.sql` with a JOIN) a column with no table prefix in any of those fields is refused, since SQL resolves it against every joined table. A `required_join` that names no join is refused |
| `dimensions[]` | `{name, kind, sql, datatype, description, values, value_map, filter_form, required_join}` | fields with `dimension.is_time`; values and map as the field's `ai_context`; a bare column with `required_join` is read on that joined table |
| `filters[]` | predicates every query applies | folded into every aggregate as `CASE WHEN`; a range comparison against a fixed date is parked in `agent_context.window` instead (see README) |
| `mandatory_filters[]` | predicates that define the metric (cannot be removed) | same, folded in |
| `grain`, `population`, `exclusions[]` | what one row is, which rows count, which are excluded | `agent_context` keys + a generated instructions sentence |
| `examples[]` | `{question, sql, source}` | questions to `ai_context.examples`; SQL to `agent_context.examples` (first part of a split query only) |
| `depends_on[]` | names of metrics this one is built from | carried, slugged; a formula built from other metrics is refused |
| `provenance` | `{source, trust_score, observed_sql, ...}` | `agent_context.trust`; `observed_sql` to `agent_context.verified_sql` when it is not already an example |
| `extensions` | producer-specific blocks | not carried |

Everything the converter writes back is one of: `name`, `description`, `output.datatype`,
`examples[].question`, `grain`, `population`, `exclusions`, `output.unit`,
`aggregation.reaggregation`, `aggregation.null_handling`, and (for a metric with one part)
`formula`, `filters` (the parked window and parameter picks), `mandatory_filters`,
`dimensions`, `joins`, and `source.table` / `source.alias` / `source.sql` when the record's
formula was a whole query or its source a filtered row set (the structure then moves into the
new formula). Patches use the record's own aliases.
