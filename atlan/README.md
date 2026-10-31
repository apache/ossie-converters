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

# Apache Ossie <-> Atlan metric registry converter

Bidirectional, offline conversion between an [Apache Ossie](https://github.com/apache/ossie)
semantic model and metric records of the Atlan metric registry. No Atlan tenant, token or
network access required: the input is the JSON the registry API returns, described in
[docs/registry-format.md](docs/registry-format.md).

A registry metric is a SQL formula (an expression, or a whole query) over a source table,
with declared joins, filters, dimensions, parameters, and the prose a talk-to-data agent
needs (grain, population, exclusions, re-aggregation rule, unit, example questions). Ossie
has fields for the shape (datasets, fields, relationships, metric expressions, `ai_context`)
and none for filters, windows, parameters, additivity or units, so:

- **Import** (`ossie-atlan import`): registry records -> one Ossie document. Filters move
  inside every aggregate as `CASE WHEN`, parameters are bound to their defaults, columns are
  rewritten to `dataset.field`, a whole-query formula becomes one metric per aggregate
  output. Everything an agent needs that Ossie core cannot hold goes to
  `custom_extensions[ATLAN]` on the metric as `agent_context`, plus the registry id and
  definition version (`registry`), so another tool can map the metric back.
- **Export** (`ossie-atlan export`): one Ossie metric -> a registry metric definition (the
  new-metric case: the record has no registry id yet). Works on files other converters
  wrote, when the metric's columns can be placed on one dataset.
- **Write-back** (`ossie-atlan write-back`): an Ossie file that came from a registry record,
  edited, -> the registry patch it implies, versioned against the record it was made from.
  Edits to description, example questions and `agent_context` write back; structural edits
  to one part of a split query are refused, since the registry holds one query for all parts.

A metric Ossie cannot express is a `METRIC_NOT_CONVERTIBLE` [issue](#conversion-issues) with
the exact reason, and the rest of the file still converts. The converter never guesses:
nothing is looked up outside the record, and nothing is written that a check would not catch.

## Installation

```bash
pip install apache-ossie-atlan        # once published to PyPI
# or, from a checkout of this directory:
pip install -e .
```

Runtime dependencies are `PyYAML` and `sqlglot` (which parses each formula in the record's
declared dialect). Python 3.11+.

## Usage

### Command line

```bash
ossie-atlan import -i records.json [-o model.yaml] [--name my_model] [--workspace WS] [--metric NAME ...]
ossie-atlan export -i model.yaml --metric NAME [-o definition.json]
ossie-atlan write-back -i model.yaml --metric NAME --record record.json
```

`records.json` is one registry record, a list of them, or a registry export
(`{"artifacts": [...]}`). With no `-o`, output goes to stdout; issues always go to stderr.
Records of one workspace share datasets and fields, so `--workspace` gives one Ossie file
per workspace.

### Python API

```python
from ossie_atlan import convert_registry_to_ossie, convert_ossie_to_registry, write_back

doc, issues = convert_registry_to_ossie(records)           # list of dicts -> Ossie dict (strict form)
definition = convert_ossie_to_registry(doc, "metric_name") # -> registry definition dict
patch = write_back(doc, "metric_name", record)             # -> {} when nothing changed
for issue in issues:
    print(issue)
```

## What goes where

| Registry | Ossie |
|---|---|
| `source.table`, `joins[].to` | `datasets[].source` (one dataset per table; `primary_key` from a declared join's key) |
| `joins[]` (`many_to_one`, with a cardinality) | `relationships[]` (`<from>_to_<to>`; a computed key becomes a field, `LEFT(ID, 15)` -> `left_id_15`) |
| `dimensions[]`, columns in the formula, `source.timeframe_column` | `datasets[].fields[]` (`dimension.is_time` from the dimension kind; dimension values and value map as the field's `ai_context` and `custom_extensions[ATLAN]`) |
| `formula` + `filters` + `mandatory_filters` + `aggregation.zero_denominator` | `metrics[].expression` under `OSSIE_SQL_2026` and `ANSI_SQL` (the same text, in the record's dialect; no translation) |
| `output.datatype` | `metrics[].datatype` |
| `description` | `metrics[].description` |
| `grain`, `population`, `exclusions`, `output.unit`, `aggregation.reaggregation`, `aggregation.null_handling` | `metrics[].ai_context.instructions` (generated sentence) **and** `custom_extensions[ATLAN].agent_context.<key>` (the value) |
| `examples[].question` | `metrics[].ai_context.examples` |
| `examples[].sql` (with its question, so the pair survives), `provenance.observed_sql`, `dialect`, `source.parameters`, `depends_on`, `provenance.trust_score`, which fields are this metric's dimensions and time field | `custom_extensions[ATLAN].agent_context` (`examples`, `verified_sql`, `sql_dialect`, `parameters`, `depends_on`, `trust`, `dimensions`, `relationships`, `timeframe_field`) |
| a fixed date range in `filters` (`d >= '2026-04-01'`, `d < :end_date`) | `custom_extensions[ATLAN].agent_context.window` (parked, one predicate per conjunct, in `dataset.field` names with `:parameter` placeholders kept) + a sentence in `ai_context.instructions`; **not** applied by the expression |
| `id`, `definition.version` | `custom_extensions[ATLAN].registry` (the reference used to map back) |
| `workspace_id` | `custom_extensions[ATLAN].registry.workspace_id` on the semantic model |

Not carried, on purpose: `display_name` (the registry holds it under the id we carry),
`artifact_status`, owner, source lineage, `search_text`, producer `extensions`, the registry's
audit fields.

A fixed date range is an analyst's one-off window, not part of the metric, so the Ossie
expression does not apply it; nothing is lost, because the range is parked in
`agent_context.window`, restored as `filters` by `export`, and kept by a write-back that
rewrites the formula. Only a range comparison of a column against a fixed date (`>=`, `<`,
`BETWEEN`, a date literal or a date parameter) counts as a window: an equality to one date, a
range relative to the current date, and any other predicate in the same filter string stay in
the metric.

The extension data is written as a JSON string (strict 0.2.0 form), so every other converter
reads the file.

## What the converter refuses

Each of these would convert to a metric that returns a different number, or to nothing a
consumer could run, so the record becomes an issue instead:

- a join read from the SQL with no declared cardinality (the registry's `joins[]` must say
  `many_to_one`; a LEFT JOIN in a formula is not enough)
- a CTE, window function, `QUALIFY`, `HAVING`, `DISTINCT` on the row set, `LIMIT`/`OFFSET` (a
  top-N population is a different number), or a subquery
- a row listing (no aggregate output) or a list-valued aggregate (`ARRAY_AGG`, `LISTAGG`)
- a `?` or `{template}` placeholder with no declared parameter, or a parameter with no default
  that does more than pick one value of a column (that case becomes a dimension)
- a metric defined in terms of other metrics (Ossie defines no metric references)
- a `dialect` outside the converter's SQL list (`snowflake`, `bigquery`, `databricks`, `postgres`,
  `duckdb`, `redshift`, `mysql`, `tsql`, `trino`, `presto`, `athena`, `clickhouse`, `oracle`,
  `sqlite`, `spark`; `promql` and the like have no SQL)
- a `source.alias` that names a joined table (a self-join the author did not mean), or a
  `{{parameter}}` inside a string literal
- a column whose qualifier is no declared table, or a join condition that is not a key equality
- a column with no table prefix anywhere in a record whose SQL joins tables (a whole-query
  formula with a JOIN, or a `source.sql` with a JOIN): SQL resolves it against every joined
  table, so its owner is unknown. In a record's structured fields alone, an unprefixed column
  belongs to `source.table` (see [docs/registry-format.md](docs/registry-format.md))
- a `required_join` that names no join of the record
- a table name that is not a plain table reference (`IDENTIFIER('x')`, `@stage`, `TABLE(...)`),
  does not parse in the record's dialect, or uses quotes that dialect does not use
- a filter over `MIN_BY`/`MAX_BY` or a `WITHIN GROUP` aggregate (folding the filter into one
  argument would leave the ranking column unfiltered)

## Conversion issues

`convert_registry_to_ossie` returns an `IssueLog`:

| Type | Meaning |
|---|---|
| `METRIC_NOT_CONVERTIBLE` | this record has no Ossie form; the detail names the shape |
| `MERGE_CONFLICT` | two records define one field or metric name differently, or spell two tables (or one table, quoted differently) with the same dataset name; the first is kept, this one left out |
| `PROSE_DIFFERENCE_KEPT_FIRST` | the same field with different prose; the first description is kept |

`export` and `write-back` raise `NotConvertible`, `NotWritable` (an edit the registry cannot
take) or `Conflict` (the registry record moved on since the file was made).

## Tests

`tests/fixtures/<case>/` holds 20 golden cases on a synthetic TPC-DS registry
(`tests/_synthetic.py`): the input record, the expected Ossie document, the definition each
metric converts back to, the write-back of the unedited file (`{}`), and for refused shapes the
reason. `uv run pytest` checks that the converter still produces every file byte for byte,
that each Ossie document passes the core-spec schema and apache/ossie's own validator, and
that each rebuilt definition passes the registry's metric kind schema
(`tests/fixtures/metric_kind.json`). After an intended change,
`uv run python tests/_golden.py --write` and review the diff.
