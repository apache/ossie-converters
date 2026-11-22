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

# Apache Ossie Converters

Reference converters that translate between [Apache Ossie](https://github.com/apache/ossie)
semantic models and other semantic formats.

## Repository layout

Each converter lives in its own top-level directory (`dbt/`, `snowflake/`, `salesforce/`, ...)
and is built and tested on its own; see the `README.md` in each directory.

The Ossie specification, examples, validation script and shared Python package live in
[apache/ossie](https://github.com/apache/ossie), which is pinned here as the `ossie` git
submodule. Converter builds and tests read from it, so clone with submodules:

```bash
git clone --recurse-submodules https://github.com/apache/ossie-converters.git
# or, in an existing clone
git submodule update --init
```

Repository-level cross-converter acceptance and pairwise checks live in
[`interop/`](interop/README.md). The matrix registers all 25 converter projects,
with explicit capability skips, JSON/Markdown reports, and intermediate artifacts.

The Microsoft converter also has an optional
[vendor-specific live validation helper](microsoft/README.md#optional-live-engine-validation).
`ossie_microsoft.engine` deploys exported Power BI models to Microsoft Fabric and asks
Microsoft's engine to compile and evaluate their DAX against generated sample data.
It does not implement Ossie's Layer 2/3 query semantics or establish cross-vendor
query-result equivalence.

Contributions follow the Apache Ossie
[contribution guide](https://github.com/apache/ossie/blob/main/CONTRIBUTING.md).

## Document format

Converters use the [flat core format](https://github.com/apache/ossie/tree/main/core-spec): one model per JSON/YAML document,
with `name`, `datasets`, `relationships`, and `metrics` at the root alongside
`version`. Migrate legacy `semantic_model` wrappers before conversion unless the
target converter explicitly supports them. The [Microsoft converter](microsoft/README.md)
accepts the single-model `0.1.1` array returned by Snowflake Semantic Views. See the
[migration guidance](https://github.com/apache/ossie/blob/main/core-spec/spec.md#migrating-earlier-document-shapes).
For catalog exports, write separate documents rather than wrapping multiple
models in one file. Ontology documents retain their embedded `semantic_model`
property as defined by the ontology schema.

## Overview

An Ossie Converter translates between the Ossie semantic model format and a specific vendor's semantic implementation. This enables teams to author a semantic model once in the Ossie standard and then generate the corresponding vendor-specific representation automatically.

## Hub-and-Spoke Model

Ossie converters follow a **hub-and-spoke** architecture:

- **Hub**: The Ossie core specification acts as the central, vendor-neutral format.
- **Spokes**: Each converter handles translation to or from a specific vendor format.

```
                  ┌─────────────┐
                  │  Snowflake  │
                  └──────┬──────┘
                         │
┌─────────────┐    ┌─────┴─────┐    ┌─────────────┐
│     dbt     ├────┤   Ossie   ├────┤  Salesforce │
└─────────────┘    └─────┬─────┘    └─────────────┘
                         │
                  ┌──────┴──────┐
                  │  Databricks │
                  └─────────────┘
```

This approach avoids the need for point-to-point converters between every pair of vendors. With N vendors, a point-to-point strategy would require N*(N-1) converters. With Ossie as the hub, only 2*N converters are needed (one import and one export per vendor), and interoperability with all other vendors comes for free.

## Converter Responsibilities

A converter handles two directions of translation:

### Export (Apache Ossie → Vendor)

Read an Ossie semantic model and produce the equivalent vendor-specific representation. For example:

- Ossie → Snowflake semantic model definition
- Ossie → dbt `semantic_models` YAML
- Ossie → Tableau data source / Salesforce semantic layer
- Ossie → Databricks Unity Catalog metric views
- Ossie → LookML (Looker / BigQuery)

### Import (Vendor → Apache Ossie)

Read a vendor-specific semantic model and produce a valid Ossie model, including mapping vendor-specific metadata into `custom_extensions`.

## Supported Vendors

The Ossie specification currently defines extensions for the following vendors:

| Vendor | Description |
|--------|-------------|
| `SNOWFLAKE` | Snowflake semantic model |
| `SALESFORCE` | Salesforce / Tableau semantic layer |
| `DBT` | dbt semantic models |
| `DATABRICKS` | Databricks semantic layer |
| `BIGQUERY` | Looker / LookML (Google Cloud semantic layer over BigQuery) |
| `OMNI` | Omni semantic model |
| `WISDOM` | WisdomAI domain |
| `NVIDIA_AUTO_ONTOLOGY` | NVIDIA Auto Ontology model document YAML (formerly `NVIDIA_GSF`) |
| `SIGMA` | Sigma Computing data model |
| `THOUGHTSPOT` | ThoughtSpot TML (Model + Table/SQL View) |
| `CUBE` | Cube data model |
| `HEX` | Hex semantic project |
| `HOLISTICS` | Holistics AML dataset |
| `ATLAN` | Atlan metric registry records |
| `LIGHTDASH` | Lightdash semantic layer (model files, or dbt `meta`) |
| `ATACCAMA` | Ataccama ONE catalog metadata |

[`wisdom-semantic-view`](wisdom-semantic-view/README.md) is a separate, fixture-scoped spoke. It reads one Snowflake semantic view, writes one Ossie document, writes a Wisdom domain-export JSON (format `1.0`), and reverses that path. It keeps metric synonyms and `module_custom_instructions.sql_generation`. It does not call `snowflake` (Cortex Analyst export only) or `wisdom` (that spoke drops synonyms and field or metric `ai_context`).

Each vendor may define custom extensions (via the `custom_extensions` field in the Ossie spec) to carry vendor-specific metadata that does not have an equivalent in the core specification.

## Mapping Core Constructs

The following table shows how each Ossie construct maps conceptually to vendor equivalents. A converter must handle each of these:

### Semantic Model

The top-level container. Maps to the root object in each vendor's format.

| Ossie Field | Description | Converter Consideration |
|-----------|-------------|------------------------|
| `name` | Model identifier | Map to vendor's model/project name |
| `description` | Human-readable description | Most vendors support a description field |
| `ai_context` | Instructions, synonyms for AI tools | Map if vendor supports AI/LLM annotations |
| `datasets` | Logical datasets (fact/dimension tables) | See dataset mapping below |
| `relationships` | Foreign key connections | See relationship mapping below |
| `metrics` | Model-scoped aggregate measures | See metric mapping below |
| `custom_extensions` | Vendor-specific metadata | Extract extensions matching the target vendor |

### Datasets

Datasets represent logical tables (fact or dimension tables). They contain fields and define the structure of the data.

| Ossie Field | Description | Converter Consideration |
|-----------|-------------|------------------------|
| `name` | Dataset identifier | Map to table/entity/model name |
| `source` | Physical table reference (e.g., `database.schema.table`) | Parse into vendor-specific catalog/schema/table components |
| `primary_key` | Single or composite primary key | Map to vendor's PK syntax; note composite keys use arrays like `[order_id, line_number]` |
| `unique_keys` | Alternative unique identifiers | Map if vendor supports unique constraints |
| `fields` | Row-level attributes (columns) | See field mapping below |
| `metrics` | Dataset-scoped aggregate measures | See metric mapping below |
| `ai_context` | Synonyms and context for AI | Map if vendor supports semantic annotations |
| `custom_extensions` | Vendor-specific metadata | Extract extensions matching the target vendor |

### Fields

Fields represent row-level attributes. They can be simple column references or computed expressions.

> **Note:** `datatype` (on `Field` and `Metric`) declares a field's logical data type; `dimension.is_time` is an independent temporal-role marker.
> A field may carry both, either, or neither. Use `datatype` for data-type questions (casting, serialization); use `is_time` for role questions
> (classifying time dimensions). When `is_time` is unset it defaults to `true` if `datatype` is one of `Date`, `Time`, `DateTime`, `DateTimeTz`,
> and `false` otherwise. Explicit `is_time` always wins.

| Ossie Field | Description | Converter Consideration |
|-----------|-------------|------------------------|
| `name` | Field identifier | Map to column/attribute name |
| `expression` | Portable `sql` expression plus optional vendor `dialects` | Use the target vendor's entry in `dialects` when present, otherwise `sql` (see below) |
| `datatype` | Logical data type of the field (one of `String`, `Integer`, `Decimal`, `Float`, `Boolean`, `Date`, `Time`, `DateTime`, `DateTimeTz`, `Opaque`). | Converters SHOULD consult `datatype` for the field's logical data type. `Decimal` is exact base-10 with unspecified precision and scale; `Float` is approximate. Prefer the temporal members (`Date`, `Time`, `DateTime`, `DateTimeTz`) to classify time dimensions. Omit `datatype` when unknown; use `Opaque` + `custom_extensions` for a known type outside the portable vocabulary. |
| `dimension.is_time` | Temporal-role marker. When `true`, the field should be treated as a time dimension regardless of its `datatype` (e.g. an `Integer` year grain, a `String` month name, or a `Date` column). When unset, defaults to `true` for temporal `datatype`s (`Date`, `Time`, `DateTime`, `DateTimeTz`) and `false` otherwise. | Map to vendor-specific time dimension markers. Converters SHOULD classify as a time dimension when `is_time` resolves to `true` (either explicit or defaulted from a temporal `datatype`). An explicit `is_time: false` suppresses the time-dimension classification even on temporal-typed columns. |
| `label` | Categorization label | Map if vendor supports field labels/tags |
| `description` | Human-readable description | Most vendors support field descriptions |
| `ai_context` | Synonyms and business context | Map if vendor supports semantic annotations |

**Expression dialect selection**: An expression has a portable top-level `sql` (written in `OSSIE_SQL_2026`) and may add vendor-specific variants in `dialects`. For example:

```yaml
expression:
  sql: LOWER(email)
  dialects:
    - dialect: SNOWFLAKE
      expression: LOWER(email)::VARCHAR
```

A Snowflake converter should pick the `SNOWFLAKE` entry when available, and use `sql` otherwise. Either part may stand alone. When both `sql` and an `OSSIE_SQL_2026` entry in `dialects` are present, `sql` is authoritative. Documents written for `0.1.1` may carry only an `ANSI_SQL` entry; accept it as a last fallback. Metrics use the same selection.

### Relationships

Relationships define foreign key connections between datasets. They support both simple and composite keys.

| Ossie Field | Description | Converter Consideration |
|-----------|-------------|------------------------|
| `name` | Relationship identifier | Map to vendor's join/relationship name |
| `from` | Many-side dataset | Map to the referencing table |
| `to` | One-side dataset | Map to the referenced table |
| `from_columns` | Foreign key columns (array) | Positional correspondence with `to_columns`; handle composite keys |
| `to_columns` | Primary/unique key columns (array) | Must have same cardinality as `from_columns` |

**Composite key example**: A composite relationship like:

```yaml
from_columns: [product_id, variant_id]
to_columns: [id, variant_id]
```

means `from.product_id = to.id AND from.variant_id = to.variant_id`. The converter must generate the equivalent multi-column join in the target vendor format.

### Metrics

Metrics are aggregate measures. They are declared in one of two places:

- **Dataset-scoped** (`datasets[].metrics`): aggregations over a single dataset, which then carries its own metrics and stays queryable on its own. Metric names are unique within the dataset, and consumers reference the metric as `dataset.metric`.
- **Model-scoped** (`metrics` at the model root): aggregations that may combine fields from several datasets via relationships. Consumers reference the metric as `metric`. A model-scoped metric over a single dataset is also valid, like `total_sales` in the TPC-DS example.

A name in a metric expression resolves according to the metric's scope:

| Name in the expression | In a dataset-scoped metric | In a model-scoped metric |
|------------------------|----------------------------|--------------------------|
| `amount` (unqualified) | The dataset's declared field `amount` | Invalid: qualify it as `dataset.field` |
| `source.amount` | The column `amount` of the dataset's `source`, even when the dataset also declares a field named `amount` | The field `amount` of a dataset named `source`; model-scoped metrics cannot reference source columns |
| `orders.amount` | Invalid: a metric that needs another dataset's field is model-scoped | The field `amount` of the dataset `orders` |

So in a dataset-scoped metric, a converter must never read `source.amount` as a reference to a dataset named `source`. If a field `amount` is defined as `amount * fx_rate`, `SUM(amount)` sums the converted field and `SUM(source.amount)` sums the raw column. A converter also has to handle these rules:

- **A field named `source`.** `source` is not reserved. If a dataset declares a field named `source`, the name refers to that field, and the dataset's metrics cannot reference source columns. On import, declare a field for the column and reference that instead.
- **One namespace per dataset.** A dataset's field names and dataset-scoped metric names must be unique across the two. On import, keep a vendor measure model-scoped when its name matches a field of the dataset.
- **Names unique per dataset only.** `orders.count` and `customers.count` can coexist. An exporter that places metrics in one vendor namespace must detect such collisions and report them, rather than emit duplicate names or let one metric replace another.

When producing Ossie, prefer declaring a metric that aggregates a single dataset under that dataset. When consuming Ossie, accept single-dataset metrics at the model root; don't reject or move them.

These rules follow the spec pinned by the `ossie` submodule: see [Metric Scoping](https://github.com/apache/ossie/blob/ee89dbdc2844d8b7d293ed42c8613928bb99505f/core-spec/spec.md#metric-scoping) and [Name Resolution](https://github.com/apache/ossie/blob/ee89dbdc2844d8b7d293ed42c8613928bb99505f/core-spec/spec.md#name-resolution) at that commit, or `ossie/core-spec/spec.md` in your clone. The spec's `main` branch may already have rules the converters don't implement yet.

| Ossie Field | Description | Converter Consideration |
|-----------|-------------|------------------------|
| `name` | Metric identifier | Map to vendor's measure/KPI name |
| `expression` | Portable `sql` aggregate expression plus optional vendor `dialects` | Select the expression as for fields, then resolve its names by the metric's scope (see above) |
| `datatype` | Logical data type of the metric result (one of `String`, `Integer`, `Decimal`, `Float`, `Boolean`, `Date`, `Time`, `DateTime`, `DateTimeTz`, `Opaque`). | Converters SHOULD consult `datatype` to declare the result type of the aggregation. Numeric measures should distinguish exact `Decimal` or `Integer` results from approximate `Float` results. Omit `datatype` when unknown; use `Opaque` + `custom_extensions` for a known type outside the portable vocabulary. |
| `description` | What the metric measures | Most vendors support descriptions |
| `ai_context` | Synonyms and business context | Map if vendor supports semantic annotations |

**Cross-dataset metric example**:

```yaml
- name: customer_lifetime_value
  expression:
    sql: SUM(store_sales.ss_ext_sales_price) / COUNT(DISTINCT customer.c_customer_sk)
```

The converter must ensure that the dataset references (`store_sales`, `customer`) are resolved correctly in the target vendor's format and that any required joins are established.

**Dataset-scoped metric example**, from the `store_sales` dataset of the TPC-DS example:

```yaml
datasets:
  - name: store_sales
    # ...
    metrics:
      - name: total_quantity
        expression:
          sql: SUM(ss_quantity)
```

`ss_quantity` is a field of `store_sales`, and consumers reference the metric as `store_sales.total_quantity`.

**Exporting dataset-scoped metrics**: If the target vendor attaches measures to tables, map each dataset's metrics there. If it only has model-level measures, rewrite each expression for model scope: qualify every field reference with the dataset name (`ss_quantity` becomes `store_sales.ss_quantity`), and translate each `source.column` into the vendor's own reference to that column of the dataset's table. Do both before any model-scope name resolution runs: there, `source.column` would mean a field of a dataset named `source`. Check the result for name collisions with other metrics, and report any metric the vendor cannot express instead of dropping it.

### Custom Extensions

Custom extensions carry vendor-specific metadata as a JSON string. A converter should:

1. **On export (Ossie → Vendor)**: Extract extensions where `vendor_name` matches the target vendor, parse the `data` JSON, and apply the vendor-specific settings.
2. **On import (Vendor → Ossie)**: Capture vendor-specific settings that have no Ossie core equivalent and store them as a `custom_extension` entry.

**Example**: A Snowflake export converter would extract:

```yaml
custom_extensions:
  - vendor_name: SNOWFLAKE
    data: '{"warehouse": "ANALYTICS_WH", "database": "PROD", "schema": "PUBLIC"}'
```

and apply `warehouse`, `database`, and `schema` to the appropriate places in the Snowflake model definition.

### AI Context

The `ai_context` field appears at every level (model, dataset, field, relationship, metric). It can be a simple string or a structured object:

```yaml
# Simple string
ai_context: "orders, purchases, sales"

# Structured object
ai_context:
  instructions: "Use this for sales analysis"
  synonyms:
    - "orders"
    - "purchases"
  examples:
    - "Show total sales last month"
```

A converter should map `ai_context` when the target vendor supports equivalent constructs (e.g., LLM instructions, column descriptions with business terms). When the target vendor does not support AI annotations natively, the converter may encode them in `custom_extensions` on import to avoid data loss.

## Writing a Converter

### Step-by-Step Guide

1. **Validate input**: Use the [Ossie JSON Schema](https://github.com/apache/ossie/blob/main/core-spec/ossie-schema.json) and the [validation script](https://github.com/apache/ossie/blob/main/validation/validate.py) to ensure the source Ossie model is valid before conversion.

2. **Parse the Ossie model**: Load the JSON or YAML document as one model.

3. **Map datasets**: For each dataset, translate the `name`, `source`, `primary_key`, `unique_keys`, `fields`, and dataset-scoped `metrics` (see step 6) to the vendor's format. Parse the `source` string (typically `database.schema.table`) into the vendor's catalog structure.

4. **Map fields with dialect selection**: For each field, select the expression that matches the target vendor. Implement a fallback chain:
   - Prefer the vendor-specific entry in `dialects` (e.g., `SNOWFLAKE` for a Snowflake converter)
   - Otherwise use the top-level `sql`, or an `OSSIE_SQL_2026` entry in `dialects` when there is no `sql`
   - Accept a legacy `ANSI_SQL` entry if none of these is available
   - Raise a warning or error if no expression is usable

5. **Map relationships**: Translate relationship definitions into the vendor's join syntax. Ensure composite key column ordering is preserved.

6. **Map metrics**: Map the model-root `metrics` and each dataset's `metrics`, selecting expressions as for fields. In a model-scoped metric, resolve `dataset.field` references (e.g., `store_sales.ss_ext_sales_price`) into the vendor's qualified column format. In a dataset-scoped metric, resolve unqualified names as fields of that dataset and `source.column` as a column of its `source`, not as a dataset named `source`. If the vendor has no per-table measures, see [Exporting dataset-scoped metrics](#metrics). Report any metric that cannot be mapped instead of dropping it.

7. **Apply custom extensions**: Extract `custom_extensions` entries matching the target `vendor_name` and apply vendor-specific settings to the output.

8. **Preserve AI context**: Map `ai_context` to vendor-equivalent annotations where supported.

9. **Validate output**: Verify the generated vendor model is valid according to the vendor's own schema or tooling.

### Handling Edge Cases

| Scenario | Recommended Approach |
|----------|---------------------|
| Missing vendor-specific dialect for a field/metric | Use the top-level `sql`; if only a legacy `ANSI_SQL` entry exists, use it and log a warning |
| Computed fields with vendor-specific SQL syntax | Require the vendor dialect in the source Ossie model; error if neither the vendor dialect nor `sql` is available |
| Composite primary keys | Ensure the vendor format supports composite keys; if not, flatten or document the limitation |
| Cross-dataset metrics referencing multiple tables | Ensure all referenced datasets exist and relationships are defined; resolve qualified column names |
| Dataset-scoped metrics on a vendor with only model-level measures | Rewrite them for model scope (see [Exporting dataset-scoped metrics](#metrics)); report any that cannot be rewritten |
| Dataset-scoped metrics with the same name in different datasets | Valid in Ossie; if the vendor has one metric namespace, report the collision instead of emitting duplicates |
| Imported vendor measure named like a field of its table | Keep it model-scoped, since a dataset's fields and metrics share one namespace |
| Custom extensions with unknown vendor | Ignore (do not discard) — preserve for round-tripping |
| `ai_context` on a vendor that doesn't support it | Store in a `custom_extension` under the converter's own vendor name (see [`extensions/vendors.md`](https://github.com/apache/ossie/blob/main/extensions/vendors.md)) to preserve for round-tripping; there is no shared cross-vendor token |

### Round-Trip Fidelity

A well-implemented converter pair (import + export) should preserve as much information as possible during round-tripping:

```
Vendor A model → [Import] → Ossie model → [Export] → Vendor A model
```

To achieve this:

- **Never discard information silently**. If a vendor-specific attribute has no Ossie core equivalent, store it in `custom_extensions`.
- **Preserve field ordering** where possible, as some vendors are sensitive to declaration order.
- **Preserve `custom_extensions`** for all vendors, not just the target vendor. This allows a model to carry metadata for multiple vendors simultaneously (e.g., a single Ossie model with both Snowflake and dbt extensions).

## Example: Conceptual Conversion Flow

Given the [TPC-DS example](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml) from the Apache Ossie repository, a Snowflake export converter would:

1. Read the `tpcds_retail_model` semantic model.
2. Create a Snowflake semantic model with name `tpcds_retail_model`.
3. Map each dataset (`store_sales`, `date_dim`, `customer`, `item`, `store`) to Snowflake table references, parsing `source` values like `tpcds.public.store_sales` into Snowflake's `database.schema.table` format.
4. For each field, use the top-level `sql` expression (since no `SNOWFLAKE` dialect is provided in this example).
5. Translate relationships into Snowflake join definitions, mapping `from_columns` and `to_columns` pairs.
6. Translate model-scoped metrics like `total_sales` (`SUM(store_sales.ss_ext_sales_price)`) and dataset-scoped metrics like `store_sales.total_quantity` (`SUM(ss_quantity)`) into Snowflake metric definitions.
7. Extract the `SALESFORCE` and `DBT` custom extensions — these would not be applied to the Snowflake output but should be preserved if the model is later imported back to Ossie.

## Contributing a New Converter

The main Ossie repository does not accept new converters. Develop new semantic model and ontology converters in a separate repository. For Apache Ossie hosting, use the [ossie-converters repository](https://github.com/apache/ossie-converters) and coordinate contributions on `dev@ossie.apache.org`. See [Converter Contributions](https://github.com/apache/ossie/blob/main/CONTRIBUTING.md#converter-contributions) for details.

To add support for a new vendor, in your own repository or in `ossie-converters`:

1. Use a stable `vendor_name` string in each custom extension emitted by the converter.
2. Define the custom extension schema for the vendor (what vendor-specific metadata fields are supported in the `data` JSON).
3. Implement the export converter (Ossie → Vendor).
4. Implement the import converter (Vendor → Ossie).
5. Add tests using the [TPC-DS example model](https://github.com/apache/ossie/blob/main/examples/tpcds_semantic_model.yaml) as a baseline.
6. Document any limitations or unsupported constructs.
