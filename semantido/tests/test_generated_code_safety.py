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

"""Safety of the generated Python: identifier sanitization, repr-escaping,
class-name casing/collisions, a synthesized primary key, enum validation,
computed-expression reporting, and registry hardening."""

import json

from ossie import OssieDocument

from ossie_semantido.converter_issues import ConverterIssueType
from ossie_semantido.ossie_to_semantido import _emit_registry, ossie_to_semantido_source


def _expr(expression: str) -> dict:
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]}


def _field(name, datatype="Integer", expression=None, extension=None):
    field = {"name": name, "expression": expression or _expr(name)}
    if datatype is not None:
        field["datatype"] = datatype
    if extension is not None:
        field["custom_extensions"] = [
            {"vendor_name": "SEMANTIDO", "data": json.dumps(extension)}
        ]
    return field


def _doc(name, source, fields, primary_key=("id",)):
    dataset = {"name": name, "source": source, "fields": fields}
    if primary_key is not None:
        dataset["primary_key"] = list(primary_key)
    return OssieDocument.model_validate({"name": "m", "datasets": [dataset]})


def _issues_of(result, kind):
    return {i.element_name for i in result.issues if i.issue_type is kind}


def test_awkward_identifiers_are_sanitized_and_originals_preserved():
    document = _doc(
        "t",
        "t",
        [
            _field("order-id"),
            _field("class", datatype="String"),
            _field("2020", datatype="String"),
        ],
        primary_key=["order-id"],
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")

    # Hyphen -> underscore (PK), keyword gets a trailing underscore, a leading
    # digit is prefixed; the real column name is carried as the first argument.
    assert "order_id = Column('order-id', Integer, primary_key=True)" in result.output
    assert "class_ = Column('class', String)" in result.output
    assert "_2020 = Column('2020', String)" in result.output

    sanitized = _issues_of(result, ConverterIssueType.IDENTIFIER_SANITIZED)
    assert "t.order-id -> order_id" in sanitized
    assert "t.class -> class_" in sanitized


def test_foreign_key_value_is_repr_escaped():
    # A quote in the authored reference must not break or inject into the source.
    document = _doc(
        "t",
        "t",
        [
            _field("id"),
            _field("ref", extension={"references": 'evil".id'}),
        ],
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert 'ForeignKey(\'evil".id\')' in result.output


def test_class_name_preserves_casing_and_avoids_collision():
    document = OssieDocument.model_validate(
        {
            "name": "m",
            "datasets": [
                {
                    "name": n,
                    "source": n,
                    "primary_key": ["id"],
                    "fields": [_field("id")],
                }
                for n in ("LEI_data", "a_b", "A_b")
            ],
        }
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")

    assert "class LEIData(" in result.output  # tail casing kept, not "LeiData"
    assert "class AB(" in result.output
    assert "class AB_2(" in result.output  # collision disambiguated


def test_schema_is_derived_from_source():
    document = _doc("orders", "analytics.public.orders", [_field("id")])
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert "__tablename__ = 'orders'" in result.output
    assert "__table_args__ = ({\"schema\": 'public'},)" in result.output


def test_missing_primary_key_is_synthesized_and_reported():
    document = _doc(
        "t", "t", [_field("a"), _field("b", datatype="String")], primary_key=None
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert "a = Column(Integer, primary_key=True)" in result.output
    assert _issues_of(result, ConverterIssueType.MISSING_PRIMARY_KEY) == {"t:a"}


def test_unknown_privacy_level_reported_not_emitted():
    document = _doc(
        "t",
        "t",
        [
            _field("id"),
            _field(
                "secret",
                datatype="String",
                extension={"privacy_level": "TOP_SECRET", "data_type": "VARCHAR"},
            ),
        ],
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert "PrivacyLevel." not in result.output
    assert _issues_of(result, ConverterIssueType.UNKNOWN_PRIVACY_LEVEL) == {
        "t.secret:TOP_SECRET"
    }


def test_unknown_time_grain_reported_not_emitted():
    document = _doc(
        "t",
        "t",
        [
            _field("id"),
            _field(
                "ts",
                datatype="DateTime",
                extension={"time_grain": "FORTNIGHT", "data_type": "TIMESTAMP"},
            ),
        ],
    )
    result = ossie_to_semantido_source(document)
    compile(result.output, "<generated>", "exec")
    assert "TimeGrain." not in result.output
    assert _issues_of(result, ConverterIssueType.UNKNOWN_TIME_GRAIN) == {
        "t.ts:FORTNIGHT"
    }


def test_computed_expression_is_reported():
    document = _doc(
        "t",
        "t",
        [
            _field("id"),
            _field("email_lower", datatype="String", expression=_expr("LOWER(email)")),
        ],
    )
    result = ossie_to_semantido_source(document)
    assert _issues_of(result, ConverterIssueType.NON_ANSI_EXPRESSION_DROPPED) == {
        "t.email_lower"
    }


# --- registry hardening ----------------------------------------------------


def test_registry_unknown_relation_kind_reported():
    issues = []
    _emit_registry(
        {
            "concepts": {
                "a": {"definition": "A", "relations": [{"relation": "bogus", "concept": "b"}]},
                "b": {"definition": "B"},
            }
        },
        issues,
    )
    assert any(
        i.issue_type is ConverterIssueType.REGISTRY_RELATION_UNKNOWN_KIND for i in issues
    )


def test_registry_self_relation_reported():
    issues = []
    _emit_registry(
        {
            "concepts": {
                "a": {"definition": "A", "relations": [{"relation": "same_as", "concept": "a"}]}
            }
        },
        issues,
    )
    assert any(
        i.issue_type is ConverterIssueType.REGISTRY_RELATION_UNRESOLVED
        and i.element_name == "a:same_as:a"
        for i in issues
    )


def test_registry_unknown_mapping_helper_reported():
    issues = []
    _emit_registry(
        {
            "concepts": {
                "a": {
                    "definition": "A",
                    "mappings": [
                        {"relation": "board_match", "source": "x", "target": "y"}
                    ],
                }
            }
        },
        issues,
    )
    assert any(
        i.issue_type is ConverterIssueType.REGISTRY_MAPPING_UNKNOWN for i in issues
    )
