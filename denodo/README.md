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

# Apache Ossie ↔ Denodo Metric View Converter

Bidirectional converter between Apache Ossie semantic model YAML and Denodo Metric View VQL. Pure offline conversion — no Denodo connection required.

## Environment requirements

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (version `0.9.0` or later). `uv` can be installed without having Python installed.
  - After install uv, it could be required to modify the PATH environment variable to include the uv executable.
  - This converter requires Python 3.11 or higher, but you do not need to install Python separately.
  - Running `uv sync` downloads a compatible Python version if one is not already available, then creates the project environment and installs the dependencies.
  - An internet connection is needed the first time if Python or the dependencies must be downloaded.
- The input VQLs have to come from 9.5.x and 9.6.x Denodo versions
- For the conversor of Denodo VQL → OSI YAML, the vql has to contains the association vqls and the metric view vql (we strongly suggest to use full export with dependencies). The conversor will ignore any other vql commands and it will only take into account the associations + metric vql commands.
  ```
  DESC VQL VIEW <metric_view>
  ```

## Setup

From denodo folder, the first command has to be this one in order to install the required packages:

```bash
uv sync
```

After that all the below commands can be executed in the same denodo path.

## Converters

### OSI YAML → Denodo VQL

Converts an Apache Ossie semantic model (YAML) into Denodo VQL statements, generating `CREATE OR REPLACE ASSOCIATION` and `CREATE OR REPLACE METRIC VIEW` statements.

```bash
uv run ossie-denodo osi-to-vql -i model.yaml -o output.vql
```

| Flag | Description |
|------|-------------|
| `-i`, `--input` | Input Ossie YAML file |
| `-o`, `--output` | Output Denodo VQL file |

### Denodo VQL → OSI YAML

Converts Denodo VQL (containing association and metric view statements) into an Apache Ossie semantic model YAML.

```bash
uv run ossie-denodo vql-to-osi -i input.vql -o model.yaml
```

| Flag | Description |
|------|-------------|
| `-i`, `--input` | Input Denodo VQL file |
| `-o`, `--output` | Output Ossie YAML file |

## Tests

Run the full test suite:

```bash
uv run pytest
```

Run a specific test file:

```bash
uv run pytest tests/test_osi_to_vql.py
uv run pytest tests/test_vql_to_osi.py
```

Run with verbose output:

```bash
uv run pytest -v
```

## Limitations

- Denodo metric views do not support `ai_context` (a warning will be issued and it will not be included in the generated VQL).
- OSI level limitations. These points are handled within specific `custom_extension` properties.

### The YAML does not group dataset fields into dimensions.

Example:
```yaml
datasets:
  - name: customer
    source: vdb_tpch.psql_tpch_customer
    fields:
      - name: customer_address_full
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: concat(customer.c_address, '-', nation.n_name)
      - name: customer_name
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: customer.c_name
      - name: customer_address
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: customer.c_address
      - name: customer_comment
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: customer.c_comment
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"fields_dimension":{"customer_address_full": "dim_customer", "customer_name":"dim_customer"}}'
```
The `fields_dimension` entry maps fields to their corresponding dimension groups.

### The YAML does not capture the different types of cardinality supported by Denodo.

Example:
```yaml
relationships:
  - name: psql_tpch_region_nation
    from: psql_tpch_nation
    to: psql_tpch_region
    from_columns:
      - r_regionkey
    to_columns:
      - n_regionkey
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"from_columns": {"relation": "+"}}'
```
The `data` field contains a `relation` property within `from_columns` or `to_columns`. Supported values for `relation` are: `+`, `*`, `0,1`, `0`, `1`.

Default values:
- `from_columns`: defaults to `0` if not specified.
- `to_columns`: defaults to `*` if not specified.

### The YAML does not capture the different types of joins associated with associations.

Example:
```yaml
relationships:
  - name: vdb_tpch.psql_tpch_nation_psql_tpch_customer
    from: customer
    to: nation
    from_columns:
      - n_nationkey
    to_columns:
      - c_nationkey
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"join_type": "RIGHT"}'
```
Supported `join_type` values are: `RIGHT`, `LEFT`, `INNER`. If not specified, the association's default join type is used.

### The YAML does not capture the referential constraint of Denodo associations.

Example:
```yaml
relationships:
  - name: psql_tpch_nation_customer
    from: psql_tpch_nation
    to: psql_tpch_customer
    from_columns:
      - c_nationkey
    to_columns:
      - n_nationkey
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"isReferentialConstraint": false, "from_columns": {"relation": "1"}}'
```
`isReferentialConstraint` indicates if the association VQL should use a referential constraint. It defaults to `true`.

### The YAML does not support relationship conditions containing functions.

Example:
```yaml
relationships:
  - name: dw_dim_customers_dw_fact_sales
    from: sales
    to: customers
    from_columns:
      - customer_key
    to_columns:
      - customer_key
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"from_columns": {"relation": "1", "fields_replace": [{"customer_key": "round(customer_key)"}]}}'
```
The `fields_replace` entry within `from_columns` or `to_columns` is used to apply specific logic or functions to the columns.

### The Denodo field properties `sourcetyperadix`, `sourcetypesize` and `sourcetypeid` have no direct OSI equivalent.

They are stored in a `nonportable_extensions` entry of the field (or metric) and read back from it when generating VQL.

Example:
```yaml
fields:
  - name: year
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: getyear(dates.full_date)
    nonportable_extensions:
      - vendor_name: DENODO
        data: '{"sourcetyperadix": "10", "sourcetypesize": "10", "sourcetypeid": "4"}'
```
