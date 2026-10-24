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

"""Converter stderr protocols must retain whole issues without double counting."""

import json

import pytest

from interop.execution import diagnostics, has_degradation


def decode(tmp_path, converter, text):
    stderr = tmp_path / "stderr.txt"
    stderr.write_text(text)
    return diagnostics({"stderr": str(stderr)}, converter)


def test_microsoft_scoped_losses_are_complete_and_summary_is_not_an_issue(tmp_path):
    messages = [
        "[dataset 'orders'] Power BI has no composite key; primary key (id, line_id) is not marked on the table",
        "[dataset 'orders' field 'id'] custom_extensions for vendor 'GOODDATA' have no Power BI equivalent and are dropped",
    ]
    issues = decode(
        tmp_path,
        "microsoft",
        "\n".join(messages)
        + "\n2 construct(s) could not be converted faithfully; see the messages above.\n",
    )
    assert [issue["message"] for issue in issues] == messages
    assert issues[0]["scope"] == "dataset 'orders'"
    assert issues[0]["severity"] == "WARNING"
    assert issues[1]["foreign_extension"]
    assert has_degradation(issues)


def test_orionbelt_warning_blocks_retain_each_warning_and_continuations(tmp_path):
    issues = decode(
        tmp_path,
        "orionbelt",
        """Written to model.yaml

Conversion warnings:
  - Relationship 'orders_to_users': no type specified, defaulting to many-to-one.
  - LOSSY: metric 'revenue' has no OBML representation;
    preserved verbatim but not queryable in OBML.
    - this is continuation text, not another issue
  - custom_extensions for foreign vendor 'CUBE' are dropped.

Validating OBML output...
  JSON Schema: valid
  Warnings:    1
    - orionbelt package not installed — skipping semantic validation
OBML output is valid
""",
    )
    assert len(issues) == 4
    assert issues[0]["message"].startswith("Relationship 'orders_to_users'")
    assert (
        issues[1]["message"]
        == "LOSSY: metric 'revenue' has no OBML representation;\npreserved verbatim but not queryable in OBML.\n- this is continuation text, not another issue"
    )
    assert issues[2]["foreign_extension"]
    assert (
        issues[3]["message"]
        == "orionbelt package not installed — skipping semantic validation"
    )
    assert all(issue["severity"] == "WARNING" for issue in issues)


def test_thoughtspot_json_issues_keep_code_severity_scope_remedy_and_extra_fields(
    tmp_path,
):
    raw = [
        {
            "code": "TS-DATASET-CONNECTION-MISSING",
            "severity": "WARNING",
            "message": "no connection name is available;\nimport will fail until one is added",
            "object_ref": "dataset:orders",
            "remedy": "Set connection.name",
            "extra": {"native": True},
        },
        {
            "code": "TS-MODEL-UNSUPPORTED",
            "severity": "ERROR",
            "message": "unsupported expression",
            "object_ref": "metric:revenue",
            "remedy": None,
        },
        {
            "code": "TS-RENDERED",
            "severity": "INFO",
            "message": "expression rendered",
            "object_ref": "metric:count",
            "remedy": None,
        },
    ]
    issues = decode(tmp_path, "thoughtspot", json.dumps(raw, indent=2))
    assert len(issues) == 3
    for actual, expected in zip(issues, raw):
        assert all(actual[key] == value for key, value in expected.items())
    assert has_degradation(issues)
    assert not has_degradation(issues[-1:])


def test_thoughtspot_json_does_not_hide_surrounding_runtime_warnings(tmp_path):
    raw = [{"code": "TS-LOSS", "severity": "WARNING", "message": "metric dropped"}]
    text = (
        "runtime.py:1: UserWarning: prefix\n"
        + json.dumps(raw)
        + "\n[WARNING] suffix\n[]\n"
    )
    issues = decode(tmp_path, "thoughtspot", text)
    assert len(issues) == 3
    assert issues[0]["message"] == "metric dropped"
    assert any("prefix" in issue["message"] for issue in issues)
    assert any("suffix" in issue["message"] for issue in issues)


@pytest.mark.parametrize("converter", ["microsoft", "orionbelt", "thoughtspot"])
def test_converter_decoders_leave_generic_python_warnings_available(
    tmp_path, converter
):
    issues = decode(
        tmp_path,
        converter,
        "runtime.py:1: UserWarning: ignored unknown custom_extensions\n  warnings.warn('ignored unknown custom_extensions')\n",
    )
    assert len(issues) == 1
    assert issues[0]["foreign_extension"]


def test_invalid_json_still_preserves_text_warning(tmp_path):
    issues = decode(
        tmp_path, "thoughtspot", "[broken\n[WARNING] conversion dropped a field\n"
    )
    assert len(issues) == 1 and has_degradation(issues)
