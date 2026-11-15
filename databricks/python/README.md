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

# Apache Ossie Databricks Converter (Python)

A Python port of the [Java Databricks converter](../java/README.md). It converts in both
directions, offline, between an [Apache Ossie](https://github.com/apache/ossie) semantic model and
a Databricks [Unity Catalog Metric View](https://docs.databricks.com/aws/en/metric-views/)
(YAML `1.1`).

The Python port aims to behave exactly like the Java implementation. That covers the mapping,
the notices, the error messages and the exit codes. Both run the same test suites against the
same YAML fixtures. For the full mapping table, the dialect-labeling rules and the conversion
requirements, see the [Java README](../java/README.md).

- **Export** (`ossie_to_metric_view`): Apache Ossie -> Metric View.
- **Import** (`metric_view_to_ossie`): Metric View -> Apache Ossie. Metric-View-only features are
  kept in `custom_extensions[DATABRICKS]`, so `MV -> Apache Ossie -> MV` loses nothing.

## Requirements

- **Python 3.11+**
- [uv](https://docs.astral.sh/uv/) (recommended) for development

## Installation

```bash
uv sync
```

## Usage

### Command line

```bash
# export: Apache Ossie -> Metric View
uv run ossie-databricks export model.yaml -o view.yaml

# import: Metric View -> Apache Ossie
uv run ossie-databricks import view.yaml -o model.yaml
```

`python -m ossie_databricks` also works. Without `-o`, output goes to stdout.

- `--source` (export only) picks the fact table, which sets the grain.
- `--name` (import only) sets the Apache Ossie model name.

Conversion notices go to stderr. An input that cannot be converted exits with a non-zero code.

### With ossie-cli

[ossie-cli](https://github.com/MikeCarlo/ossie-cli) can load and run this converter in both directions:

```bash
ossie-cli load databricks            # or: --local <path to databricks/python>
ossie-cli to-ossie databricks -i metric_views/ -o ossie/
ossie-cli from-ossie databricks -i model.ossie.yaml -o metric_view.yaml -- --source orders
```

### Python API

```python
from ossie_databricks import (
    ConversionError,
    convert_metric_view_to_ossie,
    convert_ossie_to_metric_view,
)

# export: Apache Ossie -> Metric View (optionally choose the fact/grain)
view = convert_ossie_to_metric_view(ossie_yaml, "orders")

# import: Metric View -> Apache Ossie (optionally name the model)
ossie = convert_metric_view_to_ossie(metric_view_yaml, "sales")

print(view.yaml)
print(view.notices)
```

Each `Result` has the output YAML (`result.yaml`) and a list of notices (`result.notices`). If an
input breaks a requirement, the converter raises `ConversionError`. Its `kind` attribute
distinguishes invalid input, an unsupported version, a feature that cannot be represented, and an
internal error.

## Development

```bash
uv run pytest
```

The source layout mirrors the Java classes one module per class:

```
src/ossie_databricks/
  ossie_converter.py         public facade: entry points + ConversionError/Notices/Result
  ossie_converter_common.py  shared constants, YAML I/O, map accessors, the stash codec
  ossie_to_metric_view.py    export: Apache Ossie -> Metric View v1.1
  metric_view_to_ossie.py    import: Metric View v1.1 -> Apache Ossie
  expression_dialect.py      import: the most portable dialect label for an expression
  joined_integer_sum.py      Cube joined-integer-sum grain encoding
  cli.py                     command-line entry point (export / import)
tests/
  test_ossie_converter.py             port of OssieConverterSuite
  test_expression_dialect.py          port of ExpressionDialectSuite
  test_ossie_converter_round_trip.py  port of OssieConverterRoundTripSuite
  test_cli.py                         port of OssieDatabricksConverterCliSuite
  resources/                          YAML fixtures (copies of the Java fixtures)
```

When you change the converter, update the Java and Python implementations together.
