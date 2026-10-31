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

# semantido ⇄ Apache Ossie converter

Converts between [semantido](https://github.com/hikarilabs/semantido) — a
code-native semantic layer authored as decorators on SQLAlchemy models and
Apache Ossie semantic model documents.

Unlike file-to-file converters, semantido's source of truth is live Python:
the forward direction imports a module of decorated models, syncs the
semantic layer, and emits Ossie YAML through the typed `apache-ossie`
objects. The reverse direction is code generation: it produces a Python
module of `@semantic_table`-decorated models from an Ossie document.

Tracks the **semantido ≥ 0.5.2** surface: concept bindings (`concept=` /
`<column>_concept`), the concept registry — definitions, relations,
external mappings, and grain — carried as a model-level custom extension,
and `unique_keys`. A module-level `CONCEPT_REGISTRY` in the loaded models
module is passed to `sync_semantic_layer()` automatically; the reverse
direction regenerates it as a `build_registry()` function.

## Usage

```bash
# decorated SQLAlchemy models -> Ossie YAML
ossie-semantido semantido-to-ossie \
    -m models.[SQLAlchemy model] -p ./my_project \
    -n [model name] -o [model].ossie.yaml

# Ossie YAML -> generated semantido model code
ossie-semantido ossie-to-semantido \
    -i [model].ossie.yaml -o generated_model_[model name].py
```

## Mapping

| semantido                                 | Ossie                             | Notes                                                |
|-------------------------------------------|-----------------------------------|------------------------------------------------------|
| decorated table                           | `datasets[]`                      | `source` from schema-qualified table name; reverse re-parses `source` into table + schema |
| column                                    | `fields[]`                        | expression emitted as `ANSI_SQL` column reference    |
| column SQL type                           | field `datatype` + `data_type` extension | portable logical type (`String`, `Integer`, …) set on `datatype`; exact SQL type kept in the extension for a lossless round-trip |
| `description` / `<col>_description`       | `description`                     |                                                      |
| `business_context`, `application_context` | dataset `ai_context.instructions` |                                                      |
| `synonyms` / `<col>_synonyms`             | `ai_context.synonyms`             |                                                      |
| `<col>_sample_values`                     | field `ai_context.examples`       |                                                      |
| `time_dimension=` + `<col>_time_grain`    | field `dimension.is_time`         | grain preserved in extensions                        |
| FK relationships                          | `relationships[]`                 | single and composite joins parsed to aligned `from_columns`/`to_columns`; name includes the FK columns so same-pair/composite joins stay unique. Reverse regenerates `ForeignKey` / `ForeignKeyConstraint` from `relationships[]` |
| `sql_filters`                             | dataset `custom_extensions`       | no Ossie core field (see below)                      |
| `<col>_privacy_level`                     | field `custom_extensions`         | no Ossie core field (see below)                      |

### Metadata carried in `custom_extensions`

semantido captures runtime-governance metadata that has no core-spec home
in Ossie today: per-field privacy classification, default SQL filters /
row-level security fragments, and time-dimension grain. These are preserved
losslessly under `vendor_name: SEMANTIDO` with `data` as a serialized JSON
string, so no information is lost in interchange — but tools without
semantido awareness will not interpret them.

### Known lossy conversions

Conversions report structured `ConverterIssue`s (mirroring `ossie_dbt`); the
reverse direction lists every issue in a TODO block at the top of the generated
module for review. The cases:

- **Metrics** — semantido has no metric concept, so model-scoped and
  dataset-scoped metrics are dropped (`METRIC_DROPPED`).
- **Computed expressions** — a field whose expression is not a plain reference
  to its own column (or has no `ANSI_SQL` dialect) becomes a bare `Column`; the
  expression is dropped (`NON_ANSI_EXPRESSION_DROPPED`).
- **Unmapped types** — a SQL/`datatype` with no mapping falls back to `String`
  (`UNMAPPED_DATA_TYPE`).
- **Missing primary key** — SQLAlchemy requires one, so a primary key is
  synthesized on the first column (`MISSING_PRIMARY_KEY`).
- **Awkward identifiers** — a column name that is not a valid Python identifier
  is sanitized, with the real name carried via `Column("original", …)`
  (`IDENTIFIER_SANITIZED`).
- **Unknown governance / registry values** — an unrecognized privacy level,
  time grain, concept-relation kind or mapping helper, or a relationship whose
  `from` dataset is absent, is reported and skipped rather than emitted as code
  that would fail to import.

Non-`SEMANTIDO` `custom_extensions` are preserved on import but cannot be
regenerated into semantido code on export (semantido has no place to carry
another vendor's metadata); they are therefore not round-tripped through the
reverse direction.

### Loading constraint

`load_from_module` syncs against semantido's process-global registry, so **one
models module per process** is supported: convert several models in separate
processes, or declare each against an isolated base.

## Tests

The test fixture is an EMIR (European Market Infrastructure Regulation)
trade-reporting model — a regulated-industry schema whose semantics
(role-bridge fan-out, signed vs. unsigned amount conventions,
notional/valuation ambiguity) demonstrate why interchange must carry
business meaning, not just structure.

```bash
uv run pytest tests/
```
