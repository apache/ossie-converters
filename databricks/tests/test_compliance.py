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

"""The portability ladder of apache/ossie (compliance/portability), scored by its grader."""

import copy
import importlib.util
import sys

import pytest
import yaml
from conftest import ossie_root

from ossie_databricks import convert_ossie_model_from_databricks, convert_ossie_model_to_databricks
from ossie_databricks.expressions import DATABRICKS, parse, translate
from ossie_databricks.metric_view import portable_text

LADDER = sorted((ossie_root() / "compliance" / "portability").glob("m*/*.ossie.yaml"))


def _grade(model, returned, warnings):
    path = ossie_root() / "compliance" / "portability" / "grader" / "grade.py"
    spec = importlib.util.spec_from_file_location("ossie_grade", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    diagnostics = "\n".join(str(warning) for warning in warnings)
    return module.grade(model, returned, diagnostics, "databricks")


def _databricks_only(model):
    """The model with each expression written only as a DATABRICKS entry."""
    model = copy.deepcopy(model)
    owners = [metric for metric in model.get("metrics") or []]
    for dataset in model["datasets"]:
        owners += [*dataset.get("fields", []), *dataset.get("metrics", [])]
    for owner in owners:
        text, dialect = portable_text(owner["expression"])
        databricks = translate(parse(text, dialect), DATABRICKS)
        owner["expression"] = {"dialects": [{"dialect": DATABRICKS, "expression": databricks}]}
    return model


@pytest.mark.parametrize("path", LADDER, ids=lambda path: path.name)
def test_round_trip_is_portable(path):
    model = yaml.safe_load(path.read_text())
    native = convert_ossie_model_to_databricks(model)
    back = convert_ossie_model_from_databricks(native.value)

    report = _grade(model, yaml.safe_load(back.value), native.warnings + back.warnings)

    assert {finding.outcome for finding in report.findings} == {"PORTABLE"}


@pytest.mark.parametrize("path", LADDER, ids=lambda path: path.name)
def test_databricks_only_model_becomes_portable(path):
    model = yaml.safe_load(path.read_text())
    back = convert_ossie_model_from_databricks(_databricks_only(model))

    report = _grade(model, yaml.safe_load(back.value), back.warnings)

    for finding in report.findings:
        # An ANSI_SQL entry returns as the `sql` slot (RELABELED); a REWRITTEN is cosmetic.
        assert finding.outcome in ("PORTABLE", "RELABELED", "REWRITTEN"), finding
        assert finding.outcome != "REWRITTEN" or finding.detail.startswith("cosmetic"), finding
