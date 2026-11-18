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

"""Recognize the portable SQL forms this converter emits and passes through."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

# SQLGlot's Snowflake reader understands Ossie's unit-first DATEADD and DATEDIFF.
PORTABLE_SQL_READER = "snowflake"

_PORTABLE_NODES = {
    exp.Add,
    exp.And,
    exp.Avg,
    exp.Between,
    exp.Boolean,
    exp.Case,
    exp.Coalesce,
    exp.Column,
    exp.Count,
    exp.DateAdd,
    exp.DateDiff,
    exp.Distinct,
    exp.Div,
    exp.EQ,
    exp.GT,
    exp.GTE,
    exp.Identifier,
    exp.ILike,
    exp.If,  # Also represents a WHEN branch inside CASE.
    exp.In,
    exp.Is,
    exp.LT,
    exp.LTE,
    exp.Like,
    exp.Literal,
    exp.Max,
    exp.Min,
    exp.Mod,
    exp.Mul,
    exp.NEQ,
    exp.Neg,
    exp.Not,
    exp.Null,
    exp.Nullif,
    exp.Or,
    exp.Order,
    exp.Ordered,
    exp.Paren,
    exp.PercentileCont,
    exp.PercentileDisc,
    exp.Star,
    exp.Sub,
    exp.Sum,
    exp.Var,  # The unit in DATEADD and DATEDIFF.
    exp.WithinGroup,
}


def _tokens(sql: str) -> list[tuple[TokenType, str]]:
    tokens = sqlglot.tokenize(sql, read=PORTABLE_SQL_READER)
    parts = []
    for token in tokens:
        spelling = sql[token.start : token.end + 1]
        if token.token_type not in (TokenType.STRING, TokenType.IDENTIFIER):
            spelling = spelling.upper()
        parts.append((token.token_type, spelling))
    return parts


def is_portable_sql(sql: str) -> bool:
    """Recognize supported forms without promoting vendor syntax normalized by the parser."""
    try:
        statements = sqlglot.parse(sql, read=PORTABLE_SQL_READER)
        if len(statements) != 1 or statements[0] is None:
            return False
        tree = statements[0]
        if any(type(node) not in _PORTABLE_NODES for node in tree.walk()):
            return False
        # For example, the reader normalizes Snowflake NVL into COALESCE. Compare
        # the token spellings so we only promote SQL already written in the portable form.
        return _tokens(sql) == _tokens(tree.sql(dialect=PORTABLE_SQL_READER))
    except SqlglotError:
        return False
