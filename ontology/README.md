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

# Ossie Ontology Converters

Converters between Ossie and Spec ontology formats.

The Palantir → Ossie converter ([`palantir/`](../palantir/README.md)), the
Ossie → RelationalAI converter ([`relationalai/`](../relationalai/README.md))
and the LinkML ↔ Ossie converters ([`linkml/`](../linkml/README.md)) are built
on this package but live in their own directories.

| Converter               | Direction |
|-------------------------|-----------|
| `ossie_to_spec`         | Ossie model → Spec YAML |
| `spec_to_ossie`         | Spec YAML → Ossie model |

The Ossie → RelationalAI (PyRel) converter builds on this package and lives in
its own directory, [`relationalai/`](../relationalai/README.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd ontology
uv sync
```

## Generating / updating the lock file

`uv.lock` is produced by uv from `pyproject.toml`. Run this whenever you add or
change a dependency:

```bash
uv lock
uv sync
```

## Usage

The package is importable as `ossie_ontology` after installation:

```python
from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_ontology.converter.spec_to_ossie.converter import SpecToOssieConverter
```

`OssieParser` parses and validates `derived_by` and `requires` expressions into
an AST by default. To keep formulas as raw text instead, pass the plain
`FormulaFactory` and `MappingFormulaFactory` from `ossie_ontology.model`.
`SpecToOssieConverter` takes the same argument and defaults the same way.

## Running the tests

```bash
uv run pytest
```

Regenerate pytest snapshots after an intentional output change:

```bash
uv run pytest --snapshot-update
```
