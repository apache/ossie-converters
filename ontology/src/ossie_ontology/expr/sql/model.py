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

"""SQL expression AST: what a dataset field's `expression` says it reads.

A field's expression is scalar SQL over one row of its dataset -- most often a
bare column (`L_LINESTATUS`), sometimes a computation over several
(`"L_LINESTATUS" IS NULL`, `c_first_name || ' ' || c_last_name`). These nodes
are vendor-neutral: they record what the SQL says, and leave it to a target
(the RelationalAI converter, for one) to decide what that means there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator

from ossie_ontology.expr import acceptor, Node


class SqlOp(str, Enum):
    """The binary operators of the scalar SQL subset."""
    # arithmetic
    PLUS = "+"
    MINUS = "-"
    TIMES = "*"
    DIVIDE = "/"
    MODULO = "%"
    # string
    CONCAT = "||"
    # comparison
    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="

    @property
    def is_comparison(self) -> bool:
        return self in _COMPARISON_OPS

    def negated(self) -> SqlOp:
        """The comparison that holds exactly when this one is false.

        Both need their operands to be present, which is SQL's three-valued
        logic: a comparison against NULL is neither true nor false.
        """
        return _NEGATED[self]


_COMPARISON_OPS = frozenset({SqlOp.EQ, SqlOp.NE, SqlOp.LT, SqlOp.LE, SqlOp.GT, SqlOp.GE})
_NEGATED = {
    SqlOp.EQ: SqlOp.NE, SqlOp.NE: SqlOp.EQ,
    SqlOp.LT: SqlOp.GE, SqlOp.GE: SqlOp.LT,
    SqlOp.GT: SqlOp.LE, SqlOp.LE: SqlOp.GT,
}


class SqlNode(Node):
    """A node of a SQL expression tree."""

    def children(self) -> tuple[SqlNode, ...]:
        return ()

    def columns(self) -> list[Column]:
        """Every column the expression reads, in order of first appearance."""
        seen: dict[str, Column] = {}
        for node in self._walk():
            if isinstance(node, Column) and node.stored_name not in seen:
                seen[node.stored_name] = node
        return list(seen.values())

    @property
    def is_predicate(self) -> bool:
        """Whether the expression is a condition (TRUE/FALSE) rather than a value."""
        return False

    def _walk(self) -> Iterator[SqlNode]:
        yield self
        for child in self.children():
            yield from child._walk()


@acceptor
@dataclass(frozen=True)
class Column(SqlNode):
    """A column of the field's own dataset.

    `quoted` keeps the distinction Snowflake makes: an unquoted name is folded
    to upper case, a quoted one is stored exactly as written.
    """
    name: str
    quoted: bool = False

    @property
    def identifier(self) -> str:
        """The column as a Snowflake identifier: quoted again if it was quoted."""
        if self.quoted:
            return '"' + self.name.replace('"', '""') + '"'
        return self.name

    @property
    def stored_name(self) -> str:
        """The name the column is stored under, which is what identifies it.

        ANSI SQL and Snowflake fold an unquoted name to upper case, so
        `l_linestatus`, `L_LINESTATUS` and `"L_LINESTATUS"` are one column.
        """
        return self.name if self.quoted else self.name.upper()

    def __str__(self) -> str:
        return self.identifier


@acceptor
@dataclass(frozen=True)
class Literal(SqlNode):
    """A number, string or boolean constant."""
    value: int | float | str | bool

    def __str__(self) -> str:
        if isinstance(self.value, bool):
            return "TRUE" if self.value else "FALSE"
        if isinstance(self.value, str):
            return "'" + self.value.replace("'", "''") + "'"
        return str(self.value)


@acceptor
@dataclass(frozen=True)
class Null(SqlNode):
    """The NULL constant."""

    def __str__(self) -> str:
        return "NULL"


@acceptor
@dataclass(frozen=True)
class Binary(SqlNode):
    """An arithmetic, concatenation or comparison operator applied to two operands."""
    op: SqlOp
    left: SqlNode
    right: SqlNode

    def children(self) -> tuple[SqlNode, ...]:
        return self.left, self.right

    @property
    def is_predicate(self) -> bool:
        return self.op.is_comparison

    def __str__(self) -> str:
        return f"({self.left} {self.op.value} {self.right})"


@acceptor
@dataclass(frozen=True)
class Negate(SqlNode):
    """Unary minus."""
    operand: SqlNode

    def children(self) -> tuple[SqlNode, ...]:
        return (self.operand,)

    def __str__(self) -> str:
        return f"-{self.operand}"


@acceptor
@dataclass(frozen=True)
class IsNull(SqlNode):
    """`operand IS NULL`, or `IS NOT NULL` when `negated`."""
    operand: SqlNode
    negated: bool = False

    def children(self) -> tuple[SqlNode, ...]:
        return (self.operand,)

    @property
    def is_predicate(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"({self.operand} IS {'NOT ' if self.negated else ''}NULL)"


@acceptor
@dataclass(frozen=True)
class Not(SqlNode):
    operand: SqlNode

    def children(self) -> tuple[SqlNode, ...]:
        return (self.operand,)

    @property
    def is_predicate(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"(NOT {self.operand})"


@acceptor
@dataclass(frozen=True)
class And(SqlNode):
    left: SqlNode
    right: SqlNode

    def children(self) -> tuple[SqlNode, ...]:
        return self.left, self.right

    @property
    def is_predicate(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"({self.left} AND {self.right})"


@acceptor
@dataclass(frozen=True)
class Or(SqlNode):
    left: SqlNode
    right: SqlNode

    def children(self) -> tuple[SqlNode, ...]:
        return self.left, self.right

    @property
    def is_predicate(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"({self.left} OR {self.right})"


@acceptor
@dataclass(frozen=True)
class Call(SqlNode):
    """A function call, `UPPER(name)`.

    Parsed so that a target can name the function it does not support, rather
    than the parser failing on the parenthesis.
    """
    name: str
    args: tuple[SqlNode, ...] = ()

    def children(self) -> tuple[SqlNode, ...]:
        return self.args

    def __str__(self) -> str:
        return f"{self.name}({', '.join(str(a) for a in self.args)})"
