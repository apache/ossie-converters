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

view: users {
  sql_table_name: `shop.analytics.users` ;;

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }
  dimension: first_name {
    sql: ${TABLE}.first_name ;;
  }
  dimension: last_name {
    sql: ${TABLE}.last_name ;;
  }
  dimension: full_name {
    sql: ${first_name} || ' ' || ${last_name} ;;
  }
  dimension: country {
    type: string
    sql: ${TABLE}.country ;;
  }
  measure: count {
    type: count
  }
}
