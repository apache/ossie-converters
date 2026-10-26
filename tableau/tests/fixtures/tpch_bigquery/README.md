# tpch_bigquery

The six-table TPC-H model from `../tpch_snowflake/`, with Tableau connecting to BigQuery (`class='bigquery'`).

- `tpch_semantic_model.yaml` and `.tds`: 6 tables, 5 relationships, 0 losses. They use the placeholder project `my-gcp-project` and dataset `my_dataset`.
- Expected tables: `{project}.{dataset}.ossie_it_tpch_{lineitem,orders,customer,nation,part,supplier}`. BigQuery has no TPC-H, so they are copies of `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`.

## Data

Offline tests need no BigQuery project and no copy of the tables. Loading TPC-H into a live project is outside this package. The SF1 row counts are:

| Table    | Rows      |
| -------- | --------- |
| LINEITEM | 6,001,215 |
| ORDERS   | 1,500,000 |
| PART     | 200,000   |
| CUSTOMER | 150,000   |
| SUPPLIER | 10,000    |
| NATION   | 25        |

## Tests

From `tableau/`:

```sh
uv run pytest tests/test_tpch_bigquery.py
```

## Regenerate the TDS

```
ossie-tableau to-tds tpch_semantic_model.yaml -o tpch_semantic_model.tds --warehouse-type bigquery \
  --server bigquery.googleapis.com --database my-gcp-project \
  --schema my_dataset --project my-gcp-project
```
