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

# Apache Ossie converter for Databricks metric views

Databricks metric views map directly to Ossie datasets, preserving the distinction between
reusable metric definitions and multi-dataset semantic models. Ossie models can be deployed onto
Unity Catalog metric views, so that their measures can be queried through Databricks SQL.

Databricks reads Ossie models as they are, so the conversion verbs are light: converting to
Databricks changes nothing, and converting from Databricks only fills in portable expressions.
The deployment verbs connect an Ossie model to Unity Catalog metric views.

| Metric view | Ossie |
|---|---|
| Metric view | Dataset |
| `source` (table, view, or query) | `source` |
| Dimension | Field |
| Measure | Dataset-scoped metric |
| `MEASURE(m)` | `AGG(m)` |
| `comment`, `display_name`, `synonyms` | `description`, `label`, `ai_context.synonyms` |

## Verbs

| Verb | Command | Result |
|---|---|---|
| `convert_ossie_model_to_databricks(model)` | `to-databricks in.yaml -o out.yaml` | The model, unchanged |
| `convert_ossie_model_from_databricks(model)` | `from-databricks in.yaml -o out.yaml` | The model, with the portable `sql` filled wherever possible |
| `deploy_ossie_model_to_databricks(model, catalog=..., schema=...)` | `deploy in.yaml -o out/ --catalog main --schema sales` | A metric view file for each dataset, and the model rewritten to read the metric views |
| `convert_metric_view_to_ossie_model(definition, name)` | `import-view view.yaml -o model.yaml --name orders` | An Ossie model over the metric view’s original tables and joins |
| `wrap_metric_view_in_ossie_model(name, view)` | `wrap view.yaml --name main.sales.orders -o out.yaml` | A one-dataset model whose source is the metric view |

Each verb takes YAML text (or a parsed mapping) and returns a `Result` with the output and a list
of warnings. The conversion verbs and `wrap` return an Ossie model as YAML text. `deploy` returns
its files as `{relative path: YAML text}`, as the other multi-file converters of this repository
do; the CLI writes them to the output folder and replaces existing files only with
`--overwrite`. The CLI writes each warning to stderr as `[CODE] at <path>: <message>`.

- `from-databricks` fills the portable `sql` slot of each field and metric where it can:
  - An existing `sql` is never changed, because it takes precedence over an `OSSIE_SQL_2026`
    dialect entry. An entry equal to `sql` is removed; a different entry stays.
  - Without `sql`, an `OSSIE_SQL_2026` entry moves into `sql`.
  - Otherwise `sql` is translated from the `DATABRICKS` entry, or else from the `ANSI_SQL` entry.

  The `DATABRICKS`, `ANSI_SQL`, and other dialect entries stay, and nothing else changes. When no
  entry has a verified portable form, the expression stays as it is, with the warning
  `PORTABLE_EXPRESSION_MISSING`.
- `deploy` returns `metric_views/<dataset>.yaml` and `<model>.ossie.yaml`. Each dataset becomes the
  metric view `<catalog>.<schema>.<dataset>`, with its `source` unchanged. The first line of each
  metric view file is a comment that names this target. In the rewritten model
  each field is the dimension of the same name, and each dataset-scoped metric evaluates
  `MEASURE(source.<metric>)`. A metric keeps its portable expression when that expression reads
  only fields. Keys, relationships, model-scoped metrics, and metadata are unchanged.
- `wrap` keeps every measure through `MEASURE(source.<measure>)`. It also writes a portable
  expression for a measure when each input of the measure is a dimension of the metric view, the
  measure has no window, and no input comes from a `one_to_many` join. Parameters must have
  defaults; portable expressions use the defaults.

## Expressions

Expressions are translated in two places: `from-databricks` and `wrap` translate Databricks or
ANSI text to OSSIE_SQL_2026, and `deploy` translates a portable expression to Databricks SQL when the object
has no `DATABRICKS` entry. The package parses and prints expressions with sqlglot, with the dialect mapping of the Ossie
validator: `OSSIE_SQL_2026` and `ANSI_SQL` parse with the sqlglot default dialect. sqlglot
rewrites some functions into forms with another meaning, so the package translates an
expression only when each function is in a verified list (`expressions._PORTABLE`). Each entry
has a row in `tests/test_expressions.py`.

When an expression has no verified translation, the package keeps the original text and emits a
warning. It never writes a guessed translation.

A few constructs need a rewrite because Databricks spells or evaluates them differently:

| Ossie | Databricks |
|---|---|
| `AGG(m)` | `MEASURE(m)` |
| `TRUNC(x, d)` (numbers) | `CASE WHEN x < 0 THEN CEIL(x, d) ELSE FLOOR(x, d) END` |
| `EXTRACT(DAYOFYEAR FROM x)` | `EXTRACT(DOY FROM x)` |
| `REGEXP_EXTRACT(s, p)` (whole match) | `REGEXP_EXTRACT(s, p, 0)` |
| `CURRENT_DATE` | `CURRENT_DATE()`, so that no column of that name captures it |
| `TO_CHAR(x, 'YYYY-MM')`, `TO_DATE(s, 'DD/MM/YYYY')` | `DATE_FORMAT(x, 'yyyy-MM')`, `TO_DATE(s, 'dd/MM/yyyy')` |

The datetime format translation is EXPERIMENTAL, as the format functions are in the Ossie
specification. It works from Ossie to Databricks only.

## Validation

The package does not implement Ossie checks. It loads `validation/validate.py` from the `ossie`
submodule and runs it on each Ossie model that it reads or writes. An invalid input raises
`ConversionError`. To use another apache/ossie checkout, set `OSSIE_REPO` to its root directory.
The package does not validate metric views beyond their version: Databricks validates a metric
view when it is created.

## Compliance suites

- `tests/test_compliance.py` runs each fixture of the portability ladder
  (`compliance/portability/m0`-`m3`) through `to-databricks` and `from-databricks` and scores
  the result with the ladder's grader: every element is PORTABLE. It also rewrites each fixture
  with `DATABRICKS` entries only, and checks that `from-databricks` restores a portable form for
  every element.
- The CLI takes `<input> -o <output>`, as the interop harness calls each converter. The native
  format of this converter is an Ossie model file:

  ```yaml
  databricks:
    cwd: databricks
    toolchain: uv
    prepare: [uv, sync, --locked, --no-dev]
    import: [uv, run, --no-sync, ossie-databricks, from-databricks, '{input}', -o, '{output}']
    export: [uv, run, --no-sync, ossie-databricks, to-databricks, '{input}', -o, '{output}']
    output: file
  ```

Use `deploy --joins` (Python `joins=True`) to include many-to-one joins and eligible model
metrics in the generated views. Deployment still emits one view per dataset. A relationship
needs a declared primary or unique key on the target. Nested joins follow the relationship
direction. A model metric is placed on its own dataset when it reads just one, or on a single
eligible root when it reads across datasets. The rewritten model evaluates the exported measure
with Databricks SQL. See the [Databricks metric view YAML reference](https://docs.databricks.com/aws/en/uc-semantics/metric-views/yaml-reference).

`import-view` reconstructs datasets and expressions over the source tables or queries. It
imports column equality and `using` joins as relationships and places single-source measures
on their datasets. It preserves features such as filters, windows, parameters and unsupported
join conditions in a Databricks extension, with warnings when they have no portable expression.
Deploying the imported model with `--joins` restores those features to the root view.

## Limitations

- The package does not connect to a workspace. It writes metric view definitions but does not
  create them.
- `deploy` warns for each omitted relationship or model-scoped metric. Relationships remain in
  the rewritten model. A model-scoped metric is removed when its SQL references a column the metric
  views do not expose or depends on another removed metric.
- A field or metric whose expressions are all in a non-SQL dialect has no dimension or measure.
  `deploy` reports it and keeps it unchanged in the model.
- The package reads and writes metric view YAML version `1.1` only.

## Development

```sh
git submodule update --init
cd databricks
uv run pytest
uv run ruff check && uv run ruff format --check
```

`UPDATE_GOLDEN=1 uv run pytest` rewrites `tests/fixtures/expected/`. Review each difference.
