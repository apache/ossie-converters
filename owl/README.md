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
# Ossie OWL Converters

Converts between [OWL 2](https://www.w3.org/TR/owl2-overview/) ontologies and Apache Ossie ontologies.

| Module         | Direction |
|----------------|-----------|
| `ossie_to_owl` | Ossie model → OWL ontology (Turtle or N-Triples) |
| `owl_to_ossie` | OWL ontology (Turtle or N-Triples) → Ossie model |

Both converters work with an `OssieOntology` from
[`apache-ossie-ontology`](../ontology/README.md), which this package depends on
and resolves from `../ontology`.

Both directions go through [LinkML](https://linkml.io/): Ossie → LinkML → OWL, and OWL → LinkML → Ossie. All steps are done by the natively-compiled [ohi LinkML](https://linkml.ohi.dev/) library, through the [`neverblink-linkml`](https://pypi.org/project/neverblink-linkml/) package. This package only provides wrappers – please file any issues [here](https://github.com/NeverBlink-OSS/ohi/issues).

The mapping and its limitations are documented in the ohi LinkML docs: [Ossie to LinkML](https://linkml.ohi.dev/docs/importers/ossie/) and [LinkML to OWL](https://linkml.ohi.dev/docs/generators/owl/) one way, [OWL to LinkML](https://linkml.ohi.dev/docs/importers/owl/) and [LinkML to Ossie](https://linkml.ohi.dev/docs/generators/ossie/) the other. In short:

**Ossie → OWL**

- Each entity type becomes an `owl:Class`, with `rdfs:subClassOf` for the concepts it extends.
- Each relationship becomes an `owl:ObjectProperty` (to an entity type) or `owl:DatatypeProperty` (to a value type), with `rdfs:domain` and `rdfs:range`. Multiplicities and required relationships become cardinality restrictions on the class, and `identify_by` becomes `owl:hasKey` (a single `OneToOne` relationship becomes a required property instead).
- A value type that lists its allowed values becomes a class defined by `owl:oneOf` its values, which are named individuals. Any other value type is replaced by the XSD datatype it is built on, restricted by its value ranges, such as `xsd:decimal[>= -90, <= 90]`.
- `derived_by` rules and `ontology_mappings` (datasets, join paths, metrics) are dropped.

**OWL → Ossie**

- Each class becomes an entity type, with `extends` from its named superclasses.
- Each object or datatype property becomes a relationship of the concept in its `rdfs:domain`, with its `rdfs:range` as the role's concept. A functional property or `owl:maxCardinality 1` makes it `ManyToOne`, and a minimum cardinality makes it required.
- `owl:hasKey` becomes `identify_by`, a class defined by `owl:oneOf` becomes a value type that lists its values, and datatype restrictions become value ranges.
- Labels fill the verbalizations and comments the descriptions.
- A property with no `rdfs:domain` is dropped, since an Ossie relationship belongs to a concept. Things the import does not read, such as individuals outside an enumeration, property chains and general class axioms, are reported once per kind with `warnings.warn`. `owl:imports` are not followed.
- A pattern (`xsd:pattern`) becomes a `REGEXP_LIKE(...)` formula, which the expression language defines but the `apache-ossie-ontology` parser cannot read yet. Building the runtime model from such an ontology fails, unless formulas are kept as raw text with `OwlToOssieConverter(formula_factory=FormulaFactory())` (from `ossie_ontology.model`). See: https://github.com/apache/ossie-converters/issues/267

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd owl
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

The package is importable as `ossie_owl` after installation:

```python
from ossie_ontology.converter.ossie_to_spec.converter import OssieToSpecConverter
from ossie_owl import OssieToOwlConverter, OwlToOssieConverter

ontology = OwlToOssieConverter().convert_file("ontology.ttl")
print(OssieToSpecConverter.convert(ontology).dump_yaml())

print(OssieToOwlConverter.convert(ontology, schema_id="https://example.org/my-ontology"))
```

`OssieToOwlConverter`: `schema_id` is the IRI of the ontology, and the base IRI of its classes and properties. An Ossie ontology has no IRI of its own, so without it a placeholder built from the ontology's name is used. `output_format` is `ttl` (the default) or `nt`. `convert_spec` does the same from an `OssieSpec`, without building the runtime model.

`OwlToOssieConverter` reads Turtle or N-Triples. `name` names the ontology; by default it is the ontology's `rdfs:label` if that is a usable name, else the prefix of most of its terms. `metadata_language` picks the language of labels and comments in ontologies that contain multilingual strings. `convert_to_spec` stops at the Ossie document, without building the runtime model.

## Scripts

### `scripts/ossie_to_owl.py`

Converts an Ossie ontology (YAML or JSON) into an OWL ontology, printed to stdout as Turtle, or as N-Triples with `--format nt`. Warnings about what was dropped are written to stderr.

**Usage:**

```bash
uv run python scripts/ossie_to_owl.py path/to/ossie.yaml
# Second argument is optional, and if provided will be used as the ontology IRI:
uv run python scripts/ossie_to_owl.py path/to/ossie.yaml https://example.org/my-ontology
# N-Triples instead of Turtle:
uv run python scripts/ossie_to_owl.py --format nt path/to/ossie.yaml
```

### `scripts/owl_to_ossie.py`

Converts an OWL ontology (Turtle or N-Triples) into an Ossie-compliant YAML representation of it, printed to stdout. Before printing, it builds the Ossie runtime model from the document, so output that is not valid Ossie stops the run with an error. Warnings about what was not imported are written to stderr.

**Usage:**

```bash
uv run python scripts/owl_to_ossie.py path/to/ontology.ttl
# With a name for the ontology:
uv run python scripts/owl_to_ossie.py --name MyOntology path/to/ontology.ttl
# Labels and comments in German, for ontologies that carry translations:
uv run python scripts/owl_to_ossie.py --metadata-language de path/to/ontology.ttl
# Without parsing formulas in the check, for ontologies with patterns:
uv run python scripts/owl_to_ossie.py --raw-formulas path/to/ontology.ttl
```

## Running the tests

```bash
uv run pytest
```
