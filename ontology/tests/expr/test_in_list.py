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


"""`IN` / `NOT IN` over a list of values (core spec, expression_language.md).

`FormulaParser` desugars `x IN (a, b)` to `x == a OR x == b`, and
`x NOT IN (a, b)` to `NOT (x == a OR x == b)`, so every consumer of the AST
already understands the result. The disjunction is built as a balanced tree,
so a long list stays shallow enough for the recursive visitors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ossie_ontology.model import Concept, ConceptType, OntologyComponent
from ossie_ontology.expr.model import Expression
from ossie_ontology.expr.formula.model import (
    ConceptRefHandle,
    Conjunction,
    Disjunction,
    Formula,
    LiteralHandle,
    Negation,
    VarHandle,
)
from ossie_ontology.expr.formula.parser import FormulaParser
from ossie_ontology.expr.formula.visitor.collector import FormulaCollector
from ossie_ontology.parser import OssieParser

_CODE = Concept(name="Code", type=ConceptType.VALUE_TYPE)
HEAD_VARS: dict[VarHandle | ConceptRefHandle, Concept] = {
    VarHandle(name): _CODE for name in ("x", "y")
}


def parse(formula: str):
    return FormulaParser(OntologyComponent()).parse_formula(formula, head_vars=HEAD_VARS)


def tree(node):
    """Render an AST as nested tuples. A literal is its `repr`, so `1`, `'1'` and `TRUE` differ."""
    if isinstance(node, Disjunction):
        return ("OR", tree(node.left()), tree(node.right()))
    if isinstance(node, Conjunction):
        return ("AND", tree(node.left()), tree(node.right()))
    if isinstance(node, Negation):
        return ("NOT", tree(node.arg()))
    if isinstance(node, Expression):
        return (node.op().value, *(tree(arg) for arg in node.args()))
    if isinstance(node, LiteralHandle):
        return repr(node._key())
    assert isinstance(node, VarHandle), node
    return str(node)


def eq(left, value):
    return ("==", left, repr(value))


@pytest.mark.parametrize(
    "formula, expected",
    [
        ("x IN ('A')", eq("x", "A")),
        ("x IN ('A', 'B')", ("OR", eq("x", "A"), eq("x", "B"))),
        ("x IN ('A', 'B', 'C')", ("OR", ("OR", eq("x", "A"), eq("x", "B")), eq("x", "C"))),
        ("x IN (1, 2.5)", ("OR", eq("x", 1), eq("x", 2.5))),
        ("x IN (TRUE, FALSE)", ("OR", eq("x", True), eq("x", False))),
        ("x NOT IN ('A')", ("NOT", eq("x", "A"))),
        ("x NOT IN ('A', 'B')", ("NOT", ("OR", eq("x", "A"), eq("x", "B")))),
        ("NOT x IN ('A')", ("NOT", eq("x", "A"))),
        ("x + 1 IN (2, 3)", ("OR", eq(("+", "x", "1"), 2), eq(("+", "x", "1"), 3))),
        (
            "x IN ('A', 'B') AND y == 'C'",
            ("AND", ("OR", eq("x", "A"), eq("x", "B")), eq("y", "C")),
        ),
        ("y == 'C' OR x IN ('A')", ("OR", eq("y", "C"), eq("x", "A"))),
    ],
)
def test_in_list_desugars_to_equalities(formula: str, expected):
    assert tree(parse(formula)) == expected


def test_in_list_matches_the_hand_written_disjunction():
    """Up to three values, the tree is exactly what the `OR` form parses to."""
    assert tree(parse("x IN ('A', 'B', 'C')")) == tree(parse("x == 'A' OR x == 'B' OR x == 'C'"))


@pytest.mark.parametrize(
    "formula",
    [
        "x IN ()",
        "x IN ('A',)",
        "x IN 'A'",
        "x IN (y)",
        "x IN ('A', y)",
        "x IN (1 + 2)",
        "x IN (-3)",
        "x NOT ('A')",
    ],
)
def test_in_list_accepts_only_a_list_of_values(formula: str):
    with pytest.raises(ValueError, match="Unable to parse"):
        parse(formula)


def depth(node) -> int:
    if isinstance(node, Disjunction):
        return 1 + max(depth(node.left()), depth(node.right()))
    return 0


def test_long_in_list_stays_shallow():
    """A chain of 2000 `OR`s overflows the recursive visitors; a balanced tree does not."""
    values = ", ".join(f"'v{i}'" for i in range(2000))
    parsed = parse(f"x IN ({values})")
    assert depth(parsed) == 11  # ceil(log2(2000))
    parsed.accept(FormulaCollector(HEAD_VARS))


def test_ontology_with_an_in_constraint_loads(flights_path: Path, tmp_path: Path):
    """The flights `CancelationCode` constraint, written with `IN`, parses and validates."""
    original = (
        "requires: [ CancelationCode == 'A' OR CancelationCode == 'B' "
        "OR CancelationCode == 'C' OR CancelationCode == 'D' ]"
    )
    rewritten = "requires: [ \"CancelationCode IN ('A', 'B', 'C', 'D')\" ]"
    text = flights_path.read_text(encoding="utf-8")
    assert original in text, "fixture changed: CancelationCode constraint not found"
    path = tmp_path / "flights.yaml"
    path.write_text(text.replace(original, rewritten), encoding="utf-8")

    model = OssieParser().parse(path)

    (formula,) = model.ontology.lookup_concept("CancelationCode").requires
    assert isinstance(formula, Formula)
    assert formula.raw_expr == "CancelationCode IN ('A', 'B', 'C', 'D')"
