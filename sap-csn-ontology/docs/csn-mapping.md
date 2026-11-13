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

# CSN → Apache Ossie ontology: mapping semantics

This document defines what the converter does, construct by construct. It is the
semantics description that accompanies the implementation and the
`sap-bp-slice.csn.json` ↔ `sap-bp-slice.ossie.yaml` fixture pair; the fixture
pair is the executable form of everything stated here.

**Source format.** CAP-style CSN (Core Schema Notation) — the JSON
representation behind CDS views, CAP applications, and data-product metadata.
**Target format.** The ontology layer of the Apache Ossie specification
(`ontology/ontology.json`, v0.2.0.dev0, as pinned by the `ossie` submodule of this repository).

## 0 · Two inputs, one output

CSN describes *structure*: entities, keys, element types, fixed values,
associations and their join conditions. It does not describe the *language* a
business model is read in — relationship names an analyst would recognize,
verbalization sentences an agent consumes — and it does not say where the
population lives.

The converter therefore takes two inputs:

| Input | Supplies | Required |
|---|---|---|
| CSN document | every structural decision below | yes |
| Curation overlay | names, verbalization sentences, descriptions, dataset bindings | no |

Without an overlay the converter emits a valid ontology with mechanically
derived names and template verbalizations. The overlay never changes structure:
it renames, rewords, and binds. Keeping the line visible is deliberate — it is
what lets a reviewer tell a derived fact from a curated one.

## 1 · DDIC domain → `ValueType` + `requires`

Every elementary CSN element carries a type, usually a length, and sometimes a
fixed-value enumeration. Each becomes a value-type concept extending an Ossie
built-in.

```json
"BusinessPartnerCategory": {
  "type": "cds.String", "length": 1,
  "@EndUserText.label": "Business Partner Category",
  "enum": { "Person": {"val": "1"}, "Organization": {"val": "2"}, "Group": {"val": "3"} }
}
```

```yaml
- concept: BusinessPartnerCategory
  type: ValueType
  extends: [ String ]
  requires:
    - "BusinessPartnerCategory == '1' OR BusinessPartnerCategory == '2' OR BusinessPartnerCategory == '3'"
```

Rules:

- The value type is named from `@EndUserText.label` (pascal-cased), falling back
  to the element name. SAP labels carry the domain's business name.
- `enum` → one disjunction of equalities, values in CSN declaration order.
  String values are quoted; numeric values are not. The ontology expression
  language has comparison and boolean operators but no `IN`; the spec's own
  corpus (`flights.yaml`, `CancelationCode`) writes closed sets this way.
- `length` is **recorded, not modeled**: the expression language defines no
  functions, so there is no portable `LENGTH(x) <= n`. The length is appended to
  the value type's description (`CSN length 10.`) and reported as a dropped
  constraint in the conversion warnings.
- `cds.*` scalar types map to Ossie built-ins (`String`, `Integer`, `Decimal`,
  `Float`, `Boolean`, `Date`, `Time`, `DateTime`, `DateTimeTz`).
- Elements across entities that resolve to the same value-type name produce one
  concept. Disagreement on type or constraints keeps the first definition and
  reports a warning rather than emitting two concepts with one name.

**Recorded, not modeled.** Conversion routines (ALPHA leading zeros) are a
representation concern; they belong in `description`, not in the semantics.

## 2 · Check table → a mapped population, not an enumeration

A country key checked against a several-thousand-row table must not become an
`IN (...)` list: that would be both huge and stale. The converter keeps only the
part of a check-table domain that is schema — its shape constraint — and leaves
the population to arrive through an ontology mapping.

```yaml
- concept: CountryCode
  type: ValueType
  description: "Country key. DDIC domain LAND1 (CHAR 3), check table T005. CSN length 3."
  extends: [ String ]
```

This is the same split the spec itself makes between `requires` and
`ontology_mappings`. The check table's identity survives in the description; the
converter never enumerates a check table's rows, and (see §1) it cannot state
the length as a constraint either.

## 3 · `key` elements → `identify_by` + identifying relationships

CSN always knows its keys, so `identify_by` is always populated — which makes
CSN-sourced models good citizens for transpilers into notations that require a
reference scheme.

- One key element → one identifying relationship, `multiplicity: OneToOne`, and
  no extra `requires`: `identify_by` plus one-to-one already says the identifier
  exists.
- Several key elements → one identifying relationship each, every one
  `multiplicity: ManyToOne`, plus a `requires` entry per part
  (`"Entity.part"`) stating that each part must exist.

```yaml
- concept: CustomerSalesArea
  type: EntityType
  identify_by: [ customer, sales_org, channel, division ]
  requires:
    - "CustomerSalesArea.customer"
    - "CustomerSalesArea.sales_org"
    - "CustomerSalesArea.channel"
    - "CustomerSalesArea.division"
```

## 4 · Associations

The join condition decides the shape. There are four cases, and the direction of
the `ON` clause is what distinguishes the two interesting ones.

| CSN | Condition | Emitted |
|---|---|---|
| to-many (`max: "*"`), inverse of a to-one on the target | mirrored `ON` | **not emitted**; its verbalizations join the target's foreign-key relationship |
| to-many (`max: "*"`), otherwise | any | relationship to the target entity, **no** `multiplicity` |
| to-one, `ON` references the **target's** key | foreign key | one relationship to the target entity, `ManyToOne`; the local foreign-key element does **not** also appear |
| to-one, no `ON` (managed) | CAP generates the key | same as a foreign key |
| to-one, `ON` references the **source's** key | back-reference | relationship to the target's *identifier* value type, `ManyToOne` |

The absence of `multiplicity` on a to-many association *is* the semantics: one
customer sells through many sales areas.

Folding matters. A CSN entity that carries both `Customer` (the foreign-key
element) and `_Customer` (the association) describes one fact, not two, so the
converter emits one relationship — at the foreign key's position, so element
order is preserved.

An inverse pair — `Customer._SalesArea` (to many, `ON _SalesArea.Customer =
Customer`) and `CustomerSalesArea._Customer` (to one, `ON _Customer.Customer =
Customer`) — describes one link. The converter emits it once, as the foreign-key
relationship on the many side, and verbalizes it in both directions, following
the spec's `Person.parent_of` pattern. Two independent declarations of the same
fact would otherwise have to be kept consistent by hand.

A back-reference is the opposite direction: the association is keyed on the
source's own identity, so it does not point at the target's population but at
the identifier the target is known by. If the target has no single-element
identifier, the converter points at the entity instead and warns.

Association `ON` conditions are read for their *shape*. The predicate itself has
no home in the ontology layer — see the known losses in the README.

## 5 · Compound identity → nested `referent_mappings`

Compound identity does its real work in the mapping layer. When an identifying
relationship points at another entity type, the referent mapping nests: the
sales-area row references its customer, which is itself referenced by business
partner number.

```yaml
- concept: CustomerSalesArea
  object_mappings:
    - referent_mappings:
        - relationship: customer
          referent_mappings:
            - relationship: BusinessPartner.nr
              expression: "CUSTOMER_SALES_AREA.CUSTOMER_ID"
        - relationship: sales_org
          expression: "CUSTOMER_SALES_AREA.SALES_ORG"
```

The converter computes this shape by walking the ontology's identity structure;
the overlay supplies only the column each identity slot is bound to. Nesting
follows `extends` when a concept declares no identity of its own, which is why
the inner reference is `BusinessPartner.nr` and not `Customer.nr`.

Qualification rule: a `ReferentMapping.relationship` is written bare when it is
declared on the concept being mapped, and qualified with its declaring concept
when it is inherited (`BusinessPartner.nr`, as in the spec's own `CustOrder.nr`).
A `LinkMapping.relationship` is always bare: the link mapping is already scoped
to its concept, and the spec's example and corpus write it that way.

An identity slot that the overlay does not bind is an error, not a silent gap.

## 6 · Role views → `extends` + `derived_by`

SAP's business-partner roles are neither declared subtypes nor separate
entities: they are populations defined by a condition. In CSN a role view shows
up as a to-one association from the parent whose `ON` condition equates the
*parent's own key* with a *non-key* element of the role entity. That direction
is the tell — a plain foreign key points at the target's key.

```yaml
- concept: Customer
  type: EntityType
  extends: [ BusinessPartner ]
  derived_by:
    - "EXISTS ( BusinessPartner.customer_nr )"
```

A role view redeclares no relationships: neither its own key element nor its
foreign key back to the parent becomes one. It does restate the parent's
`identify_by` by name (`identify_by: [ nr ]`). The spec does not say whether
`identify_by` is inherited through `extends`; the reference parser resolves the
name through `extends` but builds a concept's identifier list only from its own
entry, so an explicit declaration is the form that works under both readings. This is the closed-world reading of the role concept —
a Customer is exactly a BusinessPartner that carries a customer number.

## 7 · Labels → `description` and `verbalizes`

`@EndUserText.label` and `$comment` become the derived `description` of a
concept, value type, or relationship.

`verbalizes` is required by the ontology schema, so the converter always emits
at least one sentence. Derived templates:

| Relationship | Template |
|---|---|
| identifying, one-to-one | `{E} is identified by {V}` / `{V} identifies {E}` |
| to a value type | `{E} has {V}` |
| to an entity type | `{E} references {T}` / `{T} is referenced by {E}` |
| to-many | `{E} has many {T}` / `{T} belongs to {E}` |
| unary flag | `{E} is flagged <label>` |
| inverse to-many (joins the FK relationship) | `{E} has many {T}` |

These are placeholders. A curated model replaces them with the language its
agents should read — that replacement is the overlay's main job, and the reason
the derived and curated halves are kept apart.

## 8 · Boolean elements → unary relationships

A flag is a proposition about the entity, not a value it has, so a `cds.Boolean`
element becomes a relationship with no `roles` and no `multiplicity`:

```yaml
- name: delivery_blocked
  verbalizes:
    - "{CustomerSalesArea} is blocked for delivery"
```

## 9 · The mapping target is a data product

`ontology_mappings` binds concepts to datasets. The embedded `semantic_model` is
a complete core-spec document, so it carries its own `version` (required since
the pinned spec). The spec's version history marks embedded `ontology_mappings`
as deprecated in favour of separate mapping documents (`ontology/mapping.json`);
v1 emits the embedded form, which the schema still accepts, and the split is
future work.

`ontology_mappings` binds concepts to datasets. Dataset names, sources, primary
keys and field names come from the overlay; the converter wraps each field in a
portable multi-dialect expression and derives the mapping structure:

```yaml
fields:
  - name: CUSTOMER_ID
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "CUSTOMER_ID"
    datatype: String
```

Identity bindings become `object_mappings`; attribute bindings become
`link_mappings` whose children are qualified by their concept. A concept with no
bound attributes emits no `link_mappings`. Expressions are emitted in the
portable ANSI SQL core.

## 9a · CDS scalar types

| CDS type | Ossie built-in | Note |
|---|---|---|
| `cds.String(n)` | `String` | `n` recorded in the description, not modeled |
| `cds.LargeString` | `String` | unbounded (NCLOB-class); no length to record |
| `cds.Integer` | `Integer` | CAP's `Integer` is 32-bit; `cds.Integer64`/`Int64` also → `Integer` |
| `cds.Decimal(p, s)` | `Decimal` | `p`/`s` not modeled: no portable constraint form |
| `cds.Double` | `Float` | |
| `cds.Boolean` | — | becomes a unary relationship, not a value type |
| `cds.Date` | `Date` | |
| `cds.Time` | `Time` | |
| `cds.DateTime` | `DateTime` | CAP: second precision |
| `cds.Timestamp` | `DateTimeTz` | CAP: microsecond precision, UTC; the precision difference is not modeled |
| `cds.UUID` | `String` | |
| `cds.Binary`, `cds.LargeBinary` | `Opaque` | |

## 10 · Naming

All derived names are pure functions of the CSN, so the same model always
produces the same output.

| Kind | Rule | Example |
|---|---|---|
| Concept | entity name without its CSN namespace | `sap.slice.Customer` → `Customer` |
| Value type | `@EndUserText.label`, pascal-cased | `"Sales Organization"` → `SalesOrganization` |
| Relationship | element name minus a redundant entity prefix, snake-cased | `BusinessPartnerCategory` on `BusinessPartner` → `category` |
| Relationship (association) | same, leading underscore dropped | `_Customer` → `customer` |
| Relationship (to-many) | same, `has_` prefixed | `_SalesArea` → `has_sales_area` |

A prefix is only dropped at a word boundary, and never when it would leave an
empty name. Where a business-facing model wants a shorter name than the rule
produces, the overlay renames it explicitly — the converter does not guess at
abbreviations.

## 11 · Determinism

Output is a pure function of its inputs. Value types are emitted in derivation
order, entities in CSN declaration order, relationships in element order, and
constraint lists in the order their source constructs appear. Nothing time-,
locale-, or environment-dependent enters the document, and repeated values are
written out in full rather than as YAML anchors.
