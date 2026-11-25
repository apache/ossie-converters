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

"""Literal join predicates must not become portable column relationships."""

import pytest
from _util import by_name, model_of, parse, stash_of

from ossie_cube import IssueType, convert_cube_to_ossie, convert_ossie_to_cube
from ossie_cube._common import dump_yaml


def _files(join_sql, member_sql=None):
    orders = {"name": "orders", "sql_table": "example_db.public.orders",
              "dimensions": [{"name": "id", "sql": "id", "type": "number",
                              "primary_key": True}],
              "joins": [{"name": "users", "relationship": "many_to_one", "sql": join_sql}]}
    if member_sql is not None:
        orders["dimensions"].append({"name": "flag", "sql": member_sql, "type": "boolean"})
    return {"model.yml": dump_yaml({"cubes": [orders, {
        "name": "users", "sql_table": "example_db.public.users",
        "dimensions": [{"name": "id", "sql": "id", "type": "number", "primary_key": True}],
    }]})}


def _assert_parked(files, **kwargs):
    ossie, issues = convert_cube_to_ossie(files, **kwargs)
    model = model_of(ossie)
    assert not model.get("relationships")
    orders = by_name(model["datasets"])["orders"]
    assert {f["name"] for f in orders["fields"]} <= {"id", "flag"}
    assert stash_of(orders)["extra_joins"][0]["join"] == parse(files["model.yml"])["cubes"][0]["joins"][0]
    assert any("does not resolve to two physical columns" in i.detail
               for i in issues.of_type(IssueType.PARKED_IN_META))
    back, _ = convert_ossie_to_cube(ossie)
    restored = parse(back["model.yml"])["cubes"][0]["joins"]
    assert restored == parse(files["model.yml"])["cubes"][0]["joins"]

    # A portable handoff loses the native join, rather than acquiring a false key.
    def strip_extensions(node):
        if isinstance(node, dict):
            return {k: strip_extensions(v) for k, v in node.items() if k != "nonportable_extensions"}
        if isinstance(node, list):
            return [strip_extensions(v) for v in node]
        return node

    portable, _ = convert_ossie_to_cube(dump_yaml(strip_extensions(model)), base_cube="orders")
    assert all(not cube.get("joins") for text in portable.values()
               for cube in parse(text).get("cubes", []))


@pytest.mark.parametrize("literal", [
    "true", "TRUE", "FaLsE", "NULL", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_USER",
])
@pytest.mark.parametrize("reverse", [False, True])
def test_a_literal_predicate_parks_the_whole_join(literal, reverse):
    predicate = f"{literal} = {{users}}.active" if reverse else f"{{users}}.active = {literal}"
    _assert_parked(_files("{CUBE}.user_id = {users}.id AND " + predicate))


@pytest.mark.parametrize("literal", ["true", "false", "NULL", "CURRENT_DATE", "CURRENT_TIMESTAMP"])
@pytest.mark.parametrize("reference", ["{CUBE.flag}", "{flag}"])
def test_a_member_reading_a_literal_is_not_a_join_column(literal, reference):
    _assert_parked(_files(reference + " = {users}.id", member_sql=literal))


@pytest.mark.parametrize("column", ["user_id", "{CUBE}.user_id", "{CUBE}.true"])
def test_real_column_equalities_remain_portable(column):
    ossie, _ = convert_cube_to_ossie(_files(column + " = {users}.id"))
    rel = model_of(ossie)["relationships"][0]
    assert rel["from_columns"] == [column.rsplit(".", 1)[-1]]
    assert rel["to_columns"] == ["id"]


def test_a_quoted_keyword_is_preserved_as_a_computed_member():
    # Quoted SQL names are not boolean literals. The conservative join resolver
    # already parks quoted endpoints; preserve their spelling for the native hop.
    _assert_parked(_files("{flag} = {users}.id", member_sql='"true"'))


@pytest.mark.parametrize("literal,dialect", [
    ("LOCALTIME", "SNOWFLAKE"), ("LOCALTIMESTAMP", "SNOWFLAKE"),
    ("CURRENT_DATETIME", "BIGQUERY"),
])
def test_niladic_values_use_the_requested_warehouse_dialect(literal, dialect):
    _assert_parked(_files("{users}.active = " + literal), source_dialect=dialect)


@pytest.mark.parametrize("keyword", ["true", "NULL", "CURRENT_DATE"])
@pytest.mark.parametrize("reference", ["{%s}", "{CUBE.%s}", "{orders.%s}"])
def test_explicit_keyword_named_member_chains_remain_columns(keyword, reference):
    model = parse(_files("{flag} = {users}.id", member_sql=reference % keyword)["model.yml"])
    model["cubes"][0]["dimensions"].append({
        "name": keyword, "sql": "{CUBE}.user_id", "type": "number"})
    files = {"model.yml": dump_yaml(model)}
    ossie, _ = convert_cube_to_ossie(files)
    assert model_of(ossie)["relationships"][0]["from_columns"] == ["user_id"]
    back, _ = convert_ossie_to_cube(ossie)
    assert parse(back["model.yml"])["cubes"][0]["joins"] == model["cubes"][0]["joins"]


@pytest.mark.parametrize("keyword", ["true", "NULL", "CURRENT_DATE"])
def test_bare_sql_literals_do_not_read_same_named_members(keyword):
    model = parse(_files("{flag} = {users}.id", member_sql=keyword)["model.yml"])
    model["cubes"][0]["dimensions"].append({
        "name": keyword, "sql": "{CUBE}.user_id", "type": "number"})
    ossie, issues = convert_cube_to_ossie({"model.yml": dump_yaml(model)})
    assert not model_of(ossie).get("relationships")
    assert issues.of_type(IssueType.PARKED_IN_META)


def test_explicit_self_referencing_join_member_is_parked():
    _assert_parked(_files("{flag} = {users}.id", member_sql="{flag}"))
