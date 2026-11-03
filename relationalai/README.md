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

# Ossie RelationalAI Converter

Converts an Ossie ontology into a [RelationalAI](https://relational.ai/) PyRel
model.

| Converter                      | Direction                          |
|--------------------------------|------------------------------------|
| `OssieToRelationalAIConverter` | Ossie model → RelationalAI (PyRel) |

It builds on [`apache-ossie-ontology`](../ontology/README.md), which parses the
Ossie ontology, its formulas and its dataset expressions. This package holds
only the translation to PyRel and the PyRel-side model it targets.

## The `relationalai` extra

> **`relationalai` is proprietary and declares no license.** Its PyPI
> distribution carries no `License` field, no license classifier and no
> `LICENSE` file, and the [project page](https://pypi.org/project/relationalai/)
> lists none either — so it grants no use or redistribution rights by default.
> Get the applicable terms from RelationalAI (`support@relational.ai`) before
> installing it. Ossie is Apache-2.0 and neither bundles nor depends on it.
>
> Installing the package is also not enough to *run* what this converter emits.
> That needs a Snowflake account with the RelationalAI Native App installed from
> the [Marketplace](https://app.snowflake.com/marketplace/listing/GZTYZOOIX8H/relationalai-relationalai),
> and access enabled by RelationalAI on request — see the
> [setup guide](https://docs.relational.ai/manage/get-started/install/). Ossie
> never executes the generated source: it converts and compiles in-process,
> offline, against no engine.

Because that SDK is non-free, it is not a dependency of this package. It is
gated behind an opt-in extra, so nobody acquires it without asking for it:

```bash
pip install "apache-ossie-relationalai[relationalai]"    # opt in, having read the above
```

Without the extra, importing `ossie_relationalai` fails and the test suite
skips the converter's tests.

## Usage

The converter turns an `OssieOntology` into an in-memory `OntologyModel`, which
can then be serialized to PyRel source:

```python
from pathlib import Path

from relationalai.semantics.metamodel.pyrel_codegen import to_pyrel

from ossie_ontology.parser import OssieParser
from ossie_relationalai import OssieToRelationalAIConverter

ontology = OssieParser().parse(Path("model.yaml"))

model = OssieToRelationalAIConverter.convert(ontology)
Path("model_pyrel.py").write_text(to_pyrel(model.base_model().to_metamodel()))
```

`OssieParser` parses and validates `derived_by` and `requires` expressions into
an AST by default, which is what the conversion needs: an unparsed formula is
skipped and never reaches PyRel.

### Dataset fields

A dataset field is read through its SQL `expression`, not its `name`: the name
is what mappings call the field, the expression is what it reads. A field
`line_status` with the expression `L_LINESTATUS` reads the `L_LINESTATUS`
column, and one with `"L_LINESTATUS" IS NULL` reads no column of its own but a
condition over one. The `SNOWFLAKE` dialect is used when a field has it, then
`ANSI_SQL`. `ossie_ontology.expr.sql` parses the scalar subset a row-level
field needs — columns (quoted or not), literals, `NULL`, arithmetic, `||`,
comparisons, `IS [NOT] NULL`, `AND`/`OR`/`NOT` — and the conversion renders it
as PyRel over the table. A condition becomes a Boolean asserted `TRUE` on the
rows where SQL says it is `TRUE`, and on no others: under PyRel's closed-world
reading, a row without the fact is one where the condition does not hold, so
ask `not_(Item.flag)` rather than `Item.flag == False`. Anything else (`CASE`,
`CAST`, functions, a column qualified by a table) fails the conversion with a
message naming the field.

### Converting without a warehouse

Two things about the environment this needs. Constructing a model makes
`relationalai` resolve its configuration, so a `raiconfig.yaml` must be present.
And each dataset's `source` is read from the configured connection to get its
column types, so the call above needs one that can reach those tables.

To convert without a warehouse, pass a different `table_provider`. The default,
`warehouse_table`, resolves each dataset's `source` against the connection —
which is what gets the column identifiers right, since Snowflake folds an
unquoted name to upper case — and a column a field reads that the table lacks
fails the conversion. The alternative, `declared_table`, declares the columns
the fields' expressions read, from the Ossie spec, and reads nothing:

```python
from ossie_relationalai import declared_table

model = OssieToRelationalAIConverter.convert(ontology, table_provider=declared_table)
```

Nothing leaves the process, so the tables the generated source names need not
exist. The tradeoff is the one `warehouse_table` avoids: the declared
identifiers have to match how the table was actually created.

That is how the test suite runs — see `tests/conftest.py` for the offline config
it pins and the network guard it installs, and `tests/test_converter.py` for the
rest of the offline setup.

`table_provider` is also the extension point for reading rows from somewhere
else entirely — from inline CSV, for example, so a test corpus needs no
warehouse. This package ships no such provider and has no CSV handling at all,
because reading rows is not part of converting an ontology.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package and dependency manager

Install uv if you don't have it:

```bash
brew install uv
```

## Setup

```bash
cd relationalai
uv sync --extra relationalai
```

`apache-ossie-ontology` is installed from `../ontology` in editable mode, so a
change there is picked up without reinstalling.

## Generating / updating the lock file

`uv.lock` is produced by uv from `pyproject.toml`. Run this whenever you add or
change a dependency:

```bash
uv lock
uv sync --extra relationalai
```

## Running the tests

```bash
uv run pytest
```

The tests run offline against `tests/config/raiconfig.yaml`. The full-ontology
specs they convert are shared with the ontology suite and read from
`../ontology/tests/fixtures/specs/`.

Regenerate pytest snapshots after an intentional output change:

```bash
uv run pytest --snapshot-update
```
