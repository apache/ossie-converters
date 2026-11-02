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

# Cross-converter interoperability

Repository-owned acceptance and pairwise checks for [issue #41](https://github.com/apache/ossie-converters/issues/41).
The registry includes **all 21 converter projects**, with explicit capability skips.
Cube, Databricks, and dbt remain the targets of the focused semantic regressions.
The agreed design and remaining milestones are in [the implementation plan](../docs/plans/issue-41-interop-matrix.md).

## Run

Run commands from the repository root after initializing the Ossie submodule:

```sh
git submodule update --init
uv sync --project interop --locked --python 3.12
uv run --project interop --locked python -m interop
```

The runner needs Python 3.11+ and uv. Databricks and Salesforce additionally need Maven and
Java 21+ on `PATH` (and a matching `JAVA_HOME` for Maven). Missing prerequisites
appear as `SKIP`; a preparation command that actually fails appears as `FAIL`.
Dependencies are prepared once per converter: `uv sync --locked --no-dev` for
Python converters with lockfiles and `mvn -B -DskipTests package` for Java converters.
Wisdom has no checked-in lockfile, so it uses an isolated `uv run --no-project
--with-editable .` environment; dependency resolution there is not locked. These commands
build the tools; converter unit tests remain in their existing suites.
Converter environments remain separate from the harness, and tracked converter
lockfiles are never regenerated.

Output defaults to a new timestamped directory under `interop/.runs/`, ignored by
Git. Every run retains a Markdown report, JSON results, the input manifest,
native exports, returned Ossie documents, and per-command stdout/stderr logs.
Commit IDs and dirty-checkout flags identify the source state. A requested
`--output` directory must not already exist, preventing stale outputs from
producing a false pass or overwriting earlier evidence.

```sh
# Restrict the registry to two converters; this runs both ordered pairs.
uv run --project interop --locked python -m interop --converters cube,dbt

# Reuse previously prepared environments and the previously built Databricks JAR.
uv run --project interop --locked python -m interop --no-prepare

# Attempt optional native gates. Cube requires a built checkout.
OSSIE_CUBE_REPO=/path/to/cube uv run --project interop --locked python -m interop --native

# Enforce failures, empty outputs, and unexpected skips.
uv run --project interop --locked python -m interop --strict
```

`--timeout SECONDS` applies to each subprocess, including dependency preparation.
Timed-out process groups are killed on POSIX systems. Logs go directly to files.

## Registered adapters and capabilities

| Converter(s) | Native path / limits |
|---|---|
| Cube, Databricks, dbt | Existing CLI runners; focused semantic regressions retained |
| GoodData | API adapter for declarative LDM JSON, using the converter's serialization helpers |
| Honeydew, Microsoft, NVIDIA, Omni, OrionBelt, Sigma, Wisdom | File/directory CLI import and export |
| ThoughtSpot | CLI export; adapter expands a directory into the complete sorted TML input set |
| Salesforce | Java CLI in a temporary staging directory; collects exactly one generated output without modifying source fixtures. Import requires its separately obtained Salesforce JSON schema |
| Wisdom Semantic View | API adapter for its native Snowflake semantic-view boundary and the intermediate core Ossie model |
| Hex, Snowflake | Export only; native-to-Ossie import is not implemented |
| Holistics | Compiled dataset JSON import and AML source export; re-import of generated AML requires a Holistics compiler |
| Polaris | Explicit skip: requires a provisioned live REST catalog, outside the file runner |
| Ontology, Palantir, RelationalAI | Explicit skip: uses the ontology domain/schema, requiring a separate matrix |

All adapters run in the owning converter's environment. New native-content
inspectors count format-specific model members; these checks do not imply vendor
compilation or platform acceptance. Holistics checks AML member declarations only.
Its selected import/export dialect is BIGQUERY and exports use the placeholder
connection `interop_warehouse`; no live connection is created.
Existing vendor fixtures are reused, plus a small independently authored Honeydew
workspace. OrionBelt installs its locked `obml-validation` extra so its native
semantic validation is available during export checks. Missing directions never
run a dummy successful command. The report includes a capabilities table even for registrations with no native fixture.
`import_requires_files` records repository-relative prerequisites that may be absent.
A missing prerequisite skips imports and target re-imports while leaving export
checks available. Salesforce needs
`salesforce/src/main/resources/schemas/salesforce-semantic-model-schema.json`;
its README describes how to obtain it. Rebuild its JAR after supplying the schema.
`import: null` requires an `import_skip` reason; `roundtrip_skip` names a format
bridge that is not available, and a `cwd`/`skip` entry disables an entire
registration with an explicit reason.

Focused dialect/key/source oracles remain scoped to their original three targets;
other formats currently receive native-content, schema, and inventory checks.

## Coverage and grading

Every registered native fixture is imported once and validated using the pinned
`ossie/validation/validate.py`, including its JSON Schema and semantic checks.
The explicit canonical fixtures are core TPC-DS and Flights; ontology and mapping
examples are intentionally outside this matrix.

- **Tier A:** every canonical document and every valid imported fixture is
  exported to every selected converter, including its own converter. Exported
  model members must exist; empty files, nested empty directories, logs, and a
  source-only Metric View do not establish success.
- **Tier B:** every imported fixture is exported to, then re-imported from, each
  other selected converter. The returned document must pass the pinned validator.
  Named datasets/physical sources, fields, metrics, keys, relationships, and
  extensions are compared. Before/after counts and structural changes are saved.
  Extension comparisons include the owning model, dataset, field, metric, or
  relationship, so moving an unchanged payload between objects is reported as loss.
  The full registry currently schedules 378 Tier A and 320 Tier B cases across
  16 native fixtures and two canonical documents. Unsupported directions and
  unavailable or invalid upstream sources remain visible as skips.
  For dbt targets, re-import exercises MetricFlow's manifest parser; Tier A
  alone checks JSON model content rather than MetricFlow compilation.
- **Tier C:** optional native gates run on successful exports. Cube's existing
  compiler gate is registered. Its result is separate from Tier A/B; an absent
  compiler remains visible as `SKIP` and does not invalidate other evidence.

Registered **metric-binding regressions** continue the relevant Tier B chain back
to its source-native format. Both the original native fixture and the final
native export must satisfy independently authored expectations. The initial
routes are Cube -> Ossie -> Databricks -> Ossie -> Cube for TPC-DS and a focused
two-table sales/customer fixture. These checks are reported separately from
the acceptance matrix and its degradation warnings.

Ten independent Ossie **document regression fixtures** continue to run against
Cube, Databricks, and dbt: three dialect-only cases, four key cases, and three source cases.
Their 30 results and the two native binding results remain separate from
the broad acceptance matrix. Each fixture has an independently authored, versioned assertion file.

| Grade | Meaning |
|---|---|
| `OK` | Applicable checks passed with no detected degradation |
| `LOSSY` | Diagnostics or named structural changes indicate loss or degradation |
| `EMPTY` | A successful command produced no meaningful model members |
| `FAIL` | Preparation, conversion, parsing, or validation failed |
| `SKIP` | A named prerequisite or upstream result prevented execution |

If source import fails, the source row records that failure and its dependent
cases are skipped with an explicit reason. Likewise, failed Tier A exports block
their Tier B re-imports. Other cases continue. The matrix is not expected to be
green initially; failures are evidence to investigate rather than allowances to
silently add to the harness.

**Exit codes:** default report-only mode returns 0 even when converters fail or
are unavailable; configuration/harness/reporting errors return 2. `--strict`
returns 1 for `FAIL`, `EMPTY`, or unexpected `SKIP` in preparation, source,
Tier A/B cases, or registered regressions, and for an attempted native gate that
fails. A skip declared through `skip`, `import_skip`, or `roundtrip_skip` has
`expected_skip: true` in JSON; dependent source imports, cases, and regressions
inherit that marker. These declared capability skips do not fail strict mode.
Missing toolchains, prerequisite files, failed upstream stages, and unmarked
skips remain blocking. Optional native-gate skips do not fail strict mode.
`LOSSY` remains reportable and is not automatically blocking. Native-gate failures never overwrite Tier A/B grades.
Document regression failures, empty outputs, and unavailable required targets
also block strict mode. Their diagnostics and expected/actual assertions are
retained in report-only mode.

The harness is **not a SQL-equivalence or full-fidelity proof**. Physical bindings
are checked only for registered native regression metrics. General SQL expression
equivalence, metadata, data types, and AI context remain unchecked. Renames or
equivalent extension serialization can conservatively appear as structural changes.

## Dialect, key, and source assertions

`document_regressions` entries declare a category, Ossie input, selected targets,
and an expectations file:

```yaml
document_regressions:
  keys-single:
    category: keys
    input: interop/fixtures/contracts/keys-single/input.yaml
    targets: [cube, databricks, dbt]
    expectations: interop/fixtures/contracts/keys-single/expectations.yaml
```

The runner first checks the input against the pinned validator and the independent
assertions. It then exports to the target, checks meaningful native content,
re-imports, validates the returned Ossie document, and checks the assertions again.
A converter subset filters the target lists. These fixtures do not rewrite
sources, insert fallback dialects, or relax key assertions to make targets pass.

| Category | Fixtures | Required evidence |
|---|---|---|
| `dialect` | DATABRICKS-only, SNOWFLAKE-only, OSSIE_SQL_2026-only | Every input expression has only the declared dialect; fields, computed expressions, and metrics survive conversion |
| `keys` | Single primary/alternate keys, composite primary key, composite unique-only key, joined fact/dimension keys | Key declarations, their roles, compound grouping, and exposed key fields survive |
| `sources` | Two-part, three-part, quoted identifiers containing dots | Physical table identity survives in both native metadata and returned Ossie |

Dialect fixtures have one dataset, a computed discount field, a computed uppercase
field, and an aggregate metric. Expression assertions compare SQLGlot-normalized
syntax trees through a common rendering, allowing the dialect label to change
and normalizing bare versus own-dataset-qualified columns. They check every
returned dialect alternative. This deliberately bounded comparison is not an
algebraic or SQL-value equivalence proof, and it does not run warehouse queries.
An accidental ANSI fallback in a dialect-only input fails the fixture's baseline
check before conversion.

Key assertions preserve primary versus unique roles exactly. Replacing a primary
key with an equivalent unique declaration is reported as a role change even if
uniqueness still holds. Column ordering inside a key is immaterial; splitting a
compound key into independent singleton keys fails. Joined-dataset keys are
checked independently, and key fields explicitly exposed by the fixture must
remain present.

Source assertions compare parsed identifier parts, preserving quoted dots and
quoted case while normalizing unquoted case. Native checks inspect Cube
`sql_table`, Metric View root/join `source`, and dbt's `node_relation`. dbt's
catalog, schema, and alias slots are checked individually: reconstructing the
same full string from incorrectly split slots cannot pass. Each physical source
must be one two- or three-part reference; SQL queries and multiple statements
are outside these source fixtures.

Artifacts live under `document-regressions/<fixture>--to--<target>/`: input and
expectation snapshots, conversion outputs, command logs, and `result.json`.
The Markdown report includes failed assertion paths and their expected/actual
values. A scoped document result can be `OK` while unrelated warnings or inventory
changes remain in its evidence, just as with the native binding regressions.

Initial findings remain visible as incompatibilities rather than pass allowances:
Databricks rejects foreign-only expressions and unsupported source forms, drops
fact-table keys, and recovers joined keys with changed roles. dbt rejects compound
keys; quoted source references containing dots expose incorrectly split native
slots even when its reconstructed source string survives the round trip.

## Native metric-binding assertions

The top-level `regressions` mapping declares a source converter, one of its native
fixtures, a `via` converter, and a versioned expectations file. A converter subset
includes a regression only when both participants are selected.

```yaml
regressions:
  cube-databricks-tpcds:
    source: cube
    fixture: tpcds
    via: databricks
    expectations: interop/fixtures/bindings/tpcds.yaml
```

Each metric has an exact multiset of aggregate bindings:

```yaml
version: 1
metrics:
  total_sales:
    - function: sum
      source: tpcds.public.store_sales
      column: ss_ext_sales_price
      grain: tpcds.public.store_sales
```

`source` and `column` identify the physical column read. `grain` identifies the
physical table of the cube that performs the aggregation. These are independent:
a sum placed on `customer` that reads `{store_sales.ss_ext_sales_price}` still
has the wrong aggregation grain. Decomposed ratio measures are followed through
their actual native member references; expected bindings include all their
aggregate parts, even when each part lives on a different cube.

The inspector reads Cube YAML and SQL directly. It uses no converter helpers and
ignores stashed Ossie metadata. It follows raw `{CUBE}.column`, dimension/member
references, same-cube `{member}` shorthand, and cross-cube member references.
Dimension aliases are resolved to physical columns. A bare Cube `type: count`
with one primary-key dimension is represented as `count_distinct` over that key.
SQL comments and literals do not become template dependencies.

Supported aggregate functions are sum, average, minimum, maximum, count, and
distinct count. Missing/ambiguous public metrics, cycles, unresolved references,
SQL queries/windows/unmodelled functions, filtered/staged members, inherited or
SQL-backed cubes, and unsupported count-key forms fail the declared assertion.
They never silently become successful checks. This is a bounded lineage/grain
check; it does not compare arithmetic, filters, join paths, fan-out behavior,
view exposure, or SQL values in general.

`regressions/<name>/result.json` records expected/actual bindings per metric and
the original fixture check. The same directory retains the expectations file,
final native model, return-export logs, and an optional native compiler result.
`FAIL`, `EMPTY`, or an unexpected dependency `SKIP` in a registered regression
blocks `--strict`; declared capability skips retain their exemption.
A regression's `OK` means those binding assertions passed; other model losses
and conversion warnings remain visible separately.

Diagnostic counts retain one complete issue per Microsoft scoped message,
OrionBelt warning-block entry, and ThoughtSpot JSON issue (including its severity,
object reference, and remedy). Cube issue lines, dbt typed warnings, Databricks
notices, and generic Python warnings also remain supported. Informational issues
are retained without making a stage `LOSSY`; warning/error diagnostics indicate
degradation. The report's `Issues` column includes informational issues.
Foreign-extension counts are a conservative text heuristic, not an exact measure
of extension loss. Source diagnostics stay on source rows; they are not counted
against every downstream exporter.

## Manifest

`manifest.yaml` is versioned and validated before execution. Each entry declares
the converter's working directory, toolchain, preparation command, import/export
argument arrays, native format, output shape, fixtures, and optional native gate.
Paths are relative to the repository root and must stay inside it. Working
directories and fixtures must exist; import prerequisite files may be absent. Fixture and
converter identifiers use lowercase letters, digits, underscores, and hyphens.

Import/export commands require `{input}` and `{output}` placeholders. `{root}`
is also available. A native gate can use `{input}` for the export path and
`{files}` as a complete argument that expands to the discovered native files.
Commands never use a shell. All runnable registered formats have explicit
native-content inspectors; new formats require a small inspector/adapter.
For `--no-prepare`, Maven is unnecessary if the JAR already exists, but Java is
still required.

The dbt fixture at `fixtures/dbt/semantic_manifest.json` is a small, independently
authored native manifest with an entity, dimension, measure, and simple metric.
It is not generated through the exporter under test. Cube and Databricks reuse
their existing checked-in TPC-DS native fixtures. The independent Cube fixture
at `fixtures/cube/metric_bindings/` separates fact-table revenue, customer count,
and customer credit aggregates. `fixtures/bindings/` holds the physical-binding
oracles for both Cube fixtures.

JSON artifact paths are relative to the run directory. Recorded command argument
arrays and working directories retain their original values so the report also
records exactly what ran. Keep the entire run directory when sharing artifacts.

## CI publication

[Interop matrix](../.github/workflows/interop-ci.yml) runs the full registry on
pull requests to `main`, pushes to `main`, and manual `workflow_dispatch` runs.
Automatic runs are scoped to changes in converter directories, `interop/`, the
Ossie submodule pin, `.gitmodules`, or the workflow itself. Changes to any root
`*/pyproject.toml` also trigger the registration checks, so a newly added Python
converter cannot silently bypass the matrix. This initial rollout
uses the full matrix for relevant PRs, covering each changed converter as both
source and target. Changed-converter selection can reduce that cost later.

The Ubuntu job sets up Python 3.12, uv, Java 21, and Maven dependency caching,
runs the harness tests, then runs:

```sh
uv run --project interop --locked python -m interop --native --output interop/.runs/ci
```

Converter preparation and conversion failures, empty outputs, losses, and named
prerequisite skips remain **report-only**. Harness/configuration errors, harness
test failures, and artifact publication errors fail the job. The matrix step
uses the runner's existing exit policy with no `--strict` or `continue-on-error`.
Native gates retain their explicit prerequisites; CI does not provision Cube,
a Holistics compiler, a Salesforce schema, or a live Polaris catalog.

After the run, two steps use `always()` so a runner failure still allows evidence
publication and a summary. The Actions job summary contains grade counts for
each tier and regression group plus a link to the uploaded artifact. Missing or
invalid results produce a diagnostic summary and a publication error. The summary
does not render artifact-relative links that would break in the Actions UI.

Download `interop-<run-id>-<attempt>` from that workflow run's artifacts. It is
retained for **14 days** and contains the complete `interop/.runs/ci/` contents:
`report.md`, `results.json`, the manifest snapshot, native exports, returned Ossie
documents, regression inputs/oracles, per-case results, and stdout/stderr logs.
Extract it before opening `report.md` so all relative evidence links work.
Hidden files are included only within this run directory; environments and other
local runs are excluded. The job needs only `contents: read` and also supports
ordinary fork pull requests without service credentials or PR comments.

## Test and next steps

```sh
uv run --project interop --locked pytest interop/tests
```

Tests exercise real subprocess boundaries with controlled converter doubles,
including timeouts, failed preparation, failed sources/exports, silent key loss,
empty artifacts, invalid returned Ossie, environment isolation, strict exit
codes, and report persistence. Binding mutations cover wrong-table attribution
(including a column present on both tables), wrong aggregation grain, changed
functions/keys, missing measures, and unsupported SQL. Default tests do not
require Maven or vendor services. Native-return and document regression integration
tests are opt-in after preparing the selected converters and selecting Java 21+
on `PATH`:

```sh
OSSIE_INTEROP_INTEGRATION=1 uv run --project interop --locked pytest interop/tests -m integration
```

Next: inspect the first GitHub-hosted publication and triage converter findings.
Any canonical Tier A ratchet remains a separate decision; converter findings
continue to be report-only.
