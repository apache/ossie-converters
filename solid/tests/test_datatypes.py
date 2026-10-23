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

"""Warehouse type vocabulary: dialect inference and datatype mapping."""

import pytest
import yaml
from conftest import convert_quietly, field_of, fixture, model_of, stash_of

from ossie_solid import ConversionError, convert_solid_to_ossie
from ossie_solid._common import SUPPORTED_DIALECTS
from ossie_solid.datatypes import (
    OSSIE_DATATYPES,
    TYPE_ROUND_TRIP_EXCEPTIONS,
    infer_dialect,
    is_solid_fact_type,
    normalize_dialect,
    parse_type,
    to_ossie_datatype,
    to_raw_type,
)


@pytest.mark.parametrize(
    ("raw", "base", "params"),
    [
        ("NUMBER(38,0)", "NUMBER", ["38", "0"]),
        ("number(7, 2)", "NUMBER", ["7", "2"]),
        ("TEXT", "TEXT", []),
        ("VARCHAR(16777216)", "VARCHAR", ["16777216"]),
        ("ARRAY<STRING>", "ARRAY", []),
        ("MAP<STRING, STRING>", "MAP", []),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE", []),
        ("", None, []),
        (None, None, []),
    ],
)
def test_a_raw_type_splits_into_a_base_name_and_parameters(raw, base, params):
    assert parse_type(raw) == (base, params)


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        (["NUMBER(38,0)", "TEXT", "TIMESTAMP_NTZ"], "SNOWFLAKE"),
        (["LONG", "STRING", "MAP<STRING, STRING>"], "DATABRICKS"),
        (["INT64", "STRING", "BOOL", "FLOAT64"], "BIGQUERY"),
        # A STRUCT column decides nothing on its own, but must not stop the
        # surrounding vocabulary from deciding.
        (["STRUCT<a: INT>", "LONG", "STRING"], "DATABRICKS"),
        (["STRUCT<a: INT64>", "RECORD", "INT64"], "BIGQUERY"),
        # Names every warehouse shares carry no signal.
        (["STRING", "DATE", "BOOLEAN", "DECIMAL"], "ANSI_SQL"),
        ([], "ANSI_SQL"),
        # A tie between two vocabularies is not a decision.
        (["NUMBER", "INT64"], "ANSI_SQL"),
        # GEOGRAPHY is BigQuery's too, and VARIANT Databricks', so neither says Snowflake.
        (["STRING", "GEOGRAPHY", "DATE"], "ANSI_SQL"),
        (["STRING", "VARIANT"], "ANSI_SQL"),
        (["BIGDECIMAL", "STRING"], "BIGQUERY"),
        # Oracle and SQL Server are Solid sources with no Apache Ossie dialect. Oracle's
        # NUMBER would otherwise vote Snowflake, and SQL Server's BIGINT Databricks.
        (["NUMBER", "VARCHAR2", "DATE"], "ANSI_SQL"),
        (["BIGINT", "NVARCHAR", "DATETIME2"], "ANSI_SQL"),
    ],
)
def test_the_warehouse_is_inferred_from_its_distinctive_type_names(types, expected):
    dialect, confident = infer_dialect(types)
    assert dialect == expected
    assert confident is (expected != "ANSI_SQL")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tpcds_solid.yaml", "SNOWFLAKE"),
        ("databricks_solid.yaml", "DATABRICKS"),
        ("bigquery_solid.yaml", "BIGQUERY"),
    ],
)
def test_each_fixture_resolves_to_its_own_warehouse(name, expected):
    ossie, _ = convert_quietly(convert_solid_to_ossie, fixture(name))
    assert stash_of(model_of(ossie))["dialect"] == expected


@pytest.mark.parametrize("struct", ["STRUCT<a: INT, b: STRING>", "STRUCT"])
def test_a_struct_column_alone_does_not_identify_a_warehouse(struct):
    """STRUCT is Databricks and BigQuery both, and the angle-bracket params that
    would tell them apart are stripped by `parse_type` before the vote is counted --
    so it belongs to neither marker set. Regression: it was once listed as
    `STRUCT<>` under Databricks, a string `parse_type` can never return, which left
    BigQuery holding the only STRUCT marker and a Databricks model claiming BIGQUERY.
    """
    assert infer_dialect([struct, "STRING", "DATE"]) == ("ANSI_SQL", False)


def test_an_unrecognizable_vocabulary_falls_back_to_ansi_with_a_warning():
    solid = yaml.safe_load(fixture("databricks_solid.yaml"))
    for table in solid["semantic_model"]["tables"]:
        for column in table["dimensions"] + table["facts"]:
            column["type"] = "STRING"
    ossie, warnings_ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))
    assert any("could not infer the source warehouse" in w for w in warnings_)
    assert stash_of(model_of(ossie))["dialect"] == "ANSI_SQL"


def test_an_oracle_model_falls_back_to_ansi_naming_why():
    solid = yaml.safe_load(fixture("databricks_solid.yaml"))
    for table in solid["semantic_model"]["tables"]:
        for column in table["dimensions"] + table["facts"]:
            column["type"] = "NUMBER" if column["name"].endswith("_id") else "VARCHAR2(30)"
    ossie, warnings_ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))
    assert any("(VARCHAR2) are from a warehouse with no Apache Ossie dialect" in w
               for w in warnings_)
    assert stash_of(model_of(ossie))["dialect"] == "ANSI_SQL"


def test_an_explicit_dialect_suppresses_inference():
    ossie, warnings_ = convert_quietly(
        convert_solid_to_ossie, fixture("databricks_solid.yaml"), dialect="ANSI_SQL"
    )
    assert stash_of(model_of(ossie))["dialect"] == "ANSI_SQL"
    assert not any("could not infer" in w for w in warnings_)


def test_an_unsupported_dialect_is_rejected():
    with pytest.raises(ConversionError, match="Unsupported dialect"):
        convert_solid_to_ossie(fixture("databricks_solid.yaml"), dialect="POSTGRES")


def test_dialect_names_are_normalized_case_insensitively():
    assert normalize_dialect("snowflake") == "SNOWFLAKE"


@pytest.mark.parametrize(
    ("raw", "dialect", "expected"),
    [
        # A declared scale of zero is an integer, which is how Snowflake stores ids.
        ("NUMBER(38,0)", "SNOWFLAKE", "Integer"),
        ("NUMBER(7,2)", "SNOWFLAKE", "Decimal"),
        ("NUMERIC(10,4)", "BIGQUERY", "Decimal"),
        ("BIGNUMERIC(40,0)", "BIGQUERY", "Integer"),
        # A precision with no scale declares a scale of 0, in every warehouse.
        ("NUMERIC(10)", "BIGQUERY", "Integer"),
        ("NUMBER(10)", "SNOWFLAKE", "Integer"),
        ("TEXT", "SNOWFLAKE", "String"),
        ("STRING", "DATABRICKS", "String"),
        ("LONG", "DATABRICKS", "Integer"),
        ("INT64", "BIGQUERY", "Integer"),
        ("FLOAT64", "BIGQUERY", "Float"),
        ("DOUBLE", "DATABRICKS", "Float"),
        ("BOOL", "BIGQUERY", "Boolean"),
        ("BOOLEAN", "SNOWFLAKE", "Boolean"),
        ("DATE", "SNOWFLAKE", "Date"),
        # TIMESTAMP means different things per warehouse.
        ("TIMESTAMP", "SNOWFLAKE", "DateTime"),
        ("TIMESTAMP", "DATABRICKS", "DateTimeTz"),
        ("TIMESTAMP", "BIGQUERY", "DateTimeTz"),
        ("TIMESTAMP_NTZ", "SNOWFLAKE", "DateTime"),
        ("TIMESTAMP_NTZ", "DATABRICKS", "DateTime"),
        ("TIMESTAMP_TZ", "SNOWFLAKE", "DateTimeTz"),
        ("DATETIME", "BIGQUERY", "DateTime"),
        # Known, but outside the portable vocabulary.
        ("VARIANT", "SNOWFLAKE", "Opaque"),
        ("MAP<STRING, STRING>", "DATABRICKS", "Opaque"),
        ("ARRAY", "SNOWFLAKE", "Opaque"),
        ("INTERVAL DAY TO SECOND", "DATABRICKS", "Opaque"),
        ("INTERVAL YEAR TO MONTH", "BIGQUERY", "Opaque"),
        ("VECTOR(FLOAT, 256)", "SNOWFLAKE", "Opaque"),
        ("RANGE<DATE>", "BIGQUERY", "Opaque"),
        ("CHARACTER VARYING(10)", "ANSI_SQL", "String"),
        # Oracle and SQL Server, read as ANSI_SQL.
        ("VARCHAR2(100)", "ANSI_SQL", "String"),
        ("DATETIME2", "ANSI_SQL", "DateTime"),
        ("DATETIMEOFFSET", "ANSI_SQL", "DateTimeTz"),
        ("MONEY", "ANSI_SQL", "Decimal"),
        # Unknown: the spec says to omit datatype rather than guess.
        ("SOMETHING_ELSE", "SNOWFLAKE", None),
        ("", "SNOWFLAKE", None),
        (None, "SNOWFLAKE", None),
    ],
)
def test_warehouse_types_map_onto_the_portable_datatypes(raw, dialect, expected):
    assert to_ossie_datatype(raw, dialect) == expected


# The exact-decimal names each warehouse reports, bare.
BARE_DECIMAL_CASES = [
    ("SNOWFLAKE", "NUMBER"), ("SNOWFLAKE", "NUMERIC"), ("SNOWFLAKE", "DECIMAL"),
    ("DATABRICKS", "DECIMAL"), ("DATABRICKS", "DEC"), ("DATABRICKS", "NUMERIC"),
    ("BIGQUERY", "NUMERIC"), ("BIGQUERY", "DECIMAL"), ("BIGQUERY", "BIGNUMERIC"),
    ("BIGQUERY", "BIGDECIMAL"),
    # Oracle's bare NUMBER, read as ANSI_SQL, is a floating decimal.
    ("ANSI_SQL", "NUMBER"), ("ANSI_SQL", "NUMERIC"), ("ANSI_SQL", "DECIMAL"),
]


@pytest.mark.parametrize(("dialect", "raw"), BARE_DECIMAL_CASES)
def test_a_bare_decimal_type_is_a_decimal_in_every_dialect(dialect, raw):
    """A bare name says nothing about the scale, which is what the spec's Decimal
    means. Snowflake's own default is (38,0), but Solid copies the catalog's DATA_TYPE,
    which never carries the scale on Snowflake or Databricks: every NUMBER and DECIMAL
    column in the real Solid exports this was checked against is bare, rates and dollar
    amounts included, so reading them as Integer would type money as whole numbers."""
    assert to_ossie_datatype(raw, dialect) == "Decimal"


@pytest.mark.parametrize(("dialect", "raw"), BARE_DECIMAL_CASES)
def test_a_declared_scale_of_zero_is_an_integer_in_every_dialect(dialect, raw):
    assert to_ossie_datatype(f"{raw}(38,0)", dialect) == "Integer"
    assert to_ossie_datatype(f"{raw}(18)", dialect) == "Integer"
    assert to_ossie_datatype(f"{raw}(38,2)", dialect) == "Decimal"


@pytest.mark.parametrize("dialect", SUPPORTED_DIALECTS)
@pytest.mark.parametrize("datatype", sorted(OSSIE_DATATYPES))
def test_every_datatype_survives_the_type_written_for_it(dialect, datatype):
    """An export with no stash writes a warehouse type for each datatype, and Solid
    reads that back as the column's catalog type. It has to mean the same datatype, or
    a re-import changes the model: a money column written as a bare DECIMAL comes back
    as an Integer. The exceptions are types the warehouse does not have."""
    raw = to_raw_type(datatype, dialect)
    expected = TYPE_ROUND_TRIP_EXCEPTIONS.get((dialect, datatype), datatype)
    assert to_ossie_datatype(raw, dialect) == expected, raw


def test_an_unmappable_type_omits_the_datatype_and_warns():
    solid = yaml.safe_load(fixture("databricks_solid.yaml"))
    solid["semantic_model"]["tables"][0]["dimensions"][2]["type"] = "SOMETHING_ELSE"
    ossie, warnings_ = convert_quietly(convert_solid_to_ossie, yaml.safe_dump(solid))
    assert any("SOMETHING_ELSE" in w for w in warnings_)
    field = field_of(model_of(ossie), "orders", "status")
    assert "datatype" not in field
    # The raw name is still kept, so the export is unaffected.
    assert stash_of(field)["type"] == "SOMETHING_ELSE"


@pytest.mark.parametrize(
    ("datatype", "dialect", "expected"),
    [
        ("String", "SNOWFLAKE", "TEXT"),
        ("String", "DATABRICKS", "STRING"),
        ("String", "BIGQUERY", "STRING"),
        ("String", "ANSI_SQL", "VARCHAR"),
        ("Integer", "BIGQUERY", "INT64"),
        # Scaled, because a bare NUMBER or DECIMAL is an integer type.
        ("Integer", "SNOWFLAKE", "NUMBER(38,0)"),
        ("Decimal", "SNOWFLAKE", "NUMBER(38,9)"),
        ("Decimal", "DATABRICKS", "DECIMAL(38,9)"),
        ("Decimal", "BIGQUERY", "NUMERIC"),
        ("Decimal", "ANSI_SQL", "DECIMAL(38,9)"),
        ("Float", "DATABRICKS", "DOUBLE"),
        ("DateTimeTz", "SNOWFLAKE", "TIMESTAMP_TZ"),
        (None, "SNOWFLAKE", None),
    ],
)
def test_portable_datatypes_map_back_to_a_representative_warehouse_type(
    datatype, dialect, expected
):
    assert to_raw_type(datatype, dialect) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NUMBER(38,0)", True),
        ("DECIMAL(10,2)", True),
        ("DOUBLE", True),
        ("INT64", True),
        ("LONG", True),
        ("TEXT", False),
        ("STRING", False),
        ("DATE", False),
        ("BOOLEAN", False),
        (None, False),
        # solid-server matches with `startswith`, so these land where Solid puts them,
        # which is not where their names suggest. Mirrored, not corrected: the point is
        # to put a column in the list Solid itself would.
        ("BIGINT", False),
        ("SMALLINT", False),
        ("BIGNUMERIC", False),
        ("INTERVAL", True),
    ],
)
def test_solids_own_fact_type_rule_is_mirrored(raw, expected):
    assert is_solid_fact_type(raw) is expected
