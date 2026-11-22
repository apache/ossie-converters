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

# Ossie SHACL Converter

Converts Apache Ossie ontologies into [SHACL](https://www.w3.org/TR/shacl/) shapes.

| Module           | Direction |
|------------------|-----------|
| `ossie_to_shacl` | Ossie model → SHACL shapes (Turtle or N-Triples) |

The converter works with an `OssieOntology` from
[`apache-ossie-ontology`](../ontology/README.md), which this package depends on
and resolves from `../ontology`.

The conversion goes through [LinkML](https://linkml.io/): Ossie → LinkML → SHACL. Both steps are done by the natively-compiled [ohi LinkML](https://linkml.ohi.dev/) library, through the [`neverblink-linkml`](https://pypi.org/project/neverblink-linkml/) package. This package only provides a wrapper – please file any issues [here](https://github.com/NeverBlink-OSS/ohi/issues).

The mapping and its limitations are documented in the ohi LinkML docs, separately for [Ossie to LinkML](https://linkml.ohi.dev/docs/importers/ossie/) and [LinkML to SHACL](https://linkml.ohi.dev/docs/generators/shacl/). In short:

- Each entity type becomes a `sh:NodeShape` that targets the class of the same IRI. Each of its relationships becomes a property shape with counts and range derived from the multiplicity, `requires`, and the role's concept.
- `ontology_mappings` (datasets, join paths, metrics), `derived_by` rules and expressions LinkML cannot express are dropped. Each kind of dropped item is reported once with `warnings.warn`.
- Subtypes are not linked with `rdfs:subClassOf`. Instead, a subtype's shape repeats its supertypes' property shapes.
- Shapes are open by default: data may include properties the ontology does not declare. Pass `closed=True` to reject them. Shapes of concepts that have subtypes stay open even when `closed=True` is specified, since a subtype adds properties of its own.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd shacl
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

The package is importable as `ossie_shacl` after installation:

```python
from ossie_ontology.parser import OssieParser
from ossie_shacl import OssieToShaclConverter

ontology = OssieParser().parse("ontology.yaml")

print(OssieToShaclConverter.convert(ontology, schema_id="https://example.org/my-ontology"))
```

`schema_id` is the base IRI of the shapes, classes and properties. An Ossie ontology has no IRI of its own, so without it a placeholder built from the ontology's name is used. `output_format` is `ttl` (the default) or `nt`. `convert_spec` does the same from an `OssieSpec`, without building the runtime model.

## Scripts

### `scripts/ossie_to_shacl.py`

Converts an Ossie ontology (YAML or JSON) into SHACL shapes, printed to stdout as Turtle. Warnings about what was dropped are written to stderr.

**Usage:**

```bash
uv run python scripts/ossie_to_shacl.py path/to/ossie.yaml
# Second argument is optional, and if provided will be used as the base IRI:
uv run python scripts/ossie_to_shacl.py path/to/ossie.yaml https://example.org/my-ontology
# Closed shapes:
uv run python scripts/ossie_to_shacl.py --closed path/to/ossie.yaml
```

## Running the tests

```bash
uv run pytest
```
