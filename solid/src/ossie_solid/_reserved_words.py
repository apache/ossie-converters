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

"""Reserved words per dialect, for deciding when an identifier must be quoted.

sqlglot cannot decide this: it parses `order`, `group` and `limit` as ordinary columns
in every dialect, and ships a reserved-word list only for BigQuery. So the lists are
kept here, from each warehouse's documentation.

How wide each list is follows from what quoting costs in that dialect:

- **Snowflake** and the **SQL standard** fold an unquoted name to upper case and match a
  quoted one exactly, so quoting a name that did not need it can stop it matching its
  column. Those two lists are therefore exactly the documented reserved words, no more.
  (Solid reports names in the case the catalog stores them, so a reserved name it does
  quote, `"ORDER"`, still matches.)
- **Databricks** and **BigQuery** match a backtick-quoted name case-insensitively, so
  needless quoting is harmless there, and their lists also take in the SQL standard's.

All entries are upper case; compare `name.upper()`.
"""


def _words(text):
    return frozenset(text.split())


# Snowflake, "Reserved & limited keywords". The same 92 words as solid-server's
# SNOWFLAKE_RESERVED_WORDS_SET (common/src/value/semantic_layer/snowflake_constants.py).
SNOWFLAKE = _words("""
ACCOUNT ALL ALTER AND ANY AS BETWEEN BY CASE CAST CHECK COLUMN CONNECT CONNECTION
CONSTRAINT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER
DATABASE DELETE DISTINCT DROP ELSE EXISTS FALSE FOLLOWING FOR FROM FULL GRANT GROUP
GSCLUSTER HAVING ILIKE IN INCREMENT INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL
LEFT LIKE LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT NULL OF ON OR ORDER ORGANIZATION
QUALIFY REGEXP REVOKE RIGHT RLIKE ROW ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE
TABLESAMPLE THEN TO TRIGGER TRUE TRY_CAST UNION UNIQUE UPDATE USING VALUES VIEW WHEN
WHENEVER WHERE WINDOW WITH
""")

# The SQL standard's reserved words that common engines also reserve: PostgreSQL's
# fully reserved keywords, the join keywords, and the DML verbs. A word the standard
# reserves but engines accept as a column name (DATE, YEAR, VALUE, NAME) is left out, for
# the case-sensitivity reason above.
ANSI_SQL = _words("""
ALL AND ANY ARRAY AS ASC ASYMMETRIC BETWEEN BOTH BY CASE CAST CHECK COLLATE COLUMN
CONSTRAINT CREATE CROSS CURRENT_DATE CURRENT_ROLE CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_USER DEFAULT DELETE DESC DISTINCT DROP ELSE END EXCEPT EXISTS FALSE FETCH FOR
FOREIGN FROM FULL GRANT GROUP HAVING IN INNER INSERT INTERSECT INTO IS JOIN LATERAL
LEADING LEFT LIKE LIMIT LOCALTIME LOCALTIMESTAMP NATURAL NOT NULL OFFSET ON ONLY OR ORDER
OUTER PRIMARY REFERENCES RIGHT SELECT SESSION_USER SET SOME SYMMETRIC SYSTEM_USER TABLE
THEN TO TRAILING TRUE UNION UNIQUE UPDATE USER USING VALUES WHEN WHERE WINDOW WITH
""")

# Spark SQL's keywords reserved in ANSI mode, which Databricks enforces when
# `spark.sql.ansi.enforceReservedKeywords` is on, plus the words Databricks reserves
# whatever the mode (ANTI, SEMI, MINUS and the join keywords).
DATABRICKS = _words("""
ALL AND ANTI ANY AS AUTHORIZATION BOTH CASE CAST CHECK COLLATE COLUMN CONSTRAINT CREATE
CROSS CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DEFAULT DISTINCT ELSE END
ESCAPE EXCEPT FALSE FETCH FOR FOREIGN FROM FULL GRANT GROUP HAVING IN INNER INTERSECT
INTO IS JOIN LATERAL LEADING LEFT MINUS NATURAL NOT NULL OFFSET ON ONLY OR ORDER OUTER
OVERLAPS PRIMARY REFERENCES RIGHT SELECT SEMI SESSION_USER SOME TABLE THEN TIME TO
TRAILING TRUE UNION UNIQUE UNKNOWN USER USING WHEN WHERE WITH
""") | ANSI_SQL

# BigQuery, "Reserved keywords" in the lexical structure reference.
BIGQUERY = _words("""
ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE CONTAINS
CREATE CROSS CUBE CURRENT DEFAULT DEFINE DESC DISTINCT ELSE END ENUM ESCAPE EXCEPT
EXCLUDE EXISTS EXTRACT FALSE FETCH FOLLOWING FOR FROM FULL GROUP GROUPING GROUPS HASH
HAVING IF IGNORE IN INNER INTERSECT INTERVAL INTO IS JOIN LATERAL LEFT LIKE LIMIT LOOKUP
MERGE NATURAL NEW NO NOT NULL NULLS OF ON OR ORDER OUTER OVER PARTITION PRECEDING PROTO
QUALIFY RANGE RECURSIVE RESPECT RIGHT ROLLUP ROWS SELECT SET SOME STRUCT TABLESAMPLE THEN
TO TREAT TRUE UNBOUNDED UNION UNNEST USING WHEN WHERE WINDOW WITH WITHIN
""") | ANSI_SQL
