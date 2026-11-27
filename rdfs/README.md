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

# Ossie RDFS Converters

Converts between [RDFS](https://www.w3.org/TR/rdf-schema/) vocabularies and Apache Ossie ontologies.

| Module          | Direction |
|-----------------|-----------|
| `ossie_to_rdfs` | Ossie model → RDFS vocabulary (Turtle or N-Triples) |
| `rdfs_to_ossie` | RDFS vocabulary (Turtle or N-Triples) → Ossie model |

Both converters work with an `OssieOntology` from
[`apache-ossie-ontology`](../ontology/README.md), which this package depends on
and resolves from `../ontology`.

Both directions go through [LinkML](https://linkml.io/): Ossie → LinkML → RDFS, and RDFS → LinkML → Ossie. All steps are done by the natively-compiled [ohi LinkML](https://linkml.ohi.dev/) library, through the [`neverblink-linkml`](https://pypi.org/project/neverblink-linkml/) package. This package only provides wrappers – please file any issues [here](https://github.com/NeverBlink-OSS/ohi/issues).

The mapping and its limitations are documented in the ohi LinkML docs: [Ossie to LinkML](https://linkml.ohi.dev/docs/importers/ossie/) and [LinkML to RDFS](https://linkml.ohi.dev/docs/generators/rdfs/) one way, [RDFS to LinkML](https://linkml.ohi.dev/docs/importers/owl/) and [LinkML to Ossie](https://linkml.ohi.dev/docs/generators/ossie/) the other. RDFS is much less expressive than Ossie, so a round trip through RDFS loses a lot. In short:

**Ossie → RDFS**

- Each entity type becomes an `rdfs:Class`, with `rdfs:subClassOf` for the concepts it extends. A value type that lists its allowed values also becomes a class, with the values as its instances (an enumeration).
- Each relationship becomes an `rdf:Property` with `rdfs:domain` and `rdfs:range`. A relationship to a value type gets the XSD datatype that value type is built on.
- Multiplicities, `requires`, `derived_by` rules and `ontology_mappings` (datasets, join paths, metrics) are dropped.

**RDFS → Ossie**

- Each `rdfs:Class` becomes an entity type, with `extends` from `rdfs:subClassOf`.
- Each `rdf:Property` becomes a relationship of the concept in its `rdfs:domain`. Its `rdfs:range` gives the role's concept: a built-in value type for an XSD datatype, the entity type for a class, and `Any` when the range says nothing (`rdfs:Literal`, or no range).
- `rdfs:label` fills the verbalization and `rdfs:comment` the description.
- A property with no `rdfs:domain` is dropped, since an Ossie relationship belongs to a concept. Things the import does not read, such as individuals, are reported once per kind with `warnings.warn`.
- The same importer can also read OWL ontologies, but this package is not tested with them.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd rdfs
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

The package is importable as `ossie_rdfs` after installation:

```python
from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_rdfs import OssieToRdfsConverter, RdfsToOssieConverter

ontology = RdfsToOssieConverter().convert_file("vocabulary.ttl")
print(OssieToSpecConverter.convert(ontology).dump_yaml())

print(OssieToRdfsConverter.convert(ontology, schema_id="https://example.org/my-ontology"))
```

`OssieToRdfsConverter` converts a runtime model (`convert`), an `OssieSpec` (`convert_spec`) or an Ossie document on disk (`convert_file`). `schema_id` is the base IRI of the classes and properties. An Ossie ontology has no IRI of its own, so without it a placeholder built from the ontology's name is used. `output_format` is `ttl` (the default) or `nt`.

`RdfsToOssieConverter` reads Turtle or N-Triples, from a string (`convert`) or a file (`convert_file`). `name` names the ontology; by default it is the vocabulary's `rdfs:label` if that is a usable name, else the prefix of most of its terms. `metadata_language` picks the language of labels and comments in vocabularies that contain multilingual strings; where there is no text in that language, text in another language or without a language tag is used instead. `convert_to_spec` stops at the Ossie document, and `to_model` builds the runtime model from it later.

A round trip, Ossie → RDFS → Ossie, does not give back the same document. Relationship names are rebuilt from the property IRIs, so `name` on `Person` comes back as `person_name`. Value types come back as the built-in they are built on, and multiplicities, `requires` and `identify_by` are gone.

## Scripts

### `scripts/ossie_to_rdfs.py`

Converts an Ossie ontology (YAML or JSON) into an RDFS vocabulary, printed to stdout as Turtle, or as N-Triples with `--format nt`. Warnings about what was dropped are written to stderr.

**Usage:**

```bash
uv run python scripts/ossie_to_rdfs.py path/to/ossie.yaml
# Second argument is optional, and if provided will be used as the base IRI:
uv run python scripts/ossie_to_rdfs.py path/to/ossie.yaml https://example.org/my-ontology
# N-Triples instead of Turtle:
uv run python scripts/ossie_to_rdfs.py --format nt path/to/ossie.yaml
```

### `scripts/rdfs_to_ossie.py`

Converts an RDFS vocabulary (Turtle or N-Triples) into an Ossie-compliant YAML representation of the ontology it describes, printed to stdout. Before printing, it builds the Ossie runtime model from the document, so output that is not valid Ossie stops the run with an error. Warnings about what was not imported are written to stderr.

**Usage:**

```bash
uv run python scripts/rdfs_to_ossie.py path/to/vocabulary.ttl
# With a name for the ontology:
uv run python scripts/rdfs_to_ossie.py --name MyOntology path/to/vocabulary.ttl
# Labels and comments in German, for vocabularies that carry translations:
uv run python scripts/rdfs_to_ossie.py --metadata-language de path/to/vocabulary.ttl
```

## Running the tests

```bash
uv run pytest
```
