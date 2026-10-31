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

"""Portable `datatype` conformance (both directions) and metric dropping
at both the document and dataset scopes."""

from ossie import OssieDataType, OssieDocument

from ossie_semantido.converter_issues import ConverterIssueType
from ossie_semantido.ossie_to_semantido import ossie_to_semantido_source
from ossie_semantido.semantido_to_ossie import semantic_layer_to_ossie
from tests.helpers import load_emir_layer


def _expr(expression: str) -> dict:
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]}


def _doc(fields, dataset_metrics=None, model_metrics=None) -> OssieDocument:
    dataset = {"name": "t", "source": "s.t", "primary_key": ["id"], "fields": fields}
    if dataset_metrics:
        dataset["metrics"] = dataset_metrics
    document: dict = {"name": "m", "datasets": [dataset]}
    if model_metrics:
        document["metrics"] = model_metrics
    return OssieDocument.model_validate(document)


def _unmapped(result):
    return {
        i.element_name
        for i in result.issues
        if i.issue_type is ConverterIssueType.UNMAPPED_DATA_TYPE
    }


def test_forward_sets_portable_datatype():
    layer = load_emir_layer()
    document = semantic_layer_to_ossie(layer, model_name="emir_reporting").output
    trade = next(d for d in document.datasets if d.name == "trade")
    fields = {f.name: f for f in trade.fields}

    # The portable datatype is derived from the SQL type semantido reports.
    assert fields["asset_class"].datatype is OssieDataType.STRING
    assert fields["notional_amount"].datatype is OssieDataType.DECIMAL
    # A temporal SQL type yields a temporal datatype, so the time role is
    # recoverable even without the explicit dimension flag.
    assert fields["execution_timestamp"].datatype is OssieDataType.DATE_TIME
    assert fields["execution_timestamp"].is_time_dimension() is True


def test_reverse_uses_portable_datatype_without_extension():
    document = _doc(
        [
            {"name": "id", "expression": _expr("id"), "datatype": "Integer"},
            {"name": "amount", "expression": _expr("amount"), "datatype": "Decimal"},
        ]
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")

    assert "id = Column(Integer, primary_key=True)" in result.output
    assert "amount = Column(Numeric)" in result.output
    # A portable datatype is a clean mapping, not a loss.
    assert not _unmapped(result)


def test_reverse_reports_field_with_no_type():
    document = _doc(
        [
            {"name": "id", "expression": _expr("id"), "datatype": "Integer"},
            {"name": "mystery", "expression": _expr("mystery")},
        ]
    )
    result = ossie_to_semantido_source(document)

    assert "mystery = Column(String)" in result.output
    assert _unmapped(result) == {"mystery"}


def test_metrics_dropped_at_both_scopes():
    document = _doc(
        [{"name": "id", "expression": _expr("id"), "datatype": "Integer"}],
        dataset_metrics=[{"name": "row_count", "expression": _expr("COUNT(*)")}],
        model_metrics=[{"name": "grand_total", "expression": _expr("SUM(amount)")}],
    )
    result = ossie_to_semantido_source(document)

    dropped = {
        i.element_name
        for i in result.issues
        if i.issue_type is ConverterIssueType.METRIC_DROPPED
    }
    assert dropped == {"grand_total", "t.row_count"}
