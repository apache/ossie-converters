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

"""Tokens of the scalar SQL subset a dataset field's expression is written in.

The spelling follows Snowflake, which ANSI SQL agrees with for everything here:
keywords are case-insensitive, `"..."` quotes an identifier (`""` escapes a
quote inside it), and `'...'` is a string (`''` escapes a quote inside it).
"""

import ply.lex as lex


class SqlLexer:

    def __init__(self):
        self.lexer = lex.lex(module=self)

    reserved = {
        'AND': 'AND',
        'FALSE': 'FALSE',
        'IS': 'IS',
        'NOT': 'NOT',
        'NULL': 'NULL',
        'OR': 'OR',
        'TRUE': 'TRUE',
    }

    # Keywords of SQL the grammar does not cover, rejected as they are lexed: the
    # parser only notices a token one past where it went wrong, so `CAST(a AS
    # INT)` would otherwise be reported at its parenthesis.
    unsupported = frozenset({
        'BETWEEN', 'CASE', 'CAST', 'ELSE', 'END', 'EXISTS', 'FROM', 'ILIKE',
        'IN', 'LIKE', 'OVER', 'SELECT', 'THEN', 'WHEN',
    })

    tokens = [
        'ID',
        'QUOTED_ID',
        'INTEGER',
        'FLOAT',
        'STRING',
        'CONCAT',
        'PLUS',
        'MINUS',
        'TIMES',
        'DIVIDE',
        'MODULO',
        'EQ',
        'NE',
        'LT',
        'LE',
        'GT',
        'GE',
        'LPAREN',
        'RPAREN',
        'COMMA',
        'DOT',
        'CAST_OP',
    ] + list(reserved.values())

    t_ignore = ' \t\r'

    t_PLUS = r'\+'
    t_MINUS = r'-'
    t_TIMES = r'\*'
    t_DIVIDE = r'/'
    t_MODULO = r'%'
    t_EQ = r'='
    t_LPAREN = r'\('
    t_RPAREN = r'\)'
    t_COMMA = r','
    t_DOT = r'\.'

    # ply tries function rules before string rules, and in definition order,
    # so the two-character operators are functions declared ahead of their
    # one-character prefixes: `<=` and `<>` before `<`, `>=` before `>`.
    def t_CONCAT(self, t):
        r'\|\|'
        return t

    def t_CAST_OP(self, t):
        r'::'
        return t

    def t_NE(self, t):
        r'<>|!='
        t.value = '<>'
        return t

    def t_LE(self, t):
        r'<='
        return t

    def t_GE(self, t):
        r'>='
        return t

    def t_LT(self, t):
        r'<'
        return t

    def t_GT(self, t):
        r'>'
        return t

    def t_NEWLINE(self, t):
        r'\n+'
        t.lexer.lineno += len(t.value)

    # FLOAT before INTEGER, or '31.4' would lex as INTEGER DOT INTEGER.
    def t_FLOAT(self, t):
        r'\d+\.\d+'
        t.value = float(t.value)
        return t

    def t_INTEGER(self, t):
        r'\d+'
        t.value = int(t.value)
        return t

    def t_STRING(self, t):
        r"'(?:[^']|'')*'"
        t.value = t.value[1:-1].replace("''", "'")
        return t

    def t_QUOTED_ID(self, t):
        r'"(?:[^"]|"")+"'
        t.value = t.value[1:-1].replace('""', '"')
        return t

    def t_ID(self, t):
        r'[^\W\d][\w$]*'
        upper = t.value.upper()
        if upper in self.reserved:
            t.type = self.reserved[upper]
        elif upper in self.unsupported:
            raise SyntaxError(f"'{upper}' is not supported")
        return t

    def t_error(self, t):
        raise SyntaxError(f"Illegal character {t.value[0]!r} at position {t.lexpos}")
