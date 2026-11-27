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

"""Static Cube source templates and guarded native round trips."""

import copy
import json

import pytest
from _util import by_name, model_of, parse, stash_of

from ossie_cube import (ConversionError, IssueType, convert_cube_to_ossie,
                        convert_cube_view_to_ossie, convert_ossie_to_cube)
from ossie_cube._common import dump_yaml


def _cube(name, kind, source):
    return {"name": name, kind: source,
            "dimensions": [{"name": "id", "sql": "id", "type": "number", "primary_key": True}],
            "measures": [{"name": "count", "type": "count"}]}


def _files(*cubes, view=False):
    model = {"cubes": list(cubes)}
    if view:
        model["views"] = [{"name": "sales", "cubes": [
            {"join_path": "active_users", "includes": ["count"]}]}]
    return {"model.yml": dump_yaml(model)}


def _sources(ossie):
    return {ds["name"]: ds["source"] for ds in model_of(ossie)["datasets"]}


def _exported_cubes(ossie):
    files, _ = convert_ossie_to_cube(ossie, base_cube="active_users")
    return by_name([cube for text in files.values() for cube in parse(text).get("cubes", [])])


@pytest.mark.parametrize("reference", ["{users.sql()}", "${users.sql()}", "{ users.sql() }"])
def test_static_table_sql_reference_is_expanded(reference):
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", "sql", f"SELECT * FROM {reference} u WHERE u.active"))
    ossie, _ = convert_cube_to_ossie(files)
    dollar = "$" if reference.startswith("$") else ""
    assert _sources(ossie)["active_users"] == f"SELECT * FROM {dollar}example_db.public.users u WHERE u.active"
    assert _exported_cubes(ossie)["active_users"]["sql"] == files_source(files, "active_users")


def files_source(files, name):
    return by_name(parse(files["model.yml"])["cubes"])[name]["sql"]


@pytest.mark.parametrize("base_sql,embedded", [
    ("SELECT * FROM example_db.public.users", "example_db.public.users"),
    (" select * from example_db.public.users ", "example_db.public.users"),
    ('SELECT * FROM "db"."schema"."users"', '"db"."schema"."users"'),
    ("SELECT id FROM example_db.public.users", "(SELECT id FROM example_db.public.users)"),
    ("WITH u AS (SELECT * FROM example_db.public.users) SELECT * FROM u",
     "(WITH u AS (SELECT * FROM example_db.public.users) SELECT * FROM u)"),
])
def test_referenced_queries_follow_cube_wrapping(base_sql, embedded):
    files = _files(_cube("users", "sql", base_sql),
                   _cube("active_users", "sql", "SELECT * FROM {users.sql()} u WHERE u.active"))
    ossie, _ = convert_cube_to_ossie(files)
    assert _sources(ossie)["active_users"] == f"SELECT * FROM {embedded} u WHERE u.active"
    assert _exported_cubes(ossie)["users"]["sql"] == base_sql.strip()


def test_recursive_sql_dependencies_keep_the_original_definition():
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("filtered", "sql", "SELECT * FROM {users.sql()} WHERE active"),
                   _cube("active_users", "sql", "SELECT * FROM {filtered.sql()} f WHERE f.id > 0"))
    ossie, _ = convert_cube_to_ossie(files)
    assert _sources(ossie)["active_users"] == (
        "SELECT * FROM (SELECT * FROM example_db.public.users WHERE active) f WHERE f.id > 0")
    assert _exported_cubes(ossie)["active_users"]["sql"] == files_source(files, "active_users")


@pytest.mark.parametrize("source", [
    "SELECT * FROM {FILTER_PARAMS.users.id.filter('id')}",
    "SELECT * FROM {COMPILE_CONTEXT.securityContext.table}",
    "SELECT * FROM {users.id}",
    "SELECT * FROM {users.sql(1)}",
    '"{CUBE}.orders"',
])
@pytest.mark.parametrize("kind", ["sql", "sql_table"])
def test_runtime_and_unsupported_sources_set_only_their_cube_aside(source, kind):
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", kind, source))
    ossie, issues = convert_cube_to_ossie(files)
    assert _sources(ossie) == {"users": "example_db.public.users"}
    [issue] = issues.of_type(IssueType.TEMPLATED_CUBE_SKIPPED)
    assert issue.element_name == "cube 'active_users'"
    assert "static" in issue.detail
    exported, _ = convert_ossie_to_cube(ossie)
    # Restored verbatim, into the file it came from.
    restored = by_name(parse(exported["model.yml"])["cubes"])["active_users"]
    assert restored == by_name(parse(files["model.yml"])["cubes"])["active_users"]


def test_a_model_with_only_runtime_sources_is_refused():
    with pytest.raises(ConversionError, match="no cube has a static source.*active_users"):
        convert_cube_to_ossie(_files(_cube(
            "active_users", "sql", "SELECT * FROM {FILTER_PARAMS.active_users.id.filter('id')}")))


def test_projection_still_refuses_a_published_runtime_source():
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", "sql",
                         "SELECT * FROM {FILTER_PARAMS.active_users.id.filter('id')}"), view=True)
    with pytest.raises(ConversionError, match="active_users.*source.*static"):
        convert_cube_view_to_ossie(files, "sales")


def test_unknown_source_dependency_has_a_clean_error():
    with pytest.raises(ConversionError, match="active_users.*missing.*not.*cube"):
        convert_cube_to_ossie(_files(_cube("active_users", "sql", "SELECT * FROM {missing.sql()}")))


@pytest.mark.parametrize("cubes,chain", [
    ([_cube("active_users", "sql", "SELECT * FROM {active_users.sql()}")],
     "active_users -> active_users"),
    ([_cube("active_users", "sql", "SELECT * FROM {users.sql()}"),
      _cube("users", "sql", "SELECT * FROM {active_users.sql()}")],
     "active_users -> users -> active_users"),
])
def test_source_cycles_name_the_dependency_chain(cubes, chain):
    with pytest.raises(ConversionError, match=chain):
        convert_cube_to_ossie(_files(*cubes))


@pytest.mark.parametrize("edit", ["own_source", "dependency_source", "remove_dependency", "rename_dependency"])
def test_native_source_templates_do_not_override_source_edits(edit):
    ossie, _ = convert_cube_to_ossie(_files(
        _cube("users", "sql_table", "example_db.public.users"),
        _cube("active_users", "sql", "SELECT * FROM {users.sql()} WHERE active")))
    model = model_of(ossie)
    datasets = by_name(model["datasets"])
    if edit == "own_source":
        datasets["active_users"]["source"] = "SELECT * FROM example_db.public.replacement"
    elif edit == "dependency_source":
        datasets["users"]["source"] = "example_db.public.replacement"
    elif edit == "remove_dependency":
        model["datasets"] = [datasets["active_users"]]
    else:
        datasets["users"]["name"] = "renamed_users"
    restored = _exported_cubes(dump_yaml(model))["active_users"]
    assert restored["sql"] == datasets["active_users"]["source"]
    assert "{users.sql()}" not in restored["sql"]


def test_projection_resolves_sources_from_cubes_outside_the_published_tree():
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", "sql", "SELECT * FROM {users.sql()} WHERE active"), view=True)
    original = copy.deepcopy(files)
    ossie, source, _ = convert_cube_view_to_ossie(files, "sales")
    assert source == "active_users"
    assert _sources(ossie) == {"active_users": "SELECT * FROM example_db.public.users WHERE active"}
    # The missing dependency must not return as a dangling native template.
    assert _exported_cubes(ossie)["active_users"]["sql"] == _sources(ossie)["active_users"]
    assert files == original


def test_source_expansion_is_bounded():
    cubes = [_cube("c0", "sql_table", "example_db.public.users")]
    for i in range(1, 24):
        cubes.append(_cube(f"c{i}", "sql", f"SELECT * FROM {{c{i-1}.sql()}} UNION ALL SELECT * FROM {{c{i-1}.sql()}}"))
    with pytest.raises(ConversionError, match="source.*expansion.*limit"):
        convert_cube_to_ossie(_files(*cubes))


def test_source_expansion_keeps_escaped_braces_literal():
    sql = r"SELECT id, '\{literal\}' AS label FROM example_db.public.users"
    files = _files(_cube("users", "sql", sql),
                   _cube("active_users", "sql", "SELECT * FROM {users.sql()} WHERE id > 0"))
    ossie, _ = convert_cube_to_ossie(files)
    assert _sources(ossie)["users"] == sql.replace(r"\{", "{").replace(r"\}", "}")
    assert "'{literal}'" in _sources(ossie)["active_users"]
    assert _exported_cubes(ossie)["users"]["sql"] == sql
    # A portable-only export must escape SQL braces for Cube's template compiler.
    model = model_of(ossie)
    for ds in model["datasets"]:
        ds.pop("nonportable_extensions", None)
    model.pop("nonportable_extensions", None)
    exported = _exported_cubes(dump_yaml(model))
    assert r"'\{literal\}'" in exported["active_users"]["sql"]
    again, _ = convert_cube_to_ossie(_files(*exported.values()))
    assert _sources(again) == _sources(ossie)


def test_source_templates_respect_table_qualification_defaults():
    files = _files(_cube("users", "sql_table", "users"),
                   _cube("active_users", "sql", "SELECT * FROM {users.sql()} WHERE active"))
    ossie, _ = convert_cube_to_ossie(files, default_catalog="example_db", default_schema="public")
    assert _sources(ossie)["active_users"] == "SELECT * FROM example_db.public.users WHERE active"
    assert _exported_cubes(ossie)["users"]["sql_table"] == "example_db.public.users"


def test_a_source_template_can_resolve_a_table_definition():
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", "sql_table", "{users.sql()}"))
    ossie, _ = convert_cube_to_ossie(files)
    assert _sources(ossie)["active_users"] == "example_db.public.users"
    restored = _exported_cubes(ossie)["active_users"]
    assert restored["sql_table"] == "{users.sql()}"
    assert "sql" not in restored


@pytest.mark.parametrize("reverse", [False, True])
def test_long_source_chains_fail_cleanly_before_python_recursion(reverse):
    cubes = [_cube(f"c{i}", "sql", f"SELECT * FROM {{c{i+1}.sql()}}") for i in range(110)]
    cubes.append(_cube("c110", "sql_table", "example_db.public.users"))
    if reverse:
        cubes.reverse()
    with pytest.raises(ConversionError, match="source dependency depth.*limit"):
        convert_cube_to_ossie(_files(*cubes))


def test_escaped_literal_sources_need_no_reconstruction_metadata():
    files = _files(_cube("active_users", "sql", r"SELECT '\{literal\}' AS label FROM example_db.public.users"))
    ossie, _ = convert_cube_to_ossie(files)
    assert not stash_of(model_of(ossie)["datasets"][0])
    assert _exported_cubes(ossie)["active_users"]["sql"] == files_source(files, "active_users")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("dialect,comment", [(None, "-- source comment"), ("BIGQUERY", "# source comment")])
def test_referenced_queries_preserve_line_comment_terminators(newline, dialect, comment):
    import sqlglot

    sql = f"SELECT id FROM example_db.public.users {comment}{newline}"
    files = _files(_cube("users", "sql", sql), _cube(
        "active_users", "sql", "SELECT * FROM {users.sql()} u WHERE u.id > 0"))
    ossie, _ = convert_cube_to_ossie(files, source_dialect=dialect)
    trimmed = sql.rstrip() + "\n"
    assert _sources(ossie)["users"] == trimmed
    assert _sources(ossie)["active_users"] == f"SELECT * FROM ({trimmed}) u WHERE u.id > 0"
    parsed = sqlglot.parse_one(_sources(ossie)["active_users"], read="bigquery" if dialect else None)
    assert parsed.args.get("where") is not None
    assert _exported_cubes(ossie)["users"]["sql"] == trimmed


@pytest.mark.parametrize("sql,trimmed", [
    # A block scalar's final newline is not part of the source.
    ("SELECT id\nFROM example_db.public.users\n", "SELECT id\nFROM example_db.public.users"),
    ("  SELECT id FROM example_db.public.users -- source comment\n",
     "SELECT id FROM example_db.public.users -- source comment\n"),
    # Cube wraps the source in parentheses, so a final comment needs its newline.
    ("SELECT id FROM example_db.public.users # source comment",
     "SELECT id FROM example_db.public.users # source comment\n"),
])
def test_sql_sources_are_trimmed_except_for_a_final_line_comment(sql, trimmed):
    ossie, _ = convert_cube_to_ossie(_files(_cube("active_users", "sql", sql)))
    model = model_of(ossie)
    assert model["datasets"][0]["source"] == trimmed
    model.pop("nonportable_extensions", None)
    for ds in model["datasets"]:
        ds.pop("nonportable_extensions", None)
    assert _exported_cubes(dump_yaml(model))["active_users"]["sql"] == trimmed


def test_an_ossie_source_ending_in_a_line_comment_exports_terminated():
    ossie, _ = convert_cube_to_ossie(_files(_cube("active_users", "sql_table", "example_db.public.users")))
    model = model_of(ossie)
    model["datasets"][0]["source"] = "SELECT id FROM example_db.public.users -- authored in Ossie"
    model.pop("nonportable_extensions", None)
    assert _exported_cubes(dump_yaml(model))["active_users"]["sql"] == (
        "SELECT id FROM example_db.public.users -- authored in Ossie\n")


@pytest.mark.parametrize("template", [
    "malformed", ["malformed"],
    {"kind": "sql", "source_signatures": {"active_users": "0" * 64}},
    {"kind": "sql", "value": 42, "source_signatures": {"active_users": "0" * 64}},
    {"kind": "sql", "value": "{users.sql()}", "source_signatures": ["active_users"]},
    # The earlier snapshot format, with the full source text: it no longer matches.
    {"kind": "sql", "value": "SELECT * FROM {users.sql()} WHERE active",
     "sources": {"active_users": "SELECT * FROM example_db.public.users WHERE active",
                 "users": "example_db.public.users"}},
])
def test_malformed_native_source_metadata_falls_back_to_the_portable_source(template):
    from ossie_cube._common import write_stash

    ossie, _ = convert_cube_to_ossie(_files(
        _cube("users", "sql_table", "example_db.public.users"),
        _cube("active_users", "sql", "SELECT * FROM {users.sql()} WHERE active")))
    model = model_of(ossie)
    ds = by_name(model["datasets"])["active_users"]
    write_stash(ds, {"source_template": template})
    assert _exported_cubes(dump_yaml(model))["active_users"]["sql"] == ds["source"]


def test_source_snapshots_store_signatures_not_source_text():
    cubes = [_cube("c0", "sql_table", "example_db.public.users")]
    for i in range(1, 6):
        cubes.append(_cube(f"c{i}", "sql", f"SELECT id FROM {{c{i-1}.sql()}} WHERE id > {i}"))
    ossie, _ = convert_cube_to_ossie(_files(*cubes))
    snapshot = stash_of(by_name(model_of(ossie)["datasets"])["c5"])["source_template"]
    assert sorted(snapshot["source_signatures"]) == [f"c{i}" for i in range(6)]
    assert "example_db.public.users" not in json.dumps(snapshot)
    restored = _exported_cubes(ossie)
    assert restored["c5"]["sql"] == "SELECT id FROM {c4.sql()} WHERE id > 5"
    # The restored source keeps its place right after the name.
    assert list(restored["c5"])[:2] == ["name", "sql"]


def _star(fact_source):
    orders = _cube("orders", "sql", fact_source)
    orders["joins"] = [{"name": "customers", "relationship": "many_to_one",
                        "sql": "{CUBE}.customer_id = {customers.id}"}]
    model = {"cubes": [orders,
                       _cube("recent_orders", "sql", "SELECT * FROM {orders.sql()} WHERE recent"),
                       _cube("customers", "sql_table", "example_db.public.customers")],
             "views": [{"name": "sales", "cubes": [
                 {"join_path": "orders", "includes": "*"},
                 {"join_path": "orders.customers", "prefix": True, "includes": "*"}]}]}
    return {"model.yml": dump_yaml(model)}


def test_a_runtime_fact_cube_and_its_dependents_round_trip_beside_the_rest():
    files = _star("SELECT * FROM example_db.public.orders WHERE {FILTER_PARAMS.orders.id.filter('id')}")
    ossie, issues = convert_cube_to_ossie(files)
    assert _sources(ossie) == {"customers": "example_db.public.customers"}
    skipped = {i.element_name: i.detail for i in issues.of_type(IssueType.TEMPLATED_CUBE_SKIPPED)}
    assert list(skipped) == ["cube 'orders'", "cube 'recent_orders'"]
    assert "'orders.sql()'" in skipped["cube 'recent_orders'"]
    exported = parse(convert_ossie_to_cube(ossie)[0]["model.yml"])
    original = parse(files["model.yml"])
    for name in ("orders", "recent_orders"):
        assert by_name(exported["cubes"])[name] == by_name(original["cubes"])[name]
    assert exported["views"] == original["views"]


def test_a_join_into_a_set_aside_cube_names_the_reason():
    customers = _cube("customers", "sql_table", "example_db.public.customers")
    customers["joins"] = [{"name": "orders", "relationship": "one_to_many",
                           "sql": "{CUBE}.id = {orders.customer_id}"}]
    files = _files(customers, _cube("orders", "sql_table", "{COMPILE_CONTEXT.securityContext.tenant}.orders"))
    with pytest.raises(ConversionError, match="'orders' is not converted.*COMPILE_CONTEXT"):
        convert_cube_to_ossie(files)


def test_a_source_dependency_in_an_unparsed_file_sets_the_cube_aside():
    files = _files(_cube("users", "sql_table", "example_db.public.users"),
                   _cube("active_users", "sql", "SELECT * FROM {accounts.sql()} WHERE active"))
    files["accounts.js"] = "cube(`accounts`, { sql_table: `example_db.public.accounts` })"
    ossie, issues = convert_cube_to_ossie(files)
    assert _sources(ossie) == {"users": "example_db.public.users"}
    [issue] = issues.of_type(IssueType.TEMPLATED_CUBE_SKIPPED)
    assert "'accounts.js'" in issue.detail
    exported, _ = convert_ossie_to_cube(ossie)
    assert exported["accounts.js"] == files["accounts.js"]
    assert by_name(parse(exported["model.yml"])["cubes"])["active_users"]["sql"] == (
        "SELECT * FROM {accounts.sql()} WHERE active")


def test_projection_names_unparsed_files_for_a_missing_source_dependency():
    files = _files(_cube("active_users", "sql", "SELECT * FROM {accounts.sql()} WHERE active"), view=True)
    files["accounts.js"] = "cube(`accounts`, { sql_table: `example_db.public.accounts` })"
    with pytest.raises(ConversionError, match="'accounts'.*no cube was converted from 'accounts.js'"):
        convert_cube_view_to_ossie(files, "sales")


def test_a_set_aside_cube_cannot_collide_with_a_dataset():
    ossie, _ = convert_cube_to_ossie(_files(
        _cube("users", "sql_table", "example_db.public.users"),
        _cube("active_users", "sql", "SELECT * FROM {FILTER_PARAMS.active_users.id.filter('id')}")))
    model = model_of(ossie)
    model["datasets"][0]["name"] = "Active_Users"
    with pytest.raises(ConversionError, match="stashed cube 'active_users'.*dataset"):
        convert_ossie_to_cube(dump_yaml(model))
