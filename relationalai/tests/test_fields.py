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

"""Dataset fields read through their SQL `expression`, never their `name`.

A field's name is what mappings call it; its expression is what it reads from
the table. The two differ as soon as a spec renames a column (`line_status`
reads `L_LINESTATUS`) or computes one (`missing_line_status` is
`"L_LINESTATUS" IS NULL`). The converter used to read every field by its name,
which `declared_table` hid: it declared the columns from those same names, so
each lookup found the column it had just made up. Against the real table --
what `warehouse_table` sees -- the lookups missed, and pyrel invented an
untyped property for each one.

The tests here therefore check the converter against tables declared with the
columns the warehouse actually has, not the ones the spec suggests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ossie_ontology.parser import OssieParser

pyrel = pytest.importorskip(
    "relationalai.semantics",
    reason='needs the optional relationalai extra: pip install -e ".[relationalai]"',
)

from relationalai.semantics import Boolean, Date, Decimal, Integer, String  # noqa: E402
from relationalai.semantics.metamodel.pyrel_codegen import to_pyrel  # noqa: E402
from relationalai.semantics.metamodel.typer import Typer  # noqa: E402

from ossie_relationalai import (  # noqa: E402
    OssieToRelationalAIConverter,
    declared_table,
)

# The columns of Snowflake's SNOWFLAKE_SAMPLE_DATA.TPCH_SF1 tables, as the
# warehouse reports them. AutoOntologyTPCH names its fields after what they
# mean (`line_status`), and only its expressions name these.
TPCH_SF1 = {
    "CUSTOMER": {"C_CUSTKEY": Integer, "C_NAME": String, "C_ADDRESS": String, "C_NATIONKEY": Integer,
                 "C_PHONE": String, "C_ACCTBAL": Decimal, "C_MKTSEGMENT": String, "C_COMMENT": String},
    "ORDERS": {"O_ORDERKEY": Integer, "O_CUSTKEY": Integer, "O_ORDERSTATUS": String, "O_TOTALPRICE": Decimal,
               "O_ORDERDATE": Date, "O_ORDERPRIORITY": String, "O_CLERK": String, "O_SHIPPRIORITY": Integer,
               "O_COMMENT": String},
    "LINEITEM": {"L_ORDERKEY": Integer, "L_PARTKEY": Integer, "L_SUPPKEY": Integer, "L_LINENUMBER": Integer,
                 "L_QUANTITY": Decimal, "L_EXTENDEDPRICE": Decimal, "L_DISCOUNT": Decimal, "L_TAX": Decimal,
                 "L_RETURNFLAG": String, "L_LINESTATUS": String, "L_SHIPDATE": Date, "L_COMMITDATE": Date,
                 "L_RECEIPTDATE": Date, "L_SHIPINSTRUCT": String, "L_SHIPMODE": String, "L_COMMENT": String},
    "PART": {"P_PARTKEY": Integer, "P_NAME": String, "P_MFGR": String, "P_BRAND": String, "P_TYPE": String,
             "P_SIZE": Integer, "P_CONTAINER": String, "P_RETAILPRICE": Decimal, "P_COMMENT": String},
    "PARTSUPP": {"PS_PARTKEY": Integer, "PS_SUPPKEY": Integer, "PS_AVAILQTY": Integer, "PS_SUPPLYCOST": Decimal,
                 "PS_COMMENT": String},
    "SUPPLIER": {"S_SUPPKEY": Integer, "S_NAME": String, "S_ADDRESS": String, "S_NATIONKEY": Integer,
                 "S_PHONE": String, "S_ACCTBAL": Decimal, "S_COMMENT": String},
    "NATION": {"N_NATIONKEY": Integer, "N_NAME": String, "N_REGIONKEY": Integer, "N_COMMENT": String},
    "REGION": {"R_REGIONKEY": Integer, "R_NAME": String, "R_COMMENT": String},
}


def tpch_table(ontology, dataset, schema):
    """A stand-in for `warehouse_table`: the real columns, without a connection.

    The spec-derived *schema* is ignored on purpose -- using it is exactly what
    hid the bug.
    """
    real = TPCH_SF1[dataset.source.rsplit(".", 1)[-1]]
    return ontology.base_model().Table(dataset.source, schema=real)


def convert(path: Path, table_provider=declared_table) -> str:
    pyrel.Model.all_models.clear()
    om = OssieToRelationalAIConverter.convert(OssieParser().parse(path), table_provider=table_provider)
    return to_pyrel(om.base_model().to_metamodel())


def assert_executable(source: str, name: str) -> None:
    namespace: dict = {}
    pyrel.Model.all_models.clear()
    exec(compile(source, f"generated_{name}.py", "exec"), namespace)
    Typer().infer_model(namespace["m"].to_metamodel())


def define_rules(source: str) -> list[str]:
    return [line for line in source.splitlines() if ".define(" in line]


# ---------------------------------------------------------------------------
# Against the real table
# ---------------------------------------------------------------------------

def test_a_spec_that_renames_its_columns_reads_the_real_ones(specs_dir: Path):
    """Every AutoOntologyTPCH field resolves to a column the warehouse has.

    Before, all but the handful named like their columns came out as
    `{table} has {Any:line_status}` -- a property pyrel invents for a column it
    cannot find, and the PyRel osi-lab produced for this spec.
    """
    source = convert(specs_dir / "AutoOntologyTPCH.yaml", table_provider=tpch_table)

    assert "Any" not in source
    assert any(".define(orderlineitem.line_status(" in rule and ".L_LINESTATUS))" in rule
               for rule in define_rules(source))
    assert_executable(source, "AutoOntologyTPCH_real_columns")


def test_a_declared_table_declares_the_columns_the_expressions_read(specs_dir: Path):
    """`declared_table` declares the real LINEITEM columns, and nothing else.

    Not LINE_STATUS (the field's name) and not MISSING_LINE_STATUS (a field
    computed from L_LINESTATUS, which is no column at all).
    """
    source = convert(specs_dir / "AutoOntologyTPCH.yaml")
    lineitem = next(line for line in source.splitlines()
                    if 'm.Table("SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM"' in line)
    declared = set(re.findall(r"'([A-Z_]+)': [A-Za-z]+", lineitem))

    assert declared == set(TPCH_SF1["LINEITEM"])


# ---------------------------------------------------------------------------
# One field per expression shape
# ---------------------------------------------------------------------------

ITEMS = "db_schema_items"


def test_each_expression_shape_renders_as_pyrel(behaviours_dir: Path):
    source = convert(behaviours_dir / "field_expressions.yaml")
    rules = define_rules(source)

    def bound(prop: str) -> list[str]:
        return [rule for rule in rules if f".define(item.{prop}(" in rule]

    table = next(line for line in source.splitlines() if "m.Table(" in line)
    # Renamed, quoted and computed-only columns are declared; field names are not.
    assert "'I_NR': Integer" in table and "'I_LABEL': String" in table
    assert "'\"i_code\"': String" in table and "'I_PRICE': Decimal" in table
    assert "'LABEL'" not in table and "'MISSING_LABEL'" not in table and "'GROSS'" not in table

    assert "m.define(Item.new(nr=db_schema_items.I_NR))" in rules
    assert bound("label") == [r for r in rules if f"item.label({ITEMS}.I_LABEL)" in r]
    assert any(f"item.code({ITEMS}['\"i_code\"'])" in r for r in bound("code"))
    assert any(f"_concat(_concat({ITEMS}.I_LABEL, \" #\"), {ITEMS}['\"i_code\"'])" in r for r in bound("full_label"))
    assert any(f"item.gross(({ITEMS}.I_PRICE * 1.2))" in r for r in bound("gross"))

    # A condition is one rule, asserting TRUE where it holds and nothing
    # elsewhere. The SNOWFLAKE dialect wins over ANSI_SQL.
    assert bound("missing_label") == [r for r in rules if f"m.not_({ITEMS}.I_LABEL)" in r]
    assert len(bound("missing_label")) == 1
    assert bound("missing_label")[0].endswith("missing_label(True))")

    # NOT (price >= 10) is TRUE below 10. A NULL price is UNKNOWN in SQL, and
    # gets no fact here either, because the rule compares the price.
    cheap = bound("is_cheap")
    assert len(cheap) == 1
    assert f"{ITEMS}.I_PRICE < 10" in cheap[0] and cheap[0].endswith("is_cheap(True))")

    assert "(False))" not in source

    assert_executable(source, "field_expressions")


@pytest.mark.parametrize("expression, holds", [
    ("a IS NOT NULL", "m.not_(m.not_({t}.A))"),
    # Not `m.not_({t}.A == 1)`: that would also hold where `a` is NULL, which
    # SQL calls UNKNOWN, not TRUE.
    ("NOT (a = 1)", "{t}.A != 1"),
    ("a = 1 AND b = 2", "{t}.A == 1, {t}.B == 2"),
    ("a = 1 OR b IS NULL", "m.union({t}.A == 1, m.not_({t}.B))"),
    ("NOT (a = 1 OR b IS NULL)", "{t}.A != 1, m.not_(m.not_({t}.B))"),
])
def test_a_condition_is_bound_where_sql_says_it_is_true(tmp_path: Path, expression: str, holds: str):
    spec = _single_field_spec(tmp_path, expression, "Boolean", columns={"a": "Integer", "b": "Integer"})
    flag = [rule for rule in define_rules(convert(spec)) if ".define(item.flag(" in rule]
    t = "db_schema_items"

    assert len(flag) == 1, flag
    assert holds.format(t=t) in flag[0] and flag[0].endswith("flag(True))"), flag


# ---------------------------------------------------------------------------
# What is refused, and how
# ---------------------------------------------------------------------------

def test_a_column_the_table_lacks_fails_naming_the_field(specs_dir: Path):
    """Against a table that really lacks the column, conversion fails loudly."""
    def without_line_status(ontology, dataset, schema):
        real = dict(TPCH_SF1[dataset.source.rsplit(".", 1)[-1]])
        real.pop("L_LINESTATUS", None)
        return ontology.base_model().Table(dataset.source, schema=real)

    with pytest.raises(ValueError, match=r"Field 'order_line_item.line_status' \('L_LINESTATUS'\): column L_LINESTATUS"):
        convert(specs_dir / "AutoOntologyTPCH.yaml", table_provider=without_line_status)


@pytest.mark.parametrize("expression, message", [
    ("UPPER(a)", r"function UPPER is not supported"),
    ("CASE WHEN a = 1 THEN 'x' END", r"'CASE' is not supported"),
    ("(a = 1) + 1", r"condition \(a = 1\) is only supported as the whole expression"),
])
def test_unsupported_sql_fails_naming_the_field(tmp_path: Path, expression: str, message: str):
    spec = _single_field_spec(tmp_path, expression, "String", columns={"a": "Integer"})
    with pytest.raises(ValueError, match=rf"Field 'ITEMS.flag'.*{message}"):
        convert(spec)


def test_two_fields_reading_one_column_as_different_types_are_rejected(tmp_path: Path):
    spec = _single_field_spec(tmp_path, "a * 2", "Integer", columns={"a": "Integer", "a_text": ("A", "String")})
    with pytest.raises(ValueError, match=r"Fields 'ITEMS.a' and 'ITEMS.a_text' both read column A, as 'Integer' and 'String'"):
        convert(spec)


def test_a_condition_cannot_identify_an_entity(tmp_path: Path):
    spec = _single_field_spec(tmp_path, "a IS NULL", "Boolean", columns={"a": "Integer"}, identify_by_flag=True)
    with pytest.raises(ValueError, match=r"is read from a condition"):
        convert(spec)


def _single_field_spec(tmp_path: Path, expression: str, datatype: str, columns: dict,
                       identify_by_flag: bool = False) -> Path:
    """A spec whose Item has a `flag` read from *expression*, beside plain *columns*.

    A column is `name: datatype`, or `name: (COLUMN, datatype)` to read COLUMN
    under another name. Item is keyed by `nr`, or by `flag` itself when
    *identify_by_flag* is set.
    """
    import yaml

    def plain(name, spec):
        column, dtype = spec if isinstance(spec, tuple) else (name, spec)
        return {"name": name, "datatype": dtype,
                "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": column}]}}

    flag_type = "Boolean" if datatype == "Boolean" else datatype
    fields = [plain("nr", "Integer")] + [plain(n, s) for n, s in columns.items()] + [{
        "name": "flag", "datatype": datatype,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
    }]
    key = "flag" if identify_by_flag else "nr"
    document = {
        "version": "0.2.0.dev0",
        "name": "SingleField",
        "ontology": [{
            "concept": "Item", "type": "EntityType", "identify_by": [key],
            "relationships": [
                {"name": "nr", "roles": [{"concept": "Integer"}], "multiplicity": "OneToOne",
                 "verbalizes": ["{Item} has nr {Integer}"]},
                {"name": "flag", "roles": [{"concept": flag_type}],
                 "multiplicity": "OneToOne" if identify_by_flag else "ManyToOne",
                 "verbalizes": ["{Item} has flag {" + flag_type + "}"]},
            ],
        }],
        "ontology_mappings": [{
            "name": "single_field_mapping",
            "semantic_model": {
                "version": "0.2.0.dev0", "name": "Single field semantic model",
                "datasets": [{"name": "ITEMS", "source": "DB.SCHEMA.ITEMS", "fields": fields}],
            },
            "concept_mappings": [{
                "concept": "Item",
                "object_mappings": [{"referent_mappings": [{"relationship": key, "expression": f"ITEMS.{key}"}]}],
                # The schema gives `link_mappings` minItems 1, so it is left out when empty.
                **({} if identify_by_flag else {"link_mappings": [{
                    "object_mapping": {"referent_mappings": [{"relationship": "nr", "expression": "ITEMS.nr"}]},
                    "children": [{"object_mapping": {"expression": "ITEMS.flag"}, "relationship": "flag"}],
                }]}),
            }],
        }],
    }
    path = tmp_path / "single_field.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path
