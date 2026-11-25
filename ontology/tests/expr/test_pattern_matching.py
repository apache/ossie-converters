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


"""Pattern matching in ontology formulas (core spec, expression_language.md).

Tests LIKE, ILIKE and REGEXP_LIKE parsing and ontology constraint loading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ossie_ontology.model import OntologyComponent
from ossie_ontology.expr.model import Expression
from ossie_ontology.expr.formula.model import (
    Formula,
    VarHandle,
    LiteralHandle,
    Conjunction,
    Disjunction,
    Negation,
)
from ossie_ontology.expr.formula.parser import FormulaParser
from ossie_ontology.parser import OssieParser


@pytest.mark.parametrize(
    "formula, operator",
    [
        ("Code LIKE 'ABC%'", "LIKE"),
        ("Code ILIKE 'abc%'", "ILIKE"),
        ("REGEXP_LIKE(Code, '^[A-Z]+$')", "REGEXP_LIKE"),
    ],
)
def test_pattern_matching_parses(formula, operator):
    parser = FormulaParser(OntologyComponent())
    parsed = parser.parse_formula(formula)

    assert isinstance(parsed, Expression)
    assert parsed.op().value == operator
    assert len(parsed.args()) == 2

    left, right = parsed.args()

    assert isinstance(left, VarHandle)
    assert left.name() == "Code"
    assert isinstance(right, LiteralHandle)
    assert right._key() == formula.split("'")[1]


@pytest.mark.parametrize(
    "formula",
    [
        "CancelationCode LIKE 'A%'",
        "CancelationCode ILIKE 'a%'",
        "REGEXP_LIKE(CancelationCode, '^[A-D]$')",
    ],
)
def test_ontology_with_pattern_constraint_loads(
    flights_path: Path, tmp_path: Path, formula: str
):
    text = flights_path.read_text(encoding="utf-8")

    original = (
        "requires: [ CancelationCode == 'A' OR "
        "CancelationCode == 'B' OR "
        "CancelationCode == 'C' OR "
        "CancelationCode == 'D' ]"
    )

    assert original in text

    path = tmp_path / "flights.yaml"
    path.write_text(
        text.replace(original, f'requires: [ "{formula}" ]'),
        encoding="utf-8",
    )

    model = OssieParser().parse(path)
    (constraint,) = model.ontology.lookup_concept(
        "CancelationCode"
    ).requires

    assert isinstance(constraint, Formula)
    assert constraint.raw_expr == formula


def test_pattern_matching_logical_composition():
    parser = FormulaParser(OntologyComponent())
    parsed = parser.parse_formula(
        "Code LIKE 'A%' OR Code ILIKE 'b%' "
        "AND NOT REGEXP_LIKE(Code, '^C')"
    )

    assert isinstance(parsed, Disjunction)
    assert parsed.left().op().value == "LIKE"

    right = parsed.right()
    assert isinstance(right, Conjunction)
    assert right.left().op().value == "ILIKE"

    negation = right.right()
    assert isinstance(negation, Negation)
    assert negation.arg().op().value == "REGEXP_LIKE"


@pytest.mark.parametrize(
    "formula",
    [
        "Code LIKE",
        "Code ILIKE",
        "REGEXP_LIKE(Code)",
        "REGEXP_LIKE(Code, 'A', 'B')",
        "REGEXP_LIKE()",
    ],
)
def test_invalid_pattern_matching_syntax(formula):
    parser = FormulaParser(OntologyComponent())

    with pytest.raises(ValueError):
        parser.parse_formula(formula)
