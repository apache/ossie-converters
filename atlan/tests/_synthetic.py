# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""A synthetic metric registry on TPC-DS tables: one record per golden case.

Each record has the shape one registry producer writes (whole SQL queries from a query log,
structured fields from a table-level producer and a metric builder, a non-SQL dialect from an
operations producer) on invented data. Ids, workspaces and names are made up; tables are TPC-DS
plus two invented snapshot tables. `_golden.py` turns these into tests/fixtures/<case>/.
"""

from __future__ import annotations

import copy
import json

SPEC = "1.0-draft.4"
WORKSPACE = "workspace_synthetic_tpcds"

T = {  # the tables the records read
    "sales": "TPCDS.PUBLIC.STORE_SALES",
    "customer": "TPCDS.PUBLIC.CUSTOMER",
    "address": "TPCDS.PUBLIC.CUSTOMER_ADDRESS",
    "item": "TPCDS.PUBLIC.ITEM",
    "returns": "TPCDS.PUBLIC.STORE_RETURNS",
    "store": "TPCDS.PUBLIC.STORE",
    "monthly": "TPCDS.PUBLIC.STORE_MONTHLY_SNAPSHOT",     # invented: one row per store per month
    "survey": "TPCDS.PUBLIC.CUSTOMER_SURVEY",             # invented: one row per survey answer
}


def _record(name: str, definition: dict, *, description: str, display_name: str | None = None,
            source_type: str = "external", source_id: str | None = None) -> dict:
    d = {"spec_version": SPEC, "version": 1, "dialect": "snowflake",
         "aggregation": {"type": "custom"}, "output": {"alias": name, "datatype": "integer"},
         "joins": [], "dimensions": [], "filters": [], "mandatory_filters": [], "examples": [],
         "exclusions": [], "grain": "", "population": "", "provenance": {"source": "synthetic"},
         "extensions": {}}
    d.update(definition)
    d.setdefault("source", {})
    src = {"sql": None, "table": None, "alias": "m", "entity": d["source"].get("table") or "", "parameters": []}
    src.update(d["source"])
    if src["sql"] and not src.get("entity"):
        src["entity"] = src["sql"].split("FROM ", 1)[-1].split()[0]
    d["source"] = src
    ident = name.lower().replace(" ", "_").replace("(", "").replace(")", "")
    return {"id": f"metric_synth_{ident}", "kind": "metric", "app": "metric", "name": name,
            "display_name": display_name or name.replace("_", " ").title(), "description": description,
            "artifact_status": "active", "workspace_id": WORKSPACE, "version_ordinal": 1,
            "source_type": source_type, "source_id": source_id or f"synthetic:{ident}",
            "metadata": {"definition": d, "validation_warnings": []}}


def _rowset(table: str, where: str | None = None) -> dict:
    return {"sql": f"SELECT * FROM {table}" + (f" WHERE {where}" if where else ""), "entity": table}


def _dim(name, sql, kind="categorical", datatype="string", **extra) -> dict:
    return {"name": name, "kind": kind, "sql": sql, "datatype": datatype, **extra}


def records() -> dict[str, dict]:
    """golden case name -> synthetic registry record."""
    R = {}

    # --- the query-log producer: raw SQL, no structured fields -------------------------------
    R["count_rows"] = _record("store_sales_count", {
        "formula": "COUNT(*)", "aggregation": {"type": "count"}, "source": _rowset(T["sales"]),
        "grain": "table-level", "population": f"All rows in {T['sales']}"},
        description="Count all rows in the store sales fact table")
    R["count_filtered"] = _record("preferred_customer_count", {
        "formula": "COUNT(*)", "aggregation": {"type": "count"},
        "source": _rowset(T["customer"], "C_PREFERRED_CUST_FLAG = 'Y'"),
        "grain": "customer", "population": "Customers flagged as preferred"},
        description="Count of customers whose preferred flag is set")
    R["count_distinct_filtered"] = _record("book_manufacturer_count", {
        "formula": "COUNT(DISTINCT I_MANUFACT_ID)", "aggregation": {"type": "count_distinct"},
        "source": _rowset(T["item"], "I_CATEGORY = 'Books' AND NOT I_BRAND LIKE '%importo%'"),
        "grain": "one row per distinct manufacturer", "population": "Items in the Books category",
        "exclusions": ["brands containing 'importo'"]},
        description="Distinct manufacturers of items in the Books category, excluding one brand family")
    R["max_timestamp"] = _record("last_store_sale_at", {
        "formula": "MAX(SS_SOLD_TIMESTAMP)", "aggregation": {"type": "max"},
        "output": {"alias": "last_store_sale_at", "datatype": "timestamp"},
        "source": _rowset(T["sales"]), "grain": "table-level", "population": "All store sales"},
        description="Timestamp of the most recent store sale")
    R["ratio_round_window"] = _record("defective_return_pct", {
        "formula": ("SELECT ROUND(COUNT(CASE WHEN SR_RETURN_REASON='defective' THEN 1 END)*100.0"
                    "/ NULLIF(COUNT(CASE WHEN SR_RETURN_REASON IN ('defective','changed_mind') THEN 1 END),0),1) AS defective_pct, "
                    "COUNT(CASE WHEN SR_RETURN_REASON IN ('defective','changed_mind') THEN 1 END) AS classified_returns "
                    f"FROM {T['returns']} WHERE SR_RETURNED_DATE >= '2026-04-01' AND SR_RETURNED_DATE < '2026-07-01'"),
        "source": {"table": T["returns"]},
        "grain": "aggregate over entire filtered date range",
        "population": "Store returns with a reason of 'defective' or 'changed_mind'",
        "exclusions": ["returns with other reasons are excluded from the denominator"],
        "examples": [{"sql": ("SELECT ROUND(COUNT(CASE WHEN SR_RETURN_REASON='defective' THEN 1 END)*100.0"
                              "/ NULLIF(COUNT(CASE WHEN SR_RETURN_REASON IN ('defective','changed_mind') THEN 1 END),0),1) AS defective_pct "
                              f"FROM {T['returns']} WHERE SR_RETURNED_DATE >= '2026-04-01' AND SR_RETURNED_DATE < '2026-07-01'"),
                      "source": "query_log"}],
        "provenance": {"source": "query_log", "trust_score": 7.5}},
        description="Share of defective returns among classified returns, and the count of classified returns")

    # --- refusals from the query-log producer ------------------------------------------------
    R["refused_join_from_sql_split"] = _record("sales_by_customer_state", {
        "formula": ("SELECT c.C_FIRST_NAME, COALESCE(NULLIF(a.CA_STATE,''),'(unknown)') AS STATE, COUNT(*) AS SALES, "
                    "COUNT(DISTINCT m.SS_ITEM_SK) AS ITEMS, MAX(m.SS_SOLD_TIMESTAMP)::DATE AS LAST_SALE, "
                    "DATEDIFF('day', MAX(m.SS_SOLD_TIMESTAMP), CURRENT_TIMESTAMP) AS DAYS_IDLE "
                    f"FROM {T['sales']} m LEFT JOIN {T['customer']} c ON m.SS_CUSTOMER_SK = c.C_CUSTOMER_SK "
                    f"LEFT JOIN {T['address']} a ON c.C_CURRENT_ADDR_SK = a.CA_ADDRESS_SK "
                    "WHERE a.CA_COUNTRY = 'United States' AND m.SS_QUANTITY > 0 "
                    "AND m.SS_SOLD_TIMESTAMP >= '2026-03-18' AND m.SS_SOLD_TIMESTAMP < DATEADD(day,1,'2026-06-16') "
                    "GROUP BY c.C_FIRST_NAME, COALESCE(NULLIF(a.CA_STATE,''),'(unknown)') ORDER BY SALES DESC"),
        "source": {"table": T["sales"]}, "grain": "customer, state", "population": "US store sales with a positive quantity"},
        description="Sales, distinct items, last sale and days idle per customer and state")
    R["refused_join_from_sql_sum"] = _record("open_order_count_and_value", {
        "formula": (f"SELECT COUNT(*), COALESCE(SUM(s.SS_NET_PAID), 0) FROM {T['sales']} s "
                    f"LEFT JOIN {T['returns']} r ON s.SS_TICKET_NUMBER = r.SR_TICKET_NUMBER WHERE r.SR_TICKET_NUMBER IS NULL"),
        "source": {"table": T["sales"]}, "grain": "single aggregate row", "population": "Store sales never returned"},
        description="Count and value of sales that were never returned")
    R["refused_window_cte"] = _record("weekly_returns_trailing_avg", {
        "formula": ("WITH weekly AS (SELECT DATE_TRUNC('week', SR_RETURNED_DATE) AS week_start, COUNT(*) AS returns "
                    f"FROM {T['returns']} WHERE SR_RETURNED_DATE >= DATEADD('week', -16, CURRENT_DATE) GROUP BY 1), "
                    "with_avg AS (SELECT week_start, returns, ROUND(AVG(returns) OVER (ORDER BY week_start ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING), 1) AS avg_4w "
                    "FROM weekly) SELECT week_start AS \"Week\", returns AS \"Returns\", avg_4w AS \"4W Trailing Avg\" FROM with_avg ORDER BY week_start"),
        "source": {"table": T["returns"]}, "grain": "week", "population": "Store returns in the last 16 weeks"},
        description="Weekly returns with a four-week trailing average")
    R["refused_row_listing"] = _record("customer_id_by_email", {
        "formula": f"SELECT C_CUSTOMER_ID, C_EMAIL_ADDRESS FROM {T['customer']} WHERE C_EMAIL_ADDRESS = 'someone@example.com'",
        "source": {"table": T["customer"]}, "grain": "customer", "population": "One customer looked up by email"},
        description="Look up a customer id by email address")
    R["refused_list_aggregate"] = _record("item_names_by_category", {
        "formula": (f"SELECT I_CATEGORY, ARRAY_AGG(I_PRODUCT_NAME) AS names, COUNT(*) AS n FROM {T['item']} "
                    "WHERE I_REC_END_DATE IS NULL GROUP BY I_CATEGORY"),
        "source": {"table": T["item"]}, "grain": "category", "population": "Current items"},
        description="Item names and counts per category")
    R["refused_bind_placeholder"] = _record("weekly_avg_basket_for_store", {
        "formula": (f"SELECT DATE_TRUNC('week', SS_SOLD_DATE) AS WEEK, ROUND(AVG(SS_NET_PAID), 2) AS VALUE FROM {T['sales']} "
                    "WHERE SS_STORE_SK = ? AND SS_NET_PAID IS NOT NULL AND SS_SOLD_DATE >= DATEADD(month, -12, CURRENT_TIMESTAMP()) "
                    "GROUP BY 1 ORDER BY 1"),
        "source": {"table": T["sales"]}, "grain": "week", "population": "Store sales of one store, last 12 months"},
        description="Weekly average basket value for one store")

    # --- an operations producer: not SQL --------------------------------------------------------
    R["refused_non_sql_dialect"] = _record("checkout_latency_p95", {
        "formula": 'histogram_quantile(0.95, sum(rate(checkout_latency_seconds_bucket{cluster="prod"}[5m])) by (le))',
        "dialect": "promql", "source": {"table": "prod/checkout", "entity": "signals.metrics"},
        "grain": "5-minute windows", "population": "Checkout requests in production"},
        description="95th percentile checkout latency", source_type="git", source_id="operations/checkout_latency_p95")

    # --- a producer that fills the structured fields and writes bare column names ----------------------
    dims6 = [_dim("store_name", "store_name", description="Store name."), _dim("division", "division", description="Sales division."),
             _dim("state", "state", description="Store state."), _dim("segment", "segment", description="Store size segment."),
             _dim("lifecycle_stage", "lifecycle_stage", description="Opened, mature or closing."),
             _dim("milestone", "milestone", description="Latest milestone reached.")]
    R["bare_columns_ratio_dims"] = _record("Active shoppers change vs 60-90 days ago", {
        "formula": "100.0 * (SUM(active_shoppers) - SUM(shoppers_90d)) / NULLIF(SUM(shoppers_90d), 0)",
        "aggregation": {"type": "ratio", "reaggregation": "none", "null_handling": "ignore", "zero_denominator": "null"},
        "output": {"alias": "active_shoppers_change_pct", "datatype": "decimal", "unit": "percent"},
        "source": {"table": T["monthly"], "alias": "store_monthly_snapshot"},
        "dimensions": dims6, "grain": "store", "population": "Stores with a monthly snapshot"},
        description="Percentage change in active shoppers against the value 60 to 90 days ago", source_id="warehouse-tables/active_shoppers_change")
    R["mandatory_filter_time_dim"] = _record("Open returns", {
        "formula": "COUNT(DISTINCT sr_ticket_number)", "aggregation": {"type": "count_distinct", "reaggregation": "none"},
        "output": {"alias": "open_returns", "datatype": "integer", "unit": "returns"},
        "source": {"table": T["returns"], "alias": "store_returns", "timeframe_column": "sr_returned_date"},
        "mandatory_filters": ["sr_return_status <> 'void'"],
        "dimensions": [_dim("sr_returned_date", "sr_returned_date", "time", "date", description="Day the return was filed."),
                       _dim("store_name", "store_name", description="Store the return belongs to."),
                       _dim("sr_return_status", "sr_return_status", description="open, closed or void.")],
        "grain": "return ticket", "population": "Store returns that are not void"},
        description="Number of distinct return tickets, excluding voided returns", source_id="warehouse-tables/open_returns")
    R["declared_join_clause"] = _record("Sales to active customers", {
        "formula": "COUNT(*)", "aggregation": {"type": "count", "reaggregation": "sum"},
        "output": {"alias": "sales_to_active_customers", "datatype": "integer", "unit": "sales"},
        "source": {"table": T["sales"], "alias": "store_sales", "timeframe_column": "ss_sold_date"},
        "joins": [{"name": "active_customer", "to": T["customer"], "cardinality": "many_to_one", "origin": "declared",
                   "sql": f"JOIN {T['customer']} c ON c.c_customer_sk = store_sales.ss_customer_sk"}],
        "dimensions": [_dim("ss_sold_date", "ss_sold_date", "time", "date", description="Sale date."),
                       _dim("preferred", "c.c_preferred_cust_flag", description="Preferred customer flag."),
                       _dim("birth_country", "c.c_birth_country", description="Customer birth country.")],
        "grain": "sale", "population": "Store sales joined to their customer"},
        description="Store sales that can be attributed to a known customer", source_id="warehouse-tables/sales_to_active_customers")

    # --- a metric-builder producer: columns qualified by table name, FROM fragments, {{params}} ----
    S, I = T["sales"], T["item"]
    R["fragment_join_qualified"] = _record("Store Sale Errors", {
        "formula": f"SUM(CASE WHEN {S}.SS_IS_VOID = TRUE THEN 1 ELSE 0 END)",
        "aggregation": {"type": "sum", "reaggregation": "sum"},
        "output": {"alias": "Store Sale Errors", "datatype": "integer", "unit": "sales"},
        "source": {"alias": "Store Sales", "entity": S, "table": None,
                   "sql": f"{S}\nLEFT JOIN {I}\n       ON {I}.I_ITEM_SK = {S}.SS_ITEM_SK"},
        "joins": [{"name": "item tags", "to": I, "cardinality": "many_to_one",
                   "origin": "carries the Seasonal dimension; a LEFT JOIN, so it keeps every sale",
                   "sql": f"{I}.I_ITEM_SK = {S}.SS_ITEM_SK"}],
        "dimensions": [_dim("Sale Date", f"{S}.SS_SOLD_TIMESTAMP", "time", "date", required_join=None, description="When the sale happened."),
                       _dim("Store", f"{S}.SS_STORE_SK", required_join=None, description="The store the sale belongs to."),
                       _dim("Channel", f"INITCAP({S}.SS_CHANNEL)", required_join=None, description="Sales channel."),
                       _dim("Seasonal", f"CASE WHEN {I}.I_ITEM_TAGS LIKE '%Seasonal%' THEN 'Yes' ELSE 'No' END", required_join=None, description="Whether the item is tagged seasonal.")],
        "grain": f"{S}.SS_STORE_SK, {S}.SS_CUSTOMER_SK, {S}.SS_SOLD_TIMESTAMP",
        "population": "Store sales, one row per sale, attributed to the store and carrying the channel and item tags",
        "depends_on": ["Store Sales"],
        "examples": [{"question": "How are voided sales trending month by month per store, broken down by channel and whether the item is seasonal?",
                      "sql": f"SELECT DATE_TRUNC('MONTH', SS_SOLD_TIMESTAMP) AS month, SS_STORE_SK, SUM(CASE WHEN SS_IS_VOID = TRUE THEN 1 ELSE 0 END) FROM {S} GROUP BY 1, 2",
                      "source": "metric_builder"}]},
        description="Store sales that were voided: the reliability signal store managers act on", display_name="Store Sale Errors",
        source_id="metric:builder:0001")
    V = T["survey"]
    R["bound_placeholder_param"] = _record("CSAT", {
        "formula": f"ROUND(COUNT(CASE WHEN {V}.SCORE = 'good' THEN 1 END) * 100.0 / NULLIF(COUNT(CASE WHEN {V}.SCORE IN ('good', 'bad') THEN 1 END), 0), 1)",
        "aggregation": {"type": "ratio", "reaggregation": "none", "zero_denominator": "null"},
        "output": {"alias": "CSAT", "datatype": "decimal", "unit": "percent"},
        "source": {"alias": "Survey Answers", "table": V, "entity": V,
                   "parameters": [{"name": "window_days", "datatype": "integer", "default": 30}]},
        "filters": [f"{V}.ANSWERED_AT >= DATEADD(day, -{{{{window_days}}}}, CURRENT_TIMESTAMP)",
                    f"{V}.CHANNEL = 'store'"],
        "mandatory_filters": [f"{V}.SCORE IN ('good', 'bad')"],
        "dimensions": [_dim("Answer Date", f"{V}.ANSWERED_AT", "time", "date", required_join=None, description="When the survey was answered.")],
        "grain": f"{V}.SURVEY_ID", "population": "Survey answers with a good or bad score", "depends_on": ["CSAT Responses"],
        "examples": [{"question": "What is the CSAT for store surveys over the last 30 days?",
                      "sql": f"SELECT ROUND(COUNT(CASE WHEN SCORE = 'good' THEN 1 END) * 100.0 / NULLIF(COUNT(*), 0), 1) FROM {V} WHERE SCORE IN ('good','bad') AND CHANNEL = 'store' AND ANSWERED_AT >= DATEADD(day, -30, CURRENT_TIMESTAMP)",
                      "source": "metric_builder"}]},
        description="Share of good scores among all good or bad survey answers", display_name="CSAT", source_id="metric:builder:0002")
    R["account_param_as_dimension"] = _record("Resolved Returns", {
        "formula": f"COUNT_IF(({T['returns']}.SR_RESOLVED_AT IS NOT NULL OR LOWER({T['returns']}.SR_RETURN_STATUS) IN ('resolved', 'closed')))",
        "aggregation": {"type": "count", "reaggregation": "sum"},
        "output": {"alias": "Resolved Returns", "datatype": "integer", "unit": "returns"},
        "source": {"alias": "Store Returns", "table": T["returns"], "entity": T["returns"],
                   "parameters": [{"name": "store_sk", "datatype": "string", "default": None},
                                  {"name": "window_days", "datatype": "integer", "default": 30}]},
        "filters": [f"{T['returns']}.SR_STORE_SK = {{{{store_sk}}}}",
                    f"COALESCE({T['returns']}.SR_RESOLVED_AT, {T['returns']}.SR_RETURNED_DATE) >= DATEADD(day, -{{{{window_days}}}}, CURRENT_TIMESTAMP)"],
        "mandatory_filters": [f"({T['returns']}.SR_RESOLVED_AT IS NOT NULL OR LOWER({T['returns']}.SR_RETURN_STATUS) IN ('resolved', 'closed'))"],
        "dimensions": [_dim("Store", f"{T['returns']}.SR_STORE_SK", required_join=None, description="The store the return is attributed to."),
                       _dim("Return Date", f"{T['returns']}.SR_RETURNED_DATE", "time", "date", required_join=None, description="When the return was filed.")],
        "grain": f"{T['returns']}.SR_TICKET_NUMBER", "population": "Returns that reached a resolved or closed state",
        "examples": [{"question": "How many returns did store 42 resolve in the last 30 days?", "sql": f"SELECT COUNT(*) FROM {T['returns']} WHERE SR_STORE_SK = 42 AND SR_RESOLVED_AT >= DATEADD(day, -30, CURRENT_TIMESTAMP)", "source": "metric_builder"}]},
        description="Returns resolved for one store in a window", display_name="Resolved Returns", source_id="metric:builder:0003")

    # --- the authored complex record (joins on computed keys, dimension values, parameters) ------
    R["ratio_joins_dims_window"] = _record("sales_per_shopper", {
        "formula": "COUNT(*) / NULLIF(COUNT(DISTINCT m.SS_CUSTOMER_SK), 0)",
        "aggregation": {"type": "ratio", "zero_denominator": "null", "null_handling": "shoppers with no sales are not counted",
                        "reaggregation": "not additive: recompute sales and distinct shoppers, then divide"},
        "output": {"alias": "sales_per_shopper", "datatype": "decimal", "unit": "sales per shopper"},
        "source": {"table": T["sales"], "alias": "m", "entity": T["sales"], "timeframe_column": "SS_SOLD_TIMESTAMP",
                   "parameters": [{"name": "start_date", "datatype": "date", "default": "2026-03-18"},
                                  {"name": "end_date", "datatype": "date", "default": "2026-06-16"}]},
        "joins": [{"name": "a", "to": T["customer"], "sql": "LEFT(m.SS_CUSTOMER_ID, 15) = LEFT(a.C_CUSTOMER_ID, 15)",
                   "cardinality": "many_to_one", "origin": "observed in analyst query (LEFT JOIN)"},
                  {"name": "u", "to": T["address"], "sql": "a.C_CURRENT_ADDR_SK = u.CA_ADDRESS_SK",
                   "cardinality": "many_to_one", "origin": "observed in analyst query (LEFT JOIN)"}],
        "dimensions": [_dim("store_sk", "m.SS_STORE_SK", description="The store."),
                       _dim("state", "COALESCE(NULLIF(u.CA_STATE, ''), '(unknown)')", required_join="u", description="Customer state, '(unknown)' when missing."),
                       _dim("sale_date", "DATE_TRUNC('day', m.SS_SOLD_TIMESTAMP)", "time", "date", description="Day of sale."),
                       _dim("segment", "a.C_SEGMENT", values=["Majors", "Growth"], values_truncated=True,
                            value_map={"Majors": "large accounts", "Growth": "mid-size accounts"},
                            filter_form="a.C_SEGMENT = :value", description="Customer segment.")],
        "mandatory_filters": ["m.SS_CHANNEL = 'store'", "m.SS_CUSTOMER_EMAIL NOT ILIKE '%@example.com%'"],
        "filters": ["a.C_SEGMENT = 'Majors'", "a.C_IS_ACTIVE = TRUE",
                    "m.SS_SOLD_TIMESTAMP >= :start_date AND m.SS_SOLD_TIMESTAMP < DATEADD(day, 1, :end_date)"],
        "grain": "store_sk, state", "population": "Store-channel sales to active Majors customers, excluding test emails",
        "exclusions": ["online channel (SS_CHANNEL != 'store')", "test accounts (email ILIKE '%@example.com%')"],
        "depends_on": ["store_sales_count"],
        "examples": [{"question": "Which stores have the most sales per shopper this quarter?",
                      "sql": (f"SELECT m.SS_STORE_SK, COALESCE(NULLIF(u.CA_STATE,''),'(unknown)') AS STATE, COUNT(*) / NULLIF(COUNT(DISTINCT m.SS_CUSTOMER_SK), 0) AS SALES_PER_SHOPPER "
                              f"FROM {T['sales']} m LEFT JOIN {T['customer']} a ON LEFT(m.SS_CUSTOMER_ID,15)=LEFT(a.C_CUSTOMER_ID,15) "
                              f"LEFT JOIN {T['address']} u ON a.C_CURRENT_ADDR_SK = u.CA_ADDRESS_SK "
                              "WHERE a.C_SEGMENT = 'Majors' AND a.C_IS_ACTIVE = TRUE AND m.SS_CHANNEL = 'store' AND m.SS_CUSTOMER_EMAIL NOT ILIKE '%@example.com%' "
                              "AND m.SS_SOLD_TIMESTAMP >= '2026-03-18' AND m.SS_SOLD_TIMESTAMP < DATEADD(day,1,'2026-06-16') GROUP BY 1, 2 ORDER BY SALES_PER_SHOPPER DESC"),
                      "source": "analyst"},
                     {"question": "What is the sales-per-shopper ratio for the Majors segment?",
                      "sql": (f"SELECT a.C_SEGMENT AS SEGMENT, COUNT(*) / NULLIF(COUNT(DISTINCT m.SS_CUSTOMER_SK), 0) AS SALES_PER_SHOPPER "
                              f"FROM {T['sales']} m LEFT JOIN {T['customer']} a ON LEFT(m.SS_CUSTOMER_ID,15)=LEFT(a.C_CUSTOMER_ID,15) "
                              "WHERE a.C_SEGMENT = 'Majors' AND a.C_IS_ACTIVE = TRUE AND m.SS_CHANNEL = 'store' AND m.SS_SOLD_TIMESTAMP >= '2026-03-18' GROUP BY 1"),
                      "source": "analyst"}],
        "provenance": {"source": "analyst", "trust_score": 8.0, "observed_sql": None}},
        description="Average store sales per active shopper, by store and state, for active Majors customers.")
    # --- BigQuery names: a whole path in one pair of backticks, a join target quoted per part,
    #     a column qualified by the backticked table name
    R["bigquery_backticked_names"] = _record("paid_by_birth_country", {
        "dialect": "bigquery", "formula": "SUM(`store_sales`.ss_net_paid)",
        "aggregation": {"type": "sum", "reaggregation": "sum"},
        "output": {"alias": "paid_by_birth_country", "datatype": "decimal", "unit": "currency"},
        "source": {"table": "`tpcds.public.store_sales`", "alias": "s", "entity": "`tpcds.public.store_sales`"},
        "joins": [{"name": "c", "to": "`tpcds`.`public`.`customer`", "cardinality": "many_to_one", "origin": "declared",
                   "sql": "s.ss_customer_sk = c.c_customer_sk"}],
        "filters": ["s.ss_quantity > 0"],
        "dimensions": [_dim("birth_country", "c.c_birth_country", description="Customer birth country.")],
        "grain": "sale", "population": "Store sales with a positive quantity"},
        description="Net paid on store sales, by customer birth country")
    return R


if __name__ == "__main__":
    for k, v in records().items():
        print(f"{k:38} {v['name']}")
