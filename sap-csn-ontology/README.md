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

# SAP CSN → Ossie ontology converter

Reads a SAP **CSN** document (Core Schema Notation — the JSON behind CDS views,
CAP applications and data-product metadata) and writes an Apache Ossie
**ontology document**: concepts, identity, relationships, derivations,
verbalizations, and `ontology_mappings` to datasets. It works offline on files.

The input is CAP-style CSN. The converter does not check conformance to SAP's
CSN Interop Effective specification, and its test fixtures are not CSN Interop
Effective documents: they have no `$version` and carry a root `$comment`, which
that schema rejects.

```
ossie-sap-csn-ontology model.csn.json [-c curation.yaml] [-o model.ossie.yaml] [--name NAME] [--strict]
```

Warnings go to stderr, one per line. `--strict` turns them, and curation entries
that match nothing in the CSN, into exit status 1.

```
uv sync
uv run pytest
```

The complete mapping is specified in [docs/csn-mapping.md](docs/csn-mapping.md).
Its executable form is the fixture pair `tests/fixtures/sap-bp-slice.csn.json` →
`sap-bp-slice.ossie.yaml`: a Business Partner slice with compound identity,
associations in both directions, a check-table domain, a role subtype and a
data-product mapping. The same CSN is the input of the `examples/` Business
Partner ontology in apache/ossie.

## Relationship to the `sap-csn` converter

Two converters read SAP CSN; they target different layers of the spec.

| | `sap-csn` | `sap-csn-ontology` (this one) |
|---|---|---|
| Output | core semantic model: `datasets`, `fields`, `relationships`, `metrics` | ontology: concepts, `identify_by`, `extends`, `derived_by`, `requires`, relationships with `verbalizes`, `ontology_mappings` |
| Direction | CSN ⇄ Ossie, lossless round trip via an `SAP_CSN` custom extension | CSN → ontology only |
| A CSN entity | a dataset | an `EntityType` concept |
| A CSN element with a domain | a field with `datatype`, `label`, and the native `length` in the extension | a `ValueType` concept the entity has a relationship to |
| A `key` element | `primary_key` | an identifying relationship + `identify_by` |
| An association | a relationship with `from_columns`/`to_columns` | a relationship whose shape depends on which side's key the `ON` references |
| A role view (I_Customer) | a dataset | a derived subtype: `extends` + `derived_by: EXISTS(...)` |
| `@EndUserText.label` | `label` | `description`, and the raw material for `verbalizes` |
| Other annotations, `precision`/`scale` | kept in the `SAP_CSN` extension | reported as dropped (see known losses) |
| `length` on strings | kept in the `SAP_CSN` extension | `requires: LENGTH(x) <= n`, also recorded in `description` (see known parser findings) |

On the shared fixture `sap-bp-slice.csn.json`, `sap-csn` emits 3 datasets with
the same 4-part `primary_key` on `CustomerSalesArea`, 3 relationships
(`BusinessPartner__Customer`, `_SalesArea`, `CustomerSalesArea__Customer`) and
no metrics; the enum type goes to the extension's `other_definitions`. This
converter emits 8 value types and 3 entity types, 10 relationships (the two
inverse associations become one dual-verbalized relationship), and 2 concept
mappings. Both agree on key order and read the same `ON` conditions; they do not
share names, because one names tables and columns and the other names concepts
and relationships.

## Two inputs, and why

CSN is a complete description of *structure*. It is not a description of
*language*: it has no business-facing relationship names, no verbalization
sentences, and no statement of where a concept's population lives. Those come
from an optional **curation overlay**, kept apart so a reviewer can tell a
derived fact from a curated one.

| | Derived from CSN | Supplied by the overlay |
|---|---|---|
| Concepts, identity, `extends`, `derived_by` | all | — |
| Relationship structure, roles, multiplicity | all | — |
| Constraint expressions | all | may be replaced |
| `ontology_mappings` shape (incl. nested referent mappings) | all | — |
| Concept and relationship names | default | may be renamed |
| Verbalization sentences | template only | the real ones |
| Descriptions | label + `$comment` + recorded length | may be replaced |
| Dataset names, sources, columns | — | all |

On the shipped slice, converting **with no overlay** already yields all 11
concepts, all 10 relationships, both identity structures, the role subtype and
every multiplicity; the overlay adds 7 renames, 10 verbalization sets and the
data-product binding. Nothing in it changes structure.

## Mapping summary

| CSN construct | Ossie construct |
|---|---|
| `kind: entity` | `EntityType` |
| `kind: type`, scalar element with a domain | `ValueType` `extends` a built-in |
| `enum` (fixed values) | `requires: x == 'a' OR x == 'b' …` — closed world |
| `length` on strings | `requires: LENGTH(x) <= n`, as in the merged apache/ossie SAP example; also in `description` |
| `precision`, `scale` | recorded in `description`; reported as dropped |
| Check-table domain | description provenance only; population arrives via `ontology_mappings` |
| Single `key` | `identify_by` + one `OneToOne` identifying relationship |
| Compound `key` | one `ManyToOne` identifying relationship per part + a `requires` per part |
| `cds.Boolean` | unary relationship (no `roles`, no `multiplicity`) |
| Association to one, joined on the target's key (or managed) | one relationship to the target; the foreign-key element is folded in |
| Association to one, joined on the source's key | relationship to the target's identifier value type |
| Association to many that inverts a to-one on the target | folded into that relationship; verbalized in both directions |
| Association to many, otherwise | relationship with no `multiplicity` |
| Role view of another entity | `extends` + `derived_by: EXISTS ( Parent.rel )`; `identify_by` restated explicitly |
| Identity through an entity | nested `referent_mappings` |
| `@EndUserText.label`, `$comment` | `description`; verbalization templates |

## Known losses

| CSN construct | Why it does not convert | Where it belongs |
|---|---|---|
| `precision`/`scale` on decimals | no constraint form in the ontology layer | recorded in `description`, reported as a warning; a spec question |
| Annotations other than `@EndUserText.label` (`@ODM.entityName`, `@Semantics.*`, `@ObjectModel.*`, …) | dropped today. The pinned ontology schema accepts `annotations` on concepts, relationships and roles (apache/ossie#146), but this converter does not map CSN annotations into it; accepting a key is not the same as preserving its meaning | reported as dropped, one line per annotation; mapping them is a separate change |
| Association `ON` predicates | read for their shape only; a relationship carries no join predicate | open |
| `cds.Composition` containment | emitted as an association | open |
| Cardinality `min` | `Multiplicity` offers `ManyToOne` and `OneToOne` only | open |
| `cds.Timestamp` vs `cds.DateTime` precision | both become a datetime built-in (`DateTimeTz`, `DateTime`); µs vs s precision has no home | recorded in docs |
| Multi-language labels | no multi-language keys in the spec | future work |
| Actions and events | the ontology layer is structural | behaviour-layer proposal |
| Conversion routines (ALPHA) | representation, not meaning | `description`, by design |
| Aspects, `kind: context`, services | not read in v1 | future work |

Every dropped construct is **reported**: `Conversion.warnings` lists them and
`Conversion.dropped_annotations` names each annotation. Nothing is lost
silently; `tests/test_mapping_rules.py::TestKnownLosses` asserts this.

## Parser acceptance

The output is checked against `ontology/ontology.json` from the pinned `ossie`
submodule, and was run through the reference parser in [`../ontology`](../ontology)
(`OssieParser`). On the shipped fixture the parser accepts everything except one
construct: the **concept-qualified nested referent** `BusinessPartner.nr`, which
the spec's own `CustOrder.nr` example uses, is rejected because
`lookup_concept_relationship` prefixes the container name onto an already
qualified name (#129). This converter keeps the spec form. With that one reference
written bare, the parser resolves all 11 concepts, 10 relationships,
`Customer.identify_by == ['BusinessPartner.nr']`, the `EXISTS` derivation, and
both concept mappings.

**Known parser finding: `LENGTH`.** String lengths are emitted as
`requires: LENGTH(x) <= n`, the form the merged SAP Business Partner example in
apache/ossie uses. `LENGTH` is an `OSSIE_SQL_2026` function, but the reference
parser rejects it ("Unresolved reference 'LENGTH'"), and `ontology.md` does not say
which expression language `requires` uses. Each emitted length is reported as a
warning and kept in the description too. Same rule as #129: keep the project's
own form and report the parser's limit.

Two earlier parser findings (`datatype` on dataset fields; reverse-direction
verbalizations) are fixed on the current `ontology` converter.

## Deliberately out of v1

Actions and events; hierarchies; time-dependence; authorization; the reverse
direction; separate mapping documents (`ontology/mapping.json`) — the embedded
`ontology_mappings` form is emitted, which the schema still accepts and the
spec marks deprecated.

## Dependencies

Runtime: `PyYAML`. Tests: `pytest`, `jsonschema`, `referencing`. Schemas are
read from the `ossie` submodule; nothing is fetched. Run
`git submodule update --init` before `uv run pytest`.
