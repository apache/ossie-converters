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

# Apache Ossie ↔ Celonis converter

A standalone file converter between **Celonis JSON**
and Ossie `0.2.0.dev0` core, ontology and standalone mapping
documents. It handles business objects, event sources, information systems,
field types, identities, descriptions, agent context, SQL bindings, data joins
and conceptual relationships.

The converter reads and writes local files. Its Celonis output uses the same
JSON format as its input. Creating or uploading Celonis assets is outside its scope.

## Setup and usage

Requires Python 3.11+ and the initialized `ossie` submodule. From this directory:

```bash
uv sync
uv run ossie-celonis celonis-to-ossie -i celonis.json -o model.yaml
uv run ossie-celonis ossie-to-celonis -i model.yaml -o celonis.json
uv run pytest
```

The first command writes `model.yaml`, `model.ontology.yaml` and
`model.mapping.yaml`. The reverse command automatically reads both companions
when present. A core file without companions still works; having only one
companion is an error. `--core-only` skips companions in either direction.
`--ontology PATH` and `--mapping PATH` select custom companion output paths on
import and input paths on export. Mapping references include relative file IRIs,
resolved from the mapping file's directory.

Ossie input/output can be JSON or YAML, including mixed companion encodings.
Celonis input/output is JSON.
`--name` overrides the imported model name (otherwise the common authoring
package key is used, or `celonis_model` for a mixed-package graph).
For new Ossie datasets, `--namespace` defaults to `ossie` and `--package-key`
defaults to the Ossie model name. An explicit `--package-key` also overrides
authoring origins of converted native entities and their bindings; it does not
rewrite preserved processes, perspectives or embedded source SQL.

For pip-based development, install the shared package before this converter:

```bash
python -m pip install -e ../ossie/python -e . pytest jsonschema
```

## Mapping

- `objects`, `eventSources`, and legacy `events` become datasets. Dataset names
  are `namespace/name`, with each component URL-escaped to prevent collisions.
  Objects and event sources with the same namespace/name are rejected because
  they would collide in the Ossie dataset namespace.
- `fields[].id` becomes the field name and a quoted ANSI SQL column reference.
  Logical types map to String, Integer, Decimal, Float, Boolean, Date, Time and
  DateTime. Unknown native types become Opaque and retain their original type in
  the dataset extension. All fields have dimension metadata; temporal datatypes
  supply the default time role.
- `primaryKeys` becomes `primary_key`. Legacy binding `primaryKeyColumns` is a
  fallback. Keys must refer to declared fields; namespace-qualified extension
  fields are not resolved in this version.
- `description` and `agentContext` become description and `ai_context`.
- SQL lives in `dataBindings`, resolved by the complete entity identity. A single
  SQL binding becomes `dataset.source`; multiple bindings with the same dialect
  become a `UNION ALL` query. The original SQL, dialect, templates and binding
  metadata stay in the extension. Missing bindings produce a warning and a
  logical source name rather than inventing a physical table.
- `INSTANCE_TO_INSTANCE` relationships with foreign key mappings become core
  joins. MANY_TO_ONE keeps its direction; ONE_TO_MANY reverses it; ONE_TO_ONE
  keeps its direction and original cardinality in the extension. Composite
  mapping order is retained. Dangling references and undeclared join fields fail.
- TYPE_TO_TYPE edges and many-to-many/junction relationships are represented as
  ontology relationships and also preserved in native model extensions. Processes,
  perspectives, event logs, PQL metrics and other containers remain in the model
  extension. They are not advertised as portable core joins or SQL metrics.

## Ontology and mapping documents

- Objects, event sources and legacy events become `EntityType` concepts extending
  `Any`. Event concepts describe event occurrences; Ossie has no dedicated
  process-mining event type. Information systems and additional native entities
  referenced by conceptual edges also become EntityTypes, without invented rows.
- Concept names derive from namespace/name, using safe identifiers and collision
  suffixes. The mapping extension retains their original Celonis identities.
- Representable fields become binary properties targeting built-in ValueTypes:
  String, Integer, Decimal, Float, Boolean, Date and DateTime. Time and Opaque
  fields remain in the core and native metadata with a warning, because the
  ontology has no equivalent built-in value type.
- Primary keys, or the first unique key when no primary key exists, become
  `identify_by` field properties. Composite identities use ordered referent
  mappings. An entity without a representable key remains a concept, but gets no
  row population mapping; the converter warns instead of inventing an identifier.
- Standalone mapping documents connect entity identities, non-key properties and
  foreign-key joins to quoted dataset/field references. Links are populated only
  when their foreign keys supply the target's identity. Missing identity/junction
  mappings produce warnings while retaining the conceptual relationships.
- Core joins become binary ontology relationships with ManyToOne multiplicity
  (OneToOne when the unchanged native join declares it). Unmapped instance edges,
  including M:N edges, retain conceptual connectivity without invented join SQL.
- TYPE_TO_TYPE edges become named predicates. The converter preserves their
  cardinality in extensions without assigning row-level ontology multiplicity.

On reverse conversion, concept and field-property descriptions can be edited;
existing type-edge descriptions/targets can be edited, and binary type edges can
be added or removed. New edges target declared EntityTypes and generate
TYPE_TO_TYPE Celonis relationships. Conflicting core/ontology descriptions fail.
Dataset fields, keys and joins are authored in the core model. Regenerate the
companions after structural core edits, or export using `--core-only`. Editing
mapping expressions, identities, inheritance, rules, derivations, arbitrary
annotations or native type-edge multiplicity is rejected rather than ignored.

The Python API exposes `celonis_to_ossie_bundle(celonis_json)`, returning
`semantic_model`, `ontology` and `mapping` dictionaries. Use
`build_ontology_documents(model)` to generate companions for a new or edited
core model. Pass both `ontology=` and `mapping=` to `ossie_to_celonis(model, ...)`
to apply them. Ontology export currently requires this converter's CELONIS
mapping extension; importing arbitrary third-party ontologies is not implemented.
Runtime schema validation uses packaged copies of the pinned Ossie schemas and
requires no network access.

## Extensions and round trips

Every generated extension uses `vendor_name: CELONIS`. Its `data` is a JSON
string with `format_version: 1`. Core documents use three `kind` values:

- `dataset`: original entity, bindings, source container and core projection.
- `relationship`: original relationship and core projection.
- `model`: unconverted containers, unconverted relationships/bindings, original
  container names and relationship/binding ordering.

The mapping document adds an `ontology` extension containing native identity
associations and the generated concept/mapping projections. This metadata
separates field properties, data joins and type edges during reverse conversion;
the ontology document itself contains only standard Ossie constructs.

An unchanged Celonis → Ossie → Celonis round trip restores the original JSON
content, including nulls and unsupported entities. Keeping the CELONIS extensions
is required for this fidelity. The JSON whitespace may change.

Changed core fields are applied on export rather than silently ignored. New or
changed row expressions require an ANSI_SQL or OSSIE_SQL_2026 expression; a
read-only STARROCKS binding is generated using a SELECT projection over the
dataset source. Existing source SQL is not transpiled. An unchanged source and
fields retain the original bindings. Extra Ossie dataset semantics are carried
as a JSON string in the native entity's `attributes.apache_ossie` property.
On import they remain in the preserved native metadata rather than being
reinterpreted as portable fields. This includes unique keys, dimension roles,
labels and other vendors' extensions.
Model-level metadata and other vendors' model extensions are carried in the
first entity's `attributes.apache_ossie_model`; relationship extensions are
carried in that relationship's `attributes.apache_ossie`. These native string
attributes preserve the additional Ossie metadata during export.

## First-version limits

- SQL metrics are rejected on export: translating SQL aggregations into PQL is
  not implemented. Native PQL metrics are preserved in the model extension.
- Celonis timestamps map to DateTime. New timezone-aware DateTimeTz fields are
  rejected because this converter cannot infer a faithful native type.
- Native dataset renaming is rejected; deleting datasets or changing joins may
  require updates to the preserved perspectives/processes outside this tool.
- Generated SQL assumes the chosen row expressions and source are compatible
  with StarRocks. Catalog/pool templates need resolution in the target environment.
- The ontology projection covers entities, properties and relationships.
  Process execution and PQL calculation semantics remain native extensions.
- M:N/junction edges have an ontology projection but no generated row population
  mapping in this version.
- Tests cover the converter's mappings using synthetic fixtures.
- The converter is registered with an explicit skip in the cross-converter
  matrix, whose canonical models require SQL metric export. Dataset and join
  conversion against the TPC-DS example is covered in this converter's tests.
