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

# Apache Ossie ↔ NVIDIA Auto Ontology Converter

Converts between [Apache Ossie](https://github.com/apache/ossie/blob/main/core-spec/spec.md)
semantic models and [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology)
(formerly GSF) model documents. Runs offline: no Auto Ontology instance,
database or network needed. Auto Ontology itself uses this converter for its
Ossie import and export, and an Auto Ontology → Ossie → Auto Ontology round
trip keeps its IDs, SQL and native metadata.

Ossie documents hold one model at the root; legacy `semantic_model` wrappers
are rejected.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# Ossie → Auto Ontology
uv run ossie-nvidia-auto-ontology export -i model.yaml -o model.auto_ontology.yaml \
  --database-name tpcds

# Auto Ontology → Ossie, optionally with an ontology and mapping
uv run ossie-nvidia-auto-ontology import -i model.auto_ontology.yaml -o model.yaml \
  --ontology-output model.ontology.yaml --mapping-output model.mapping.yaml
```

- `--database-name` sets the database for `schema.table` sources.
- `--name` overrides the Ossie model name.
- `--embedded-ontology-output` writes the ontology with the mapping embedded,
  for tools that don't read mapping documents yet.
- Without `-o`, output goes to stdout.
- Invalid input exits non-zero with the error on stderr.

The same conversions are available from Python as
`convert_ossie_to_auto_ontology`, `convert_auto_ontology_to_ossie` and
`convert_auto_ontology_to_ossie_ontology`; they raise
`AutoOntologyConversionError` on invalid input.

## Mapping

| Ossie | Auto Ontology |
|---|---|
| Dataset | Term representing one catalog table |
| Field backed by one column | Term column attribute |
| Field `datatype` | Physical type of that column |
| Computed field | SQL attribute |
| Metric | Custom analysis |
| Relationship | Joins and foreign keys |

## Loading into Auto Ontology

Auto Ontology imports either this converter's output or the Ossie model
itself. Use **Settings → Import / Export**, or the API with an
[API token](https://github.com/NVIDIA/auto-ontology/blob/main/docs/authentication.md#api-tokens)
from an admin account:

```bash
curl --fail-with-body -X POST "$AUTO_ONTOLOGY_URL/api/model/import" \
  -H "x-api-key: $AUTO_ONTOLOGY_API_TOKEN" \
  -F file=@model.auto_ontology.yaml
```

The instance needs a connection for every database in the document.
`POST /api/model/export` with `{"format": "ossie"}` exports the model as Ossie.

## Limitations

- Each Auto Ontology term must represent exactly one table.
- Auto Ontology display names become `snake_case` identifiers; the original
  names are kept as synonyms.
- `ai_context`, dimensions, synonyms and custom extensions have no
  Auto Ontology equivalent and are dropped on export.
- Import embeds the full native document in an `NVIDIA_AUTO_ONTOLOGY`
  extension to keep the round trip intact, so Ossie files from large catalogs
  are large.

## Tests

```bash
uv run pytest
```
