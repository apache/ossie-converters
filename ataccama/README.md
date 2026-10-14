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

# Ataccama Ossie Converter

Importer from [Ataccama ONE](https://docs.ataccama.com/ataccama-one-agentic/latest/rest-api/rest-api-overview.html)
catalog metadata to the [Apache Ossie](https://github.com/apache/ossie)
semantic model specification. The output is a single model document with
`version`, `name`, and `datasets` at the root.

Given one or more Ataccama **catalog items** (selected by URN), the converter produces
a single Ossie semantic model: each catalog item becomes a dataset, each catalog attribute
becomes a field, business terms are carried across as `ai_context`, and
**data-quality scores** plus other governance metadata are attached as `ATACCAMA`
custom extensions.

## Scope & direction

- **Direction:** Ataccama → Ossie (import only). See [Limitations](#limitations) for why
  export (Ossie → Ataccama) is not provided.
- **Input:** explicit catalog-item URNs, and/or a connection, source, or location filter.
  Ataccama catalogs are large (tens of thousands of items, many of which are BI artifacts
  rather than tables), so the converter is always scoped rather than crawling everything —
  but the filter lets you take a whole source without naming every table in it. Every
  scoping flag is a filter criterion and they are unioned: a location covers everything in
  its subtree, and a URN that matches nothing contributes nothing rather than failing the
  run (a named catalog item that matched nothing is reported as a warning, since a typo
  would otherwise quietly shrink the model). Only an empty result is an error.
- **Data access:** a live REST client that authenticates via OAuth2 client-credentials
  (tokens are short-lived and refreshed automatically) and follows the catalog API's
  cursor pagination.

## Configuration

The client reads connection settings from environment variables (or a `--env-file`):

| Variable | Description |
|---|---|
| `ATACCAMA_BASE_URL` | the tenant's host, e.g. `https://<host>` (the API root `https://<host>/api` works too) |
| `ATACCAMA_CLIENT_ID` | service-account client id |
| `ATACCAMA_CLIENT_SECRET` | service-account client secret |
| `ATACCAMA_TENANT` | optional: the tenant whose Keycloak realm issues tokens; defaults to the tenant in the scope URNs (`urn:ata:<tenant>:...`) |
| `ATACCAMA_TOKEN_URL` | optional: the OAuth2 token endpoint, overriding the one derived from the tenant (`https://<host>/auth/realms/<tenant>/protocol/openid-connect/token`) |

> Never commit credentials. Keep them in a git-ignored env file.

## Usage

```bash
# CLI
ataccama-to-ossie --env-file .ataccama.env \
    --urn urn:ata:<tenant>:catalog:catalog-item:<id> \
    --urn urn:ata:<tenant>:catalog:catalog-item:<id> \
    --model-name my_model \
    --output my_model.ossie.yaml

# data-quality results are fetched by default; use --no-dq to skip them
ataccama-to-ossie --env-file .ataccama.env --urn <urn> --no-dq -o my_model.ossie.yaml

# opt-in: append DQ warnings to ai_context for datasets Ataccama flags as below quality
ataccama-to-ossie --env-file .ataccama.env --urn <urn> --dq-ai-warnings -o my_model.ossie.yaml

# or supply a file of URNs (one per line)
ataccama-to-ossie --env-file .ataccama.env --urns-file items.txt -o my_model.ossie.yaml

# every catalog item in a source, without naming each one
ataccama-to-ossie --env-file .ataccama.env \
    --source-urn urn:ata:<tenant>:catalog:source:<id> -o my_model.ossie.yaml

# the same for a connection, or for everything under a location (its whole subtree,
# so a database or schema URN brings in every item beneath it)
ataccama-to-ossie --env-file .ataccama.env \
    --connection-urn urn:ata:<tenant>:processing:connection:<id> -o my_model.ossie.yaml
ataccama-to-ossie --env-file .ataccama.env \
    --location-urn urn:ata:<tenant>:catalog:location:<id> -o my_model.ossie.yaml

# scopes are unioned, so filters combine with each other and with named items
ataccama-to-ossie --env-file .ataccama.env \
    --source-urn <source-urn> --urn <catalog-item-urn> -o my_model.ossie.yaml

# sample a large source: converts the first 20 matches and warns that the rest were
# left out (it truncates rather than failing, so you need not know the count up front)
ataccama-to-ossie --env-file .ataccama.env --source-urn <source-urn> --max-items 20 -o sample.ossie.yaml

# compatibility: emit ai_context as plain strings instead of objects
# (both are valid Ossie, but e.g. Snowflake's parser only accepts the string form)
ataccama-to-ossie --env-file .ataccama.env --urn <urn> --flat-ai-context -o my_model.ossie.yaml

# compatibility: stamp a released spec version instead of this repo's in-development one
# (e.g. Snowflake rejects 0.2.0.dev0). The schema pins the version, so the result is
# deliberately out of spec and will not pass validation -- the converter warns.
ataccama-to-ossie --env-file .ataccama.env --urn <urn> \
    --flat-ai-context --ossie-version 0.1.1 -o my_model.ossie.yaml
```

```python
# Library — returns an ossie.OssieDocument from the apache-ossie package
from ataccama_ossie import AtaccamaClient, ataccama_to_ossie

client = AtaccamaClient(base_url="https://<host>", client_id="...", client_secret="...", tenant="<tenant>")
bundles = [client.fetch_bundle(urn) for urn in urns]
document = ataccama_to_ossie(bundles, model_name="my_model")
print(document.to_ossie_yaml())

# Filtering: find_catalog_items returns whole catalog items, so fetch_bundle_for enriches
# them without a second request each. Only the first page is read up front -- enough for
# .total -- and the rest stream, so enrichment starts immediately. ataccama_to_ossie takes
# any iterable, so each bundle is released once its dataset is built.
results = client.find_catalog_items(source_urns=["urn:ata:<tenant>:catalog:source:<id>"])
print(f"{results.total} catalog items match")
document = ataccama_to_ossie((client.fetch_bundle_for(i) for i in results), model_name="my_model")
```

## Development

```bash
uv sync --group dev    # or: python -m venv .venv && pip install -e . pytest jsonschema
uv run pytest
```

Tests run fully offline against a recorded catalog fixture
(`tests/fixtures/ataccama_bundles.json`, with license metadata and a `bundles` array) and validate the output against the Ossie schema.

## Concept mapping

| Ataccama ONE | Ossie Semantic Model |
|---|---|
| `CatalogItem` | `dataset` |
| `CatalogItem.name` | `dataset.name` (delimited if not already an identifier, de-duplicated if repeated) |
| `CatalogItem.locations` + name | `dataset.source` (best-effort dotted namespace) |
| `CatalogItem.description` (rich text) | `dataset.description` (flattened to plain text) |
| assigned `Term`s | `dataset.ai_context` (`synonyms` + `instructions`; a plain string with `--flat-ai-context`) |
| `CatalogAttribute` | `field` |
| `CatalogAttribute.name` | `field.name` (delimited: `Join Date` → `"Join Date"`) + quoted `ANSI_SQL` identifier expression carrying the physical name verbatim |
| `CatalogAttribute.dataType` | `field.datatype` (omitted, with a warning, for a type outside the mapping) |
| `CatalogAttribute.dataType` ∈ {DATE, DATETIME, TIMESTAMP, TIME} | `field.dimension.is_time = true` |
| `CatalogAttribute.description` / `comment` | `field.description` |
| attribute `Term`s | `field.ai_context` |
| `primaryKey` / `primaryKeyColumn` (Metadata Entities API) | `dataset.primary_key`, `dataset.unique_keys` |
| unique `index` / `indexColumn` (Metadata Entities API) | `dataset.unique_keys` |
| `foreignKey` / `foreignKeyColumn` (Metadata Entities API) | `relationships` (`from`/`to` + `from_columns`/`to_columns`) |
| DQ `overallQuality` + `dimensionResults` + findings + monitor threshold | dataset `custom_extensions` (`vendor_name: ATACCAMA`) → `data.dq` (`passed`, `failed`, `pass_rate_pct`, `threshold_pct`, `below_threshold`, `active_findings`, `dimensions[]`, `results_link`) |
| DQ per-attribute `overallQuality` | field `custom_extensions` (`vendor_name: ATACCAMA`) → `data.dq` (`passed`, `failed`, `pass_rate_pct`) |
| URNs, `dataType`, `columnType`, connection/source/stewardship/monitor | `custom_extensions` (`vendor_name: ATACCAMA`) → `data` |

### Data quality

Data-quality results (from the DQ API's latest processing on each item's primary
monitor) are fetched by default and attached to the `ATACCAMA` extension — overall
and per-dimension quality on the dataset, and per-column quality on each field. Pass
`--no-dq` to skip the DQ calls.

Notes:
- `pass_rate_pct` is Ataccama's overall quality (`overallQuality`) expressed as a
  percentage: `passed / (passed + failed)`. The DQ API returns only the pass/fail
  **counts**, not a preformatted percentage, so the converter formats it — the counts
  themselves are Ataccama's own, consistent with what the platform reports.
  `pass_rate_pct` is rounded to one decimal place for display; threshold comparisons
  use the unrounded ratio.
- `threshold_pct` / `below_threshold` come from the monitor's configured overall
  threshold (`overallDqThresholds`, fetched from the monitor-config endpoint) — i.e.
  the same pass/fail bar shown in Ataccama. They are omitted when the monitor has no
  overall threshold configured.
- `active_findings` is always reported (`0` = no open data-quality findings), so the
  clean state is an explicit signal rather than an omission.
- Dimensions with no evaluated records in the processing are omitted.
- Items without a published monitor/results simply carry no `dq` block (best-effort).

#### AI warnings (opt-in)

By default the DQ block is descriptive data only. With `--dq-ai-warnings`, a plain-language
warning is appended to `ai_context.instructions` for any dataset Ataccama flags as having
data quality issues (data quality below configured threshold or active findings), so a
downstream AI/BI tool reading the Ossie model (not just the vendor extension) is told to
treat the data with caution. Example:

> *Data-quality warning: 60.0% of quality checks passed on the latest run (below the
> configured 75% quality threshold). Verify this data before using it for analysis or
> automated decisions.*

Warnings are appended to any existing (term-derived) instructions, never replacing them,
and require DQ (so not usable with `--no-dq`).

## Relationships & keys

Keys and indexes are read via the Generic Metadata Entities API (`primaryKey`, `index`,
`foreignKey`, and their column children) and fetched by default; pass `--no-relationships`
to skip them.

- **Primary key** → `dataset.primary_key` (the key's columns, order preserved). Composite
  keys are supported. It is not restated in `dataset.unique_keys`: a primary key is a
  unique constraint by definition, and Snowflake rejects a semantic view that declares the
  same columns as both.
- **Unique indexes** → `dataset.unique_keys`, sorted by index name, excluding the index
  that backs the primary key. Only the columns the index marks unique are taken, so
  included (covering) columns stay out of the key. Keys are compared as column *sets*,
  since uniqueness does not depend on column order.
- **Foreign keys** → `relationships` (`from`/`to` datasets with paired
  `from_columns`/`to_columns`). The target is matched by table name and, when the foreign
  key names its schema, by the catalog item's schema location as well, so same-named tables
  in different schemas stay apart. A foreign key whose target does not match exactly one
  converted item is skipped with a warning.

Keys and relationships name physical columns and tables, but reference the `name`s the
model declares, delimited the same way, so they keep pointing at the right objects.

Keys are only present where the source system defined them (typically database tables, not
BI artifacts). Because Ossie requires both ends of a relationship to exist in the model, a
foreign key whose referenced table is **not among the converted catalog items is skipped**
— include both tables in the same run to get the relationship.

## Limitations

The Ataccama Catalog API is a **catalog / governance / data-quality** surface, while Ossie
is an **analytics semantic model**. Some Ossie constructs therefore have no source today:

- **Metrics** — Ataccama has no analytics metrics; none are emitted. (Data-quality
  scores are attached as `ATACCAMA` extensions, not as Ossie metrics.)
- **Data Trust Index (DTI)** — not exposed via REST. There is no DTI entity type or
  property in the metadata model; the score is computed by the UI/AI Agent from inputs
  (DQ %, ownership, terms, description) that the converter already carries, but no DTI
  *value* is retrievable, so none is emitted.
- **`source` string** — the API exposes only a folder hierarchy (`locations`) and
  `originPath`, not `database.schema.table`. `source` is a best-effort dotted namespace;
  the authoritative connection/source URNs are preserved in `custom_extensions`.
- **Expressions are physical** — attributes map to plain quoted column identifiers
  (`ANSI_SQL` only); there are no computed/multi-dialect expressions.
- **Export (Ossie → Ataccama)** is not provided: the public Catalog API can only `PATCH`
  `description`/stewardship/aliases on existing items and cannot create catalog items,
  attributes, or relationships.
- Items with no catalogued attributes (e.g. some dashboards/reports) produce a dataset
  with no `fields`.
- **Breadth costs time.** Only the catalog items themselves come back in bulk (the filter
  endpoint returns them whole, ~100 per request). Attributes, terms, DQ results and keys
  have no batch endpoint, so each item costs its own round-trips, and a broad filter is slow
  in proportion to what it matches. Items are streamed, so enrichment starts on the first
  page rather than after the whole scope has been paged through, but the finished model is
  still held in memory before it is written — relationships depend on every dataset name,
  so the document cannot be emitted incrementally. `--max-items` bounds both.
- **A narrowed scope narrows relationships.** Foreign keys pointing outside the converted
  set are dropped, since Ossie requires both endpoints of a relationship to exist. This
  applies to any scoping, `--max-items` included.
