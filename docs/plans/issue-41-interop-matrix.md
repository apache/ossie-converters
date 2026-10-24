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

# Issue #41: cross-converter interoperability matrix

- Issue: [apache/ossie-converters#41](https://github.com/apache/ossie-converters/issues/41)
- Plan agreed: 2026-10-23
- Status: runner foundation, all 19 converter registrations, native metric-binding
  regressions, and dialect/key/source regression fixtures implemented. Results
  remain report-only. CI artifact publication is implemented locally; its first
  GitHub-hosted run and any later ratchet remain.
- Repository inspected at: `82d4e25`.
- Pinned Ossie submodule inspected at: `f098821eb16c05c49ca5631178e454f4367da452`.

## Objective

Create a repository-owned `interop/` test suite that detects failures between
converter pairs even when each converter's own round-trip tests pass. Report
acceptance, information loss, and native validity separately. A successful process
exit alone must never establish interoperability.

The motivating failures are empty exports, metrics attributed to the wrong
physical table, disappearing keys, incompatible source qualification, and
expressions available only in another vendor's dialect.

## Agreed scope and sequencing

The first iteration uses **Cube, Databricks, and dbt**. Omni is deferred to the
broader converter-registration phase, following the user's explicit preference.
The eventual harness remains inclusive; initial selection can be explained by
technical coverage:

| Converter | Initial coverage |
|-----------|------------------|
| Cube | Directory output, existing native compiler gate, original regression fixtures |
| Databricks | Java toolchain, single-file YAML, metric-attribution regression |
| dbt | Python toolchain, JSON semantic manifests, explicit loss diagnostics |

Run all six directed cross-converter pairs among these three, plus canonical
Ossie acceptance checks. Self-pairs may be useful controls but do not count toward
cross-converter coverage.

dbt exports `semantic_manifest.json`; the first iteration validates manifest
parsing and fidelity. Running dbt projects or executing queries is later work.

## Repository findings that affect the design

1. Converter directories are now top-level; the issue's `converters/...` paths
   need updating. Shared examples, schemas, and validation live in the pinned
   `ossie/` git submodule.
2. `cube/tools/interop_matrix.py` is the prototype. It is Cube-only as a source
   and covers only part of the converter set. It reports `EMPTY` without counting
   it toward the final failure exit code. Its directory-output check also accepts
   a directory containing only an empty subdirectory.
3. Databricks is Java-only in the maintained implementation. Wire its Maven/JAR
   runner in the first iteration instead of skipping every Java converter.
   Its CLI uses positional input paths:
   `java -jar <jar> export <ossie.yaml> -o <view.yaml>` and
   `java -jar <jar> import <view.yaml> -o <ossie.yaml>`.
4. dbt already exposes both directions:
   `ossie-dbt msi-to-ossie -i <manifest.json> -o <ossie.yaml>` and
   `ossie-dbt ossie-to-msi -i <ossie.yaml> -o <manifest.json>`.
   Its diagnostics distinguish dropped metrics and other semantic losses.
5. GoodData has no console script and needs a small Python API adapter. Its
   documented lack of metric conversion also demonstrates why diagnostic counts
   alone cannot establish fidelity.
6. Holistics exports AML source but imports compiled JSON. Re-import requires
   the Holistics compiler; do not pass exported source directly to its importer
   or claim full pairwise coverage without that prerequisite.
7. Diagnostics vary: Python warnings, Cube issue codes, Java conversion notices,
   and structured JSON/YAML issue files. A generic stderr warning regex is only
   a fallback and must not become the definition of loss.
8. `ossie/validation/validate.py` already includes JSON Schema validation,
   uniqueness, relationship-reference and column-arity checks, and SQL parsing.
   Reuse it with the pinned schema instead of maintaining a second validation
   implementation. These checks do not prove correct metric-to-table attribution.
9. `ossie/examples/` contains core, ontology, and mapping documents. Select core
   examples explicitly instead of sending every YAML file to every exporter.

## Architecture

### Manifest and adapters

Use a small declarative manifest, such as `interop/manifest.yaml`, recording:

- Converter identity and working directory.
- Toolchain and one-time preparation command.
- Import/export commands as argument arrays with explicit path placeholders.
- Native fixtures and required input options.
- Output shape and relevant native-file discovery rules.
- Diagnostic decoding and optional native-gate configuration/prerequisites.

Keep unusual integrations in small, explicit adapters, including GoodData's API
and multi-file formats. Do not require converter CLI migrations or build a general
plugin framework. Represent unsupported directions and prerequisites explicitly.

### Execution and evidence

- Prepare each converter once and import each source fixture once per run.
- Run ordered pairs independently and use fresh output directories per case.
- Capture stage commands, exit codes, stdout/stderr, structured diagnostics,
  validation results, content inventories, and generated artifacts.
- Apply subprocess timeouts and keep one failing pair from aborting the matrix.
- Attribute a failed source import to the source. Dependent cases should explain
  why they could not run, rather than blame every downstream exporter.
- Keep converter dependency environments isolated. Make dependency preparation
  explicit and avoid incidental changes to tracked lockfiles during execution.
- Produce machine-readable JSON and a Markdown summary linked to detailed
  evidence. Preserve the commit and submodule revision used for each run.

### Test tiers

| Tier | Execution | Required evidence |
|------|-----------|-------------------|
| A | Explicit core examples and valid imported fixtures -> each exporter | Successful command and meaningful native content |
| B | A native fixture -> Ossie -> B native -> Ossie prime | Valid source/intermediate Ossie, valid final Ossie, inventories and targeted fidelity assertions |
| C | Generated native artifacts -> real compiler/validator | Native acceptance, with its own status and prerequisite information |

Tier B must validate the source Ossie document before attributing rejection to B.
Include an explicit return to the source native format for the Cube-Databricks
metric-attribution regression.

Native-gate status remains separate from Tier A/B: an unavailable Cube compiler
must not erase successful acceptance or fidelity evidence.

### Content and fidelity

Check parsed, meaningful model content rather than file existence or byte count.
Exclude logs and issue files from native-output detection. Directory checks must
not accept empty nested directories as model output.

Record before/after inventories of datasets, fields, metrics, relationships,
keys, and extensions. Distinguish total emptiness from partial loss. Inventories
are evidence, not a semantic-equivalence proof.

Add a small regression corpus with explicit expected invariants:

- Expressions supplied only in a vendor dialect.
- Metric references bound to the intended physical source, including the original
  wrong-cube regression.
- Key preservation or explicitly reported key loss.
- Two-part versus three-part source handling.
- Relationship endpoints and column mappings.

Use conservative expression normalization where supported. Arbitrary SQL
equivalence is outside the first version. Unsupported comparisons must remain
visible instead of implying proven fidelity. Keep any expected-loss allowances
narrow and fixture-specific so they cannot conceal unrelated regressions.

### Grading

| Grade | Meaning |
|-------|---------|
| `OK` | All applicable checks passed with no detected loss |
| `LOSSY` | Valid, meaningful output with diagnosed loss or degradation |
| `EMPTY` | No meaningful model content; a failure |
| `FAIL` | Command, validation, or semantic assertion failed |
| `SKIP` | A named prerequisite or unsupported operation prevented the check |

Report warning and foreign-extension warning counts separately from detected
losses. Not every warning signifies data loss, and silent loss can occur without
a warning. Preserve diagnostic origin/stage so source-import notices are not
misattributed to the target converter.

## Implementation milestones

1. **Runner foundation.** Manifest validation, isolated subprocess execution,
   timeouts, fresh outputs, grading, JSON results, and Markdown reports. Add
   meaningful tests for empty output, command failures, missing prerequisites,
   source-stage failures, and report generation.
2. **First useful matrix.** Register Cube, Databricks, and dbt. Implement Java
   preparation now. Run the six directed pairs and canonical acceptance checks.
3. **Regression coverage.** Add the targeted dialect, metric-attribution, key,
   and source-qualification fixtures and fidelity assertions. Integrate the
   existing Cube native compiler gate without hiding its skipped state.
4. **Broader registration.** Add remaining converters, including Omni, the
   GoodData API adapter, and explicit compilation prerequisites for formats such
   as Holistics. Register unavailable integrations with clear reasons instead
   of silently omitting them. The Salesforce file runner is now wired; its import
   requires a separately supplied schema. Polaris remains a service prerequisite skip.
5. **CI rollout and ratchet.** Publish JSON/Markdown reports and intermediate
   artifacts first. Converter findings are initially report-only; harness
   failures still fail CI. Once ready, make Tier A on canonical TPC-DS blocking,
   with skips unable to satisfy required coverage. Expand blocking checks
   incrementally.

For pull requests, exercise a changed converter as both source and target against
the other registered converters. Run the full matrix for harness or pinned Ossie
submodule changes, and on main/manual runs. Reuse preparation and source imports
to control the cost of the pairwise matrix.

## Verification already performed during analysis

- Canonical `ossie/examples/tpcds_semantic_model.yaml` passed the pinned validator.
- Importing `cube/tests/fixtures/tpcds_cube` through the installed Cube CLI
  succeeded; the resulting Ossie document also passed the pinned validator.
- That import contained 5 datasets, 32 fields, 5 metrics, and 4 relationships,
  while reporting 6 conversion issues. This confirms why validation and
  degradation reporting must remain separate.
- Reproduced the prototype accepting a directory with only an empty subdirectory
  as produced output. Confirmed its `EMPTY` exit-code gap by code inspection.
- Inspected dbt's CLI and diagnostics; no full dbt interop run was performed.
- A full matrix has not been run. Maven was unavailable in the analysis session.

## PR review follow-up

PR #16 merged as `51f810e`; the interop branch has been merged with current
`main` (`62a3066`) without conflicts. The Cube CLI registration remains compatible.
Both Copilot findings are addressed: unquoted SQL qualifiers normalize case before
ownership checks while quoted identifiers retain case sensitivity, and complete
Microsoft/OrionBelt/ThoughtSpot diagnostics are retained. Informational issues stay
visible without making a stage LOSSY; the report column is now named Issues.

All 242 tests passed with integration enabled. The full updated matrix completed
630 cases with no harness errors and report-only exit 0: 62 OK, 231 LOSSY, 89 FAIL,
3 EMPTY, and 245 SKIP. Both Cube -> Databricks -> Cube regressions passed, and all
700 report evidence links resolve. Lint, formatting, and the license audit passed.
Evidence: `interop/.runs/pr-62-review-fixes/report.md`. The optional Codex CLI review
could not run because its configured model is unavailable; direct inspection and
regression tests were used. GitHub-hosted CI still requires maintainer approval.

## Guidance for a later session

Implementation entry point: [interop/README.md](../../interop/README.md).
The runner now registers **all 19 top-level converter projects**, including Omni
in this broader phase as requested. There are 16 native fixtures and two canonical
Ossie documents, scheduling 342 Tier A exports and 288 Tier B ordered returns.
The original Cube/Databricks/dbt document oracles and both native metric-binding
chains retain their original scope.

New registrations use native CLIs where available. `interop/adapters.py` provides
GoodData's API serialization, ThoughtSpot directory-to-file-set expansion,
Salesforce's isolated staging/output collection, and Wisdom Semantic View's core
Ossie API boundary. Every runnable native format has a member-content inspector.
The independently authored Honeydew workspace supplements existing vendor fixtures.

Capabilities and prerequisites remain explicit:

- Hex and Snowflake expose export only; target re-imports are skipped.
- Holistics imports compiled JSON and exports AML source. Re-import requires the
  Holistics compiler. Its ecommerce fixture currently fails on an unresolved query
  template, with the original diagnostics retained.
- Salesforce exports run, but imports skip until its separately obtained JSON
  schema is present and bundled into a rebuilt JAR. Do not replace it with the
  test-only schema or fetch a placeholder to make the matrix pass.
- Polaris requires a provisioned live REST catalog; it is registered as a skip.
- Ontology uses the ontology domain/schema and requires a separate matrix.
- Wisdom lacks a committed lockfile and uses an isolated editable uv environment.
  Other Python converters use their existing locked environments. Java runners
  require Java 21+, and Maven for preparation.

The full preparation run built all 17 runnable converters; Polaris and Ontology
remained explicit skips. Default report-only execution still returns 0 for
converter findings and prerequisite skips, and 2 for harness/configuration errors.
`--strict` remains opt-in. No CI blocking behavior was introduced.

All 191 harness tests passed with integration enabled, including the two real
Cube -> Databricks -> Cube chains. Lint, formatting, and the Apache license audit
passed. The latest report is `interop/.runs/registered-converters/report.md`,
with portable JSON results and native exports, returned documents, and command
logs alongside it. Across 630 scheduled matrix cases, the report records 81 OK,
211 LOSSY, 90 FAIL, 3 EMPTY, and 245 SKIP. Both binding regressions are OK; the
30 document cases remain 20 OK and 10 FAIL. There are no harness errors, default
exit status is 0, and all 700 report evidence links resolve. Run artifacts are
local and Git-ignored.

Read this plan and current repository instructions, inspect Git status, and
recheck assumptions against the current checkout before continuing. CI publication
is now implemented in `.github/workflows/interop-ci.yml`: full matrix runs on
relevant PRs/main pushes and manual dispatch, after blocking harness tests. The
runner keeps its existing report-only exit policy. Upload and summary steps use
`always()`; the entire run directory is retained for 14 days and linked from a
compact Actions summary generated by `interop/ci_summary.py`. Missing/invalid
results and upload errors fail publication. Workflow actions are SHA-pinned,
permissions are read-only, and the workflow uses ordinary `pull_request` events.
Dependabot now tracks the harness dependencies as well.

CI publication verification: all 202 tests passed with integration enabled, and
`actionlint` 1.7.12, Ruff, formatting, and the Apache license audit passed. A full
local rehearsal with Python 3.12 and JDK 24 rebuilt/prepared all 17 runnable
converters and completed 630 cases with report-only exit 0 and no harness errors.
Counts match the registry run above. Its report is
`interop/.runs/ci-rehearsal/report.md`. The summary command ran with standard
Python, and a locally archived/extracted artifact retained all 700 report links.
The first actual GitHub upload/summary remains pending a push of this workflow.

The next step is to inspect the first GitHub-hosted run after these changes are
pushed. PR selection optimization and a canonical Tier A ratchet remain later
decisions. Converter incompatibilities remain findings; do not turn them into
broad expected-pass allowances.

The user's verification preference is to use builds, lint, tests, and
command-line checks. Do not use a browser for development, debugging, or
verification unless explicitly requested.

Suggested continuation prompt:

> Continue docs/plans/issue-41-interop-matrix.md from the full converter registry
> and the implemented report-only CI publication. Inspect the first GitHub-hosted
> run and triage converter findings while retaining explicit prerequisites and
> zero exit status for converter findings. Use command-line verification without
> a browser.
