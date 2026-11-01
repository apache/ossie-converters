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

# Ossie LinkML Converters

Converts between [LinkML](https://linkml.io/) schemas and Apache Ossie ontologies.

| Module            | Direction |
|-------------------|-----------|
| `linkml_to_ossie` | LinkML schema → Ossie model |
| `ossie_to_linkml` | Ossie model → LinkML schema |

Both converters work with an `OssieOntology` from
[`apache-ossie-ontology`](../ontology/README.md), which this package depends on
and resolves from `../ontology`.

The converters use the natively-compiled [LinkML-Scala](https://github.com/NeverBlink-OSS/linkml-scala) library, through the [`neverblink-linkml`](https://pypi.org/project/neverblink-linkml/) package (see: [converter source code](https://github.com/NeverBlink-OSS/linkml-scala/tree/main/generator/src/eu/neverblink/linkml/generator/ossie)). This package only provides wrappers – please file any issues [here](https://github.com/NeverBlink-OSS/linkml-scala/issues).

The Ossie <-> LinkML mapping and its limitations are documented [here](https://github.com/NeverBlink-OSS/linkml-scala/blob/main/docs/ossie_mapping.md). In general, LinkML supports only a subset of restriction expressions in Ossie, `derived_by` is not yet supported, and `ontology_mappings` are not representable in LinkML. Conversely, Ossie does not support many of the features of LinkML, such as all possible inheritance patterns. We are working to iteratively improve the coverage of the mapping in both directions.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd linkml
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

The package is importable as `ossie_linkml` after installation:

```python
from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_linkml import LinkmlToOssieConverter, OssieToLinkmlConverter

ontology = LinkmlToOssieConverter().convert_file("schema.yaml")
print(OssieToSpecConverter.convert(ontology).dump_yaml())

print(OssieToLinkmlConverter.convert(ontology, schema_id="https://example.org/my-schema"))
```

## Scripts

### `scripts/ossie_to_linkml.py`

Converts an Ossie ontology (YAML or JSON) into the LinkML schema that describes it, printed to stdout.

**Usage:**

```bash
uv run python scripts/ossie_to_linkml.py path/to/ossie.yaml
# Second argument is optional, and if provided will be used as the schema's `id`:
uv run python scripts/ossie_to_linkml.py path/to/ossie.yaml https://example.org/my-schema
```

### `scripts/linkml_to_ossie.py`

Converts a LinkML schema into an Ossie-compliant YAML representation of the ontology it describes, printed to stdout. The schema's `imports` are resolved from disk.

**Usage:**

```bash
uv run python scripts/linkml_to_ossie.py path/to/schema.yaml
```

A schema can load and still have errors and warnings against it. Errors and warnings are written to stderr, only fatal problems stop the run.

## Running the tests

```bash
uv run pytest
```
