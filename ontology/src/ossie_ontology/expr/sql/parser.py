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

"""Parser for the scalar SQL a dataset field's expression is written in.

Covers what a row-level field needs: columns (bare or quoted), number, string
and boolean literals, NULL, arithmetic, `||`, comparisons, `IS [NOT] NULL`,
`AND`/`OR`/`NOT`, parentheses, and function calls. Anything else -- `CASE`,
`CAST`, `IN`, a column qualified by a table -- is rejected with a message that
names it, rather than being misread.
"""

from __future__ import annotations

import ply.yacc as yacc

from ossie_ontology.expr.sql.lexer import SqlLexer
from ossie_ontology.expr.sql.model import (
    And,
    Binary,
    Call,
    Column,
    IsNull,
    Literal,
    Negate,
    Not,
    Null,
    Or,
    SqlNode,
    SqlOp,
)


class SqlSyntaxError(ValueError):
    """The expression is not in the SQL subset the parser understands."""


class SqlExpressionParser:

    def __init__(self):
        # Same reasoning as FormulaParser: no parsetab.py or parser.out written
        # next to an installed package.
        self.parser = yacc.yacc(module=self, debug=False, write_tables=False)
        self.lexer = SqlLexer()
        self._text = ""

    tokens = SqlLexer.tokens

    # Lowest binding first. `||` shares the additive level, as in Snowflake, so
    # `a || b + c` is `(a || b) + c`.
    precedence = (
        ("left", "OR"),
        ("left", "AND"),
        ("right", "NOT"),
        ("nonassoc", "IS"),
        ("nonassoc", "EQ", "NE", "LT", "LE", "GT", "GE"),
        ("left", "PLUS", "MINUS", "CONCAT"),
        ("left", "TIMES", "DIVIDE", "MODULO"),
        ("right", "UMINUS"),
        ("left", "CAST_OP", "DOT"),
    )

    # ----------------- grammar ------------------------------------------

    def p_goal(self, p):
        "goal : expr"
        p[0] = p[1]

    def p_expr_or(self, p):
        "expr : expr OR expr"
        p[0] = Or(p[1], p[3])

    def p_expr_and(self, p):
        "expr : expr AND expr"
        p[0] = And(p[1], p[3])

    def p_expr_not(self, p):
        "expr : NOT expr"
        p[0] = Not(p[2])

    def p_expr_is_null(self, p):
        "expr : expr IS NULL"
        p[0] = IsNull(p[1])

    def p_expr_is_not_null(self, p):
        "expr : expr IS NOT NULL"
        p[0] = IsNull(p[1], negated=True)

    def p_expr_binary(self, p):
        """
        expr : expr EQ expr
             | expr NE expr
             | expr LT expr
             | expr LE expr
             | expr GT expr
             | expr GE expr
             | expr PLUS expr
             | expr MINUS expr
             | expr CONCAT expr
             | expr TIMES expr
             | expr DIVIDE expr
             | expr MODULO expr
        """
        p[0] = Binary(SqlOp(p[2]), p[1], p[3])

    def p_expr_negate(self, p):
        "expr : MINUS expr %prec UMINUS"
        operand = p[2]
        # A negative number is a literal, not an operation on one.
        if isinstance(operand, Literal) and isinstance(operand.value, (int, float)) \
                and not isinstance(operand.value, bool):
            p[0] = Literal(-operand.value)
        else:
            p[0] = Negate(operand)

    def p_expr_plus(self, p):
        "expr : PLUS expr %prec UMINUS"
        p[0] = p[2]

    def p_expr_parenthesized(self, p):
        "expr : LPAREN expr RPAREN"
        p[0] = p[2]

    def p_expr_call(self, p):
        "expr : ID LPAREN args RPAREN"
        p[0] = Call(p[1].upper(), tuple(p[3]))

    def p_expr_call_no_args(self, p):
        "expr : ID LPAREN RPAREN"
        p[0] = Call(p[1].upper())

    def p_args(self, p):
        "args : expr"
        p[0] = [p[1]]

    def p_args_many(self, p):
        "args : args COMMA expr"
        p[1].append(p[3])
        p[0] = p[1]

    def p_expr_column(self, p):
        "expr : ID"
        p[0] = Column(p[1])

    def p_expr_quoted_column(self, p):
        "expr : QUOTED_ID"
        p[0] = Column(p[1], quoted=True)

    def p_expr_literal(self, p):
        """
        expr : INTEGER
             | FLOAT
             | STRING
        """
        p[0] = Literal(p[1])

    def p_expr_bool(self, p):
        """
        expr : TRUE
             | FALSE
        """
        p[0] = Literal(p[1].upper() == "TRUE")

    def p_expr_null(self, p):
        "expr : NULL"
        p[0] = Null()

    # The constructs below are recognised only to be rejected by name: a field
    # that uses one gets told which, instead of "unexpected token".

    def p_expr_cast(self, p):
        "expr : expr CAST_OP expr"
        raise SqlSyntaxError(f"'::' casts are not supported, in {self._text!r}")

    def p_expr_qualified(self, p):
        "expr : expr DOT ID"
        raise SqlSyntaxError(
            f"Qualified column names are not supported (a field reads its own dataset), "
            f"in {self._text!r}"
        )

    def p_error(self, p):
        if p is None:
            raise SqlSyntaxError(f"Unexpected end of expression in {self._text!r}")
        raise SqlSyntaxError(f"Unexpected {p.value!r} at position {p.lexpos}, in {self._text!r}")

    # ----------------- entrypoint ----------------------------------------

    def parse(self, text: str) -> SqlNode:
        self._text = text
        try:
            return self.parser.parse(text, lexer=self.lexer.lexer)
        except SyntaxError as e:
            # From the lexer: a character no token starts with, or a keyword
            # of SQL the grammar does not cover.
            raise SqlSyntaxError(f"{e}, in {text!r}") from e
