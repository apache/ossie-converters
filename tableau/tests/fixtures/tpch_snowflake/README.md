# tpch_snowflake

The six-table TPC-H model, with Tableau connecting to Snowflake (`class='snowflake'`). Source of truth for `../tpch_bigquery/`. The checked-in workbooks under `../features/tpch_multi/` and `../features/tpch_multi_twbx/` are copies of this model; refresh their expected YAML with `tools/update_snapshots.py`.

- `tpch_semantic_model.yaml` and `.tds`: 6 tables, 5 relationships, 0 losses. They use the placeholder host `account.snowflakecomputing.com`, empty username/warehouse, `dbname=SNOWFLAKE_SAMPLE_DATA`, `schema=TPCH_SF1`.
- Expected tables: `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.{LINEITEM,ORDERS,CUSTOMER,NATION,PART,SUPPLIER}`. Snowflake already ships TPC-H SF1; no copy is required.

## Tests

From `tableau/`:

```sh
uv run pytest tests/test_tpch_roundtrip.py
```

## Regenerate the TDS

```
ossie-tableau to-tds tpch_semantic_model.yaml -o tpch_semantic_model.tds \
  --server account.snowflakecomputing.com \
  --database SNOWFLAKE_SAMPLE_DATA --schema TPCH_SF1
```

Snowflake is the default `--warehouse-type`.
