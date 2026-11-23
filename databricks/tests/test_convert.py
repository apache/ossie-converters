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

import yaml
from conftest import assert_golden, fixture, warnings_of

from ossie_databricks import convert_ossie_model_from_databricks, convert_ossie_model_to_databricks


def test_to_databricks_is_a_no_op():
    for name in ("retail.ossie.yaml", "databricks_first.ossie.yaml", "sources.ossie.yaml"):
        result = convert_ossie_model_to_databricks(fixture(name))
        assert result.value == fixture(name)
        assert result.warnings == []
    model = yaml.safe_load(fixture("retail.ossie.yaml"))
    assert yaml.safe_load(convert_ossie_model_to_databricks(model).value) == model


def test_from_databricks_fills_portable_expressions():
    result = convert_ossie_model_from_databricks(fixture("databricks_first.ossie.yaml"))
    model = yaml.safe_load(result.value)
    assert_golden("from_databricks", {"model": model, "warnings": warnings_of(result)})


def _filled(model):
    return yaml.safe_load(convert_ossie_model_from_databricks(model).value)


def _owners(model):
    owners = list(model.get("metrics") or [])
    for dataset in model["datasets"]:
        owners += [*dataset.get("fields", []), *dataset.get("metrics", [])]
    return owners


def _forms(expression):
    """Each (dialect, text) of an expression, with `sql` read as OSSIE_SQL_2026."""
    forms = {(entry["dialect"], entry["expression"]) for entry in expression.get("dialects", [])}
    return forms | ({("OSSIE_SQL_2026", expression["sql"])} if "sql" in expression else set())


def test_from_databricks_loses_nothing():
    model = yaml.safe_load(fixture("databricks_first.ossie.yaml"))
    filled = _filled(model)
    for before, after in zip(_owners(model), _owners(filled), strict=True):
        assert _forms(before["expression"]) <= _forms(after["expression"])
        assert {k: v for k, v in after.items() if k != "expression"} == {
            k: v for k, v in before.items() if k != "expression"
        }


def test_ossie_dialect_entry_moves_into_sql():
    filled = _filled(fixture("databricks_first.ossie.yaml"))
    orders, customers = filled["datasets"]
    assert customers["fields"][1]["expression"] == {
        "sql": "UPPER(c_mktsegment)",
        "dialects": [{"dialect": "DATABRICKS", "expression": "upper(c_mktsegment)"}],
    }
    assert orders["fields"][4]["expression"] == {
        "sql": "o_totalprice",
        "dialects": [{"dialect": "DATABRICKS", "expression": "o_totalprice"}],
    }
    assert orders["fields"][5]["expression"] == {
        "sql": "o_orderstatus",
        "dialects": [{"dialect": "ANSI_SQL", "expression": "o_orderstatus"}],
    }


def test_sql_takes_precedence_over_an_ossie_dialect_entry():
    model = yaml.safe_load(fixture("sources.ossie.yaml"))
    expression = {
        "sql": "o_orderkey",
        "dialects": [
            {"dialect": "OSSIE_SQL_2026", "expression": "CAST(o_orderkey AS BIGINT)"},
            {"dialect": "DATABRICKS", "expression": "o_orderkey"},
        ],
    }
    model["datasets"][0]["fields"][0]["expression"] = expression
    filled = _filled(model)
    assert filled["datasets"][0]["fields"][0]["expression"] == expression


def test_from_databricks_keeps_a_portable_model():
    result = convert_ossie_model_from_databricks(fixture("sources.ossie.yaml"))
    assert yaml.safe_load(result.value) == yaml.safe_load(fixture("sources.ossie.yaml"))
    assert result.warnings == []
