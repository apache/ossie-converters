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

# Apache Ossie ↔ Starburst Context Studio

Convert between Apache Ossie and Starburst Context Studio YAML.
Both directions run offline. Context Studio binds the resulting annotations to
existing Starburst catalogs, schemas, tables, views, and columns.

[Starburst Context Studio documentation](https://docs.starburst.io/starburst-galaxy/context-studio/index.html)

```sh
git submodule update --init ossie
cd starburst
uv sync --locked
uv run ossie-starburst export -i model.ossie.yaml -o model.context.yaml --domain Sales
uv run ossie-starburst import -i model.context.yaml -o model.ossie.yaml --name sales
```

Omit `-o` for stdout. Export uses an imported domain or the Ossie model name by
default. Import uses the document's domain or `context_studio` by default.

| Ossie | Context Studio |
| --- | --- |
| Dataset with a three-part physical source | Table annotation, or a view or materialized-view annotation when recorded in the extension |
| Physical field with the same name | Column annotation |
| Computed or aliased field | Table-bound dimension |
| Dataset metric or model metric bound to one dataset | Table-bound metric |
| Relationship with physical key columns | Positional join, including composite joins |
| Descriptions and synonyms | Descriptions and synonyms on equivalent entries |

The converter accepts explicit `TRINO` SQL, a subset of portable
`OSSIE_SQL_2026` SQL, and `ANSI_SQL`. For portable forms with different Trino
semantics, such as division, supply a Trino alternative. Cross-dataset metrics,
measure evaluation with `AGG`, and query-backed datasets fail export. Ossie
primary and unique keys, datatypes, time-role overrides, and model-level AI
context have no matching Starburst Context Studio declarations and produce warnings.
Use `--skip-unsupported-metrics` to export the rest of a model when a metric
cannot be converted. The converter prints a warning for each skipped metric.

The `STARBURST` nonportable extension carries native domain metadata,
catalog and schema annotations, terms, rules, constraints, sample queries,
business contexts, and any joins or semantic entries without an equivalent
Ossie representation. Re-export restores these entries. Other Ossie converters
can use the portable model without interpreting the extension. A Context
Studio document must identify at least one relation to produce an Ossie model.

Run tests with `uv run --locked pytest`.
