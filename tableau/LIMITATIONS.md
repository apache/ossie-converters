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

# Known limitations

What the Tableau to Ossie converter does not do, or does only approximately. Nothing is dropped silently: every gap below is reported as a classified `Loss` in the `.loss.json` file (kinds, categories, severities and remedies are in [docs/loss-catalog.md](docs/loss-catalog.md)). The README section "Known limitations and lossiness" has the full per-kind tables.

### Calculations

| Limitation | Loss kind | Workaround |
|---|---|---|
| `{INCLUDE}` / `{EXCLUDE}`, `PREVIOUS_VALUE`, offset and mismatched `WINDOW_*`, and INCLUDE+n emit window SQL plus `sql_unreconciled` (table-wide addressing, not Tableau Compute Using). A 40-row slice match is not Compute Using: those nine stay loud. TWB `<table-calc ordering-field>` is used when present; nothing is invented. Brace `{AGG}` / `{FIXED : AGG}`, `SIZE()`, `{FIXED [dim]: AGG} * n`, `{FIXED [dim]: [x]}` (`FIRST_VALUE`), INDEX/RANK/LOOKUP/FIRST/LAST compounds, `INDEX()>1`, `LOOKUP(...) IS NULL`, `WINDOW_COUNT`, `RANK_PERCENTILE`, and `LOOKUP(..., FIRST())` are reconciled. `RUNNING_*`, LOD inside `IF`, `WINDOW_STDEV`, and `RANK_MODIFIED` stay parked. | `sql_unreconciled`, `lod`, `table_calc` | Rebuild parked shapes as a window or subquery metric. Treat unreconciled SQL as best-effort. |
| Converted LOD and `TOTAL` calculations are approximations: whole-formula `{AGG}`, `{FIXED : AGG}` and `TOTAL(AGG)` become metrics, and a single-dataset `{FIXED d : AGG}` becomes a row-level field. Each carries the TABLEAU form plus a window SQL form. | `lod_translated` | Check the result against the source workbook. |
| Metrics that return a non-numeric value are not converted. Mixed or cross-dataset aggregates are emitted with their TABLEAU expression; warehouse SQL is attached when the catalog can translate them, otherwise `sql_untranslated`. | `sql_untranslated`, `non_numeric_result`, `non_numeric_aggregate` | A single `SUM/AVG/MIN/MAX/MEDIAN` of a one-table row-level expression is rescued via a helper field (`aggregate_expression_helper`, approximated). A reference to another metric is a bare identifier when the name is legal (`metric_inlined` only when it is not). A reference inside an aggregate stays `calc_reference`. |
| Tableau Number of Records (`1`) is emitted as `COUNT(*)` in the TABLEAU dialect and as warehouse SQL. | `record_count` | None. |
| Parameters are not modelled. They are stashed for round trips, or inlined at their current value with `--inline-parameters`. | `parameters`, `parameter_ref`, `parameter_inlined` | Replace references with a literal or a field. |
| `RAWSQL_*` converts only over resolvable single-dataset columns, and the inner SQL runs unchanged in the source database. | `rawsql`, `rawsql_translated` | Check the SQL for dialect differences. |
| Functions with no exact SQL equivalent use the closest Snowflake form (for example `MAKEPOINT(lat, lon)` becomes `ST_MAKEPOINT(lon, lat)` with the arguments swapped). `DATEPARSE` with a literal format converts to `TRY_TO_TIMESTAMP` (a non-matching value gives NULL). `DATENAME` converts literal parts that already have a DATEPARSE `TO_CHAR` token (`year`/`month`/`day`/`hour`/`minute`/`second`). Other parts, non-literal `DATEPARSE` formats, and `MAKEPOINT` leftovers stay parked. | `expression_approximated` | See [expression-mapping.md](docs/expression-mapping.md). |

### Structure

- Cardinality is assumed (first endpoint is the many side) unless a `unique-key` hint exists. Tableau relationships carry none.
- Only equality joins between table columns convert. Other relationship operands are dropped as errors (`relationship`).
- Unions with differing columns become a NULL-padded `UNION ALL`. Tableau's `Table Name` and `Sheet` columns are not produced. With unknown column metadata only the first member is kept (`union`).
- Groups, hierarchies, parameters, data-source filters, initial SQL, default sorts, aliases and extract settings have no Ossie type. They are stashed in a TABLEAU extension and restored by `to-tds`. Unknown `<datasource>` children are stashed as `unmodeled_xml` and replayed. Non-`TABLEAU` `custom_extensions` round-trip through `<user:ossie-foreign-extensions>`. If the Ossie name or label changed after conversion, the stash is stale and ignored (`stash_stale`).
- Dropped outright: sets, folders as first-class objects (membership is kept per field and restored when the stash survives), row-level security (`error`, enforce it in the warehouse), published-datasource references, and legacy join details.
- Connection-only data sources, columns that cannot be attributed to a table, and unsupported objects stop conversion (`no_tables`, `no_column_metadata`, `unsupported_object`).
- Spatial, tuple and table types become `Opaque` with the original type stashed.
- Worksheets and dashboards are out of scope.

### Ossie to Tableau (`to-tds`)

- Snowflake and BigQuery connections are written (`warehouse.py`). Calc catalog templates are Snowflake SQL; BigQuery uses overrides plus a portable allowlist, and `MEDIAN` has no BigQuery translation. Output has a logical layer; no extracts and no credentials are written.
- An expression with no TABLEAU dialect that is not a bare column or simple aggregate, and cannot be translated back, is dropped (`untranslatable_expression`). The converter does not guess and does not emit RAWSQL.
- Time, timezone and opaque types map to the closest Tableau type. Fields with no datatype get one from their dimension flags.
- Tableau names fields by physical column, so the Ossie name survives only as the caption.
- Column instances, semantic-value hints and groups are stashed and re-emitted.
- Bin and categorical-bin calculations are restored from the stash.
- A restored data-source filter changes query results exactly as it did in Tableau.

Kinds, categories, severities and remedies stay in [docs/loss-catalog.md](docs/loss-catalog.md). Expression mapping stays in [docs/expression-mapping.md](docs/expression-mapping.md).
