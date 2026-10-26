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

"""Catalogue of loss kinds: category, default severity and remedy for every `Loss(kind, ...)` the converter can emit.

Categories (what happened to the information):
  preserved     stashed and re-emitted by the writer, so the round trip is lossless
  parked        stashed in a TABLEAU extension and restorable by the writer
  approximated  converted to something close but not identical
  dropped       not carried over; only the loss record remains
Severities: info (benign bookkeeping), warning (information lost or changed), error (the output is missing content a
consumer is likely to need). `--strict` fails on `error`.
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = ("preserved", "parked", "approximated", "dropped")
SEVERITIES = ("info", "warning", "error")


@dataclass(frozen=True)
class LossSpec:
    category: str
    severity: str
    remedy: str


def _s(category: str, severity: str, remedy: str) -> LossSpec:
    assert category in CATEGORIES and severity in SEVERITIES
    return LossSpec(category, severity, remedy)


CATALOG: dict[str, LossSpec] = {
    # --- Tableau -> Ossie: calculations
    "row_level_calc": _s("parked", "warning", "Express the calculation over a single dataset so it becomes a TABLEAU-dialect field. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "row_level_cross_dataset": _s("approximated", "warning", "A row-level calculation over several datasets was emitted as a field on the many-side dataset with qualified references; it only evaluates where a relationship path from that dataset exists."),
    "lod": _s("parked", "warning", "No Ossie metric type for level-of-detail expressions; rebuild as a window or subquery metric. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "lod_translated": _s("approximated", "warning", "A whole-formula {AGG(x)}, {FIXED : AGG(x)} or TOTAL(AGG(x)) was emitted as a metric, and a single-dataset {FIXED d : AGG(x)} as a row-level field, each with its TABLEAU form and a window SQL form; Snowflake semantic views reject window metrics, so the Snowflake adapter keeps only the TABLEAU form for those and sends window SQL for fields. Window SQL on other warehouses is verified only by that warehouse's live suite."),
    "table_calc": _s("parked", "warning", "Table calculations depend on viz addressing and have no Ossie equivalent. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "table_calc_translated": _s("approximated", "warning", "A Snowflake PARTITION BY EXCLUDING / ordered window was emitted as a Tableau table calculation (RUNNING_SUM, WINDOW_AVG, LOOKUP); addressing follows the viz, not EXCLUDING, and there is no SQL dialect on read-back."),
    "rawsql": _s("parked", "warning", "RAWSQL passthrough is not translated; define the expression natively in the SQL dialect. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "parameter_ref": _s("parked", "warning", "Parameters are not modelled; replace the reference with a literal or a field. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "calc_reference": _s("parked", "warning", "The calculation references another calculation that was not converted. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "unresolved_reference": _s("parked", "warning", "A referenced field could not be resolved to a dataset column. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "calc_class": _s("parked", "warning", "Unknown calculation class; the formula was not converted. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "passthrough": _s("dropped", "info", "Pass-through calculation; the underlying column is already converted."),
    # --- Tableau -> Ossie: constructs with no Ossie type
    "group": _s("preserved", "warning", "Tableau groups have no Ossie type; the XML is kept in a TABLEAU extension (categorical bins, a different Tableau construct, are the ones rebuilt as a CASE field)."),
    "bin": _s("approximated", "warning", "Numeric bins become a FLOOR(x / size) * size field; a size taken from a parameter is frozen at its current value. Bins that cannot be expressed stay unconverted."),
    "categorical_bin": _s("approximated", "warning", "Single-field categorical bins become a CASE field over the member lists; others stay unconverted."),
    "hierarchy": _s("preserved", "info", "Hierarchies (drill paths) are not modelled in Ossie; kept in a TABLEAU extension and restored by to-tds."),
    "folder": _s("parked", "info", "Folders are not modelled in Ossie; membership is kept per field in a TABLEAU extension and written back by the TDS writer."),
    "parameters": _s("preserved", "warning", "Parameters are not modelled in Ossie; the ones a datasource references are kept in a TABLEAU extension and restored by to-tds."),
    "parameters_unreferenced": _s("dropped", "info", "Workbook parameters that this datasource does not reference are not converted; they are workbook-level and stay in the original workbook."),
    "datasource_filter": _s("parked", "warning", "Ossie has no filter construct, so the filter is not applied to the model; it is kept in a TABLEAU extension."),
    "initial_sql": _s("preserved", "warning", "Initial SQL is not modelled in Ossie; kept in a TABLEAU extension and restored on the connection by to-tds."),
    "connection_customization": _s("preserved", "info", "Driver capability overrides (connection-customization) are not modelled in Ossie; kept in a TABLEAU extension and restored by to-tds. The converter never invents them: they slow Tableau Cloud queries by about 40 s."),
    "initial_sql_merged": _s("approximated", "warning", "The source datasource had several connections with different initial SQL; to-tds writes one connection, so the statements are joined (newline-separated) on it and now run for every table."),
    "row_level_security": _s("dropped", "error", "Row-level security is not modelled; enforce it in the warehouse."),
    "set": _s("preserved", "warning", "Tableau sets (dynamic in/out membership) have no Ossie type; the XML is kept in a TABLEAU extension and restored by to-tds. Rebuild them as boolean fields if other tools need them."),
    "duplicate_datasource": _s("preserved", "info", "Another data source in the same workbook uses the same connection and tables; each is still converted to its own document."),
    "wildcard_table": _s("approximated", "warning", "Wildcard/sharded tables (for example BigQuery events_*) are kept as written in the dataset source; resolve them to a real table or view before use."),
    "query_parameter": _s("approximated", "warning", "Custom SQL with @named query parameters is kept verbatim; the parameters are not bound, so the SQL cannot run until they are replaced."),
    "extract": _s("parked", "info", "Extracts are not modelled; the live connection is converted and the extract settings are kept as metadata."),
    "published_datasource": _s("dropped", "warning", "A published data source is only a reference; convert the published source itself."),
    "default_sort": _s("preserved", "info", "Default sort orders are kept in a TABLEAU extension and restored by to-tds."),
    "column_instance": _s("preserved", "info", "Column instances (derived fields) are kept in a TABLEAU extension; restored by to-tds."),
    "semantic_values": _s("preserved", "info", "Semantic-value hints are kept in a TABLEAU extension; restored by to-tds."),
    "aliases": _s("preserved", "info", "Value aliases are kept in a TABLEAU extension and restored by to-tds."),
    # --- Tableau -> Ossie: structure and metrics
    "record_count": _s("approximated", "info", "The Tableau Number of Records auto-column (formula 1) was emitted as COUNT(*) in the TABLEAU dialect and as warehouse SQL."),
    "metric_inlined": _s("approximated", "info", "A referenced metric whose Ossie name cannot stand as a bare identifier (keyword or spaces) had its aggregate expression inlined."),
    "sql_untranslated": _s("approximated", "warning", "The metric was emitted with its TABLEAU expression; warehouse SQL could not be produced. Downstream tools that need SQL must translate it themselves."),
    "unmodeled_xml": _s("preserved", "info", "Direct <datasource> children whose tags are not modelled are kept in a TABLEAU extension and written back by to-tds."),
    "aggregate_expression_helper": _s("approximated", "warning", "A SUM/AVG/MIN/MAX/MEDIAN/COUNT/COUNTD of a one-table row-level expression becomes that aggregate of a helper field holding the expression."),
    "non_numeric_aggregate": _s("parked", "warning", "The aggregate is not numeric and was not converted. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "non_numeric_result": _s("parked", "warning", "The calculation result is not numeric and was not converted as a metric. The Ossie model has no such calculation; the original <column> is stashed in the TABLEAU extension and a Tableau round trip restores it unchanged."),
    "relationship": _s("dropped", "error", "Only equality joins between table columns are converted."),
    "relationship_cardinality": _s("approximated", "info", "Cardinality is assumed (first endpoint is the many side) unless a unique-key hint exists."),
    "union": _s("approximated", "warning", "Members with differing columns become an explicit NULL-padded UNION ALL (Tableau's Table Name/Sheet columns are not produced); with unknown column metadata only the first member is kept."),
    "unknown_warehouse": _s("approximated", "warning", "The connection class is not a registered warehouse (Snowflake, BigQuery); expressions were translated as generic ANSI SQL and table sources were copied as written. Check them against the real database, or register the warehouse in warehouse.py."),
    "blend": _s("dropped", "warning", "Worksheets blend several data sources by linking fields; blends live in worksheets, not in the data sources, so each source is converted alone. Model the relationship explicitly (join or relationship) if needed."),
    "file_source": _s("dropped", "warning", "Load the file into a warehouse table and point the data source at it; Excel and text files cannot back a semantic view."),
    "no_tables": _s("dropped", "error", "The data source has no tables to convert."),
    "no_column_metadata": _s("dropped", "error", "Columns could not be attributed to a table."),
    "unsupported_object": _s("dropped", "error", "The object type is not supported."),
    "source_qualified": _s("approximated", "info", "A two-part table source was qualified with the database named by the Tableau connection."),
    "unsupported_datatype": _s("parked", "info", "Spatial, tuple and table types are kept as Opaque with the original type stashed and restored."),
    # --- Ossie -> Tableau
    "untranslatable_expression": _s("dropped", "error", "Provide a TABLEAU-dialect expression or simplify the SQL expression."),
    "relationship_key_synthesized": _s("approximated", "info", "A join column that is not a declared field was added as a hidden column."),
    "datatype_defaulted": _s("approximated", "info", "The field has no datatype; one was chosen from its dimension flags."),
    "field_renamed": _s("approximated", "info", "Tableau names fields by physical column; the Ossie name survives as the caption."),
    "datasource_filter_restored": _s("approximated", "warning", "A stashed data-source filter was written back to the TDS; it changes query results exactly as it did in Tableau."),
    # --- Snowflake
    "sql_unreconciled": _s("approximated", "warning", "Best-effort window SQL (table-wide OVER / PARTITION BY) is attached, but live Snowflake values did not match Tableau table-across addressing on SAMPLE_SUPERSTORE. The SQL is kept for downstream tools; do not treat it as proven equivalent."),
    "expression_approximated": _s("approximated", "info", "A Tableau function with no exact SQL equivalent was translated to the closest form in the target warehouse's SQL; see docs/expression-mapping.md."),
    "parameter_inlined": _s("approximated", "warning", "A parameter reference was replaced by the parameter's current value (--inline-parameters); the calculation no longer follows parameter changes."),
    "rawsql_translated": _s("approximated", "warning", "The inner SQL of a RAWSQL_* call was used as the warehouse expression; it runs in the source database unchanged, so check it for dialect differences."),
    # --- stash codec
    "stash_stale": _s("approximated", "info", "The model changed since the stash was written; the stash was ignored and the live value used."),
}

KNOWN_KINDS: frozenset[str] = frozenset(CATALOG)


def spec(kind: str) -> LossSpec:
    """The catalogue entry for `kind`; unknown kinds get a conservative default so they are never hidden."""
    return CATALOG.get(kind) or LossSpec("dropped", "warning", "")
