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

"""A small, dependency-free parser for DAX expressions.

It turns a measure body into a syntax tree for ``_dax_to_sql`` to translate. It parses
the whole DAX expression grammar (operators with DAX precedence, function calls,
``VAR`` / ``RETURN``, column and measure references, table constructors with single- or
multi-column rows, comments), so
that a construct the translator does not handle is *recognised and refused by name*
rather than misread. It does no evaluation and knows nothing about any model.

Precedence, highest first, as documented for DAX: ``^``, sign, ``*`` ``/``, ``+`` ``-``,
``&``, comparison (``=`` ``==`` ``<>`` ``<`` ``>`` ``<=`` ``>=`` ``IN``), ``NOT``,
``&&``, ``||``. Note that ``^`` binds tighter than sign, so ``-2^2`` is ``-(2^2)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class DaxParseError(ValueError):
    """The text is not a DAX expression this parser understands."""


# --- Syntax tree ------------------------------------------------------------------


@dataclass(frozen=True)
class Number:
    text: str


@dataclass(frozen=True)
class String:
    value: str


@dataclass(frozen=True)
class Column:
    """``Table[Column]`` or ``'Table Name'[Column]``."""

    table: str
    column: str


@dataclass(frozen=True)
class Bracket:
    """An unqualified ``[Name]``: a measure, or a column in row context."""

    name: str


@dataclass(frozen=True)
class Name:
    """A bare identifier: a variable, or an unquoted table name."""

    name: str


@dataclass(frozen=True)
class Table:
    """A quoted table reference on its own: ``'Sales'``."""

    name: str


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple


@dataclass(frozen=True)
class Empty:
    """An omitted optional argument, as in ``FUNC(a, , b)``."""


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class Var:
    """``VAR a = ... VAR b = ... RETURN body``; ``bindings`` is ((name, expr), ...)."""

    bindings: tuple
    body: object


@dataclass(frozen=True)
class TableConstructor:
    rows: tuple


@dataclass(frozen=True)
class Row:
    """A multi-column row of a table constructor: ``(1, "a")`` in ``{(1, "a"), (2, "b")}``."""

    values: tuple


# --- Lexer ------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*|--[^\n]*|/\*.*?\*/)
    | (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    | (?P<string>"(?:[^"]|"")*")
    | (?P<qtable>'(?:[^']|'')*')
    | (?P<bracket>\[(?:[^\]]|\]\])*\])
    | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<op>==|<>|<=|>=|&&|\|\||[-+*/^&=<>(),{}])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str


def _tokenize(text):
    tokens = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise DaxParseError(f"unexpected character {text[position]!r}")
        position = match.end()
        kind = match.lastgroup
        if kind in ("ws", "comment"):
            continue
        tokens.append(_Tok(kind, match.group()))
    tokens.append(_Tok("eof", ""))
    return tokens


# --- Parser -----------------------------------------------------------------------

_COMPARISON = {"=", "==", "<>", "<", ">", "<=", ">="}


class _Parser:
    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.index = 0

    def peek(self, offset=0):
        return self.tokens[self.index + offset]

    def take(self):
        token = self.tokens[self.index]
        self.index += 1
        return token

    def at_op(self, *ops):
        token = self.peek()
        return token.kind == "op" and token.text in ops

    def at_keyword(self, word):
        token = self.peek()
        return token.kind == "ident" and token.text.upper() == word

    def expect_op(self, op):
        if not self.at_op(op):
            raise DaxParseError(f"expected {op!r}, found {self.peek().text or 'end of input'!r}")
        self.take()

    def parse(self):
        node = self.expression()
        if self.peek().kind != "eof":
            raise DaxParseError(f"unexpected {self.peek().text!r} after the expression")
        return node

    def expression(self):
        if self.at_keyword("VAR"):
            return self.var_block()
        return self.or_expr()

    def var_block(self):
        bindings = []
        while self.at_keyword("VAR"):
            self.take()
            name = self.take()
            if name.kind != "ident":
                raise DaxParseError("expected a variable name after VAR")
            self.expect_op("=")
            bindings.append((name.text, self.expression()))
        if not self.at_keyword("RETURN"):
            raise DaxParseError("expected RETURN after the VAR definitions")
        self.take()
        return Var(tuple(bindings), self.expression())

    def or_expr(self):
        node = self.and_expr()
        while self.at_op("||"):
            self.take()
            node = Binary("||", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.not_expr()
        while self.at_op("&&"):
            self.take()
            node = Binary("&&", node, self.not_expr())
        return node

    def not_expr(self):
        # NOT as an operator; NOT(...) followed by "(" is the function form.
        if self.at_keyword("NOT") and not (
            self.peek(1).kind == "op" and self.peek(1).text == "("
        ):
            self.take()
            return Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self):
        node = self.concat()
        while True:
            if self.at_op(*_COMPARISON):
                op = self.take().text
                node = Binary(op, node, self.concat())
            elif self.at_keyword("IN"):
                self.take()
                node = Binary("IN", node, self.concat())
            else:
                return node

    def concat(self):
        node = self.additive()
        while self.at_op("&"):
            self.take()
            node = Binary("&", node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.take().text
            node = Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.sign()
        while self.at_op("*", "/"):
            op = self.take().text
            node = Binary(op, node, self.sign())
        return node

    def sign(self):
        if self.at_op("+", "-"):
            op = self.take().text
            return Unary(op, self.sign())
        return self.power()

    def power(self):
        node = self.primary()
        while self.at_op("^"):
            self.take()
            node = Binary("^", node, self.sign())
        return node

    def primary(self):
        token = self.take()
        if token.kind == "number":
            return Number(token.text)
        if token.kind == "string":
            return String(token.text[1:-1].replace('""', '"'))
        if token.kind == "bracket":
            return Bracket(_unbracket(token.text))
        if token.kind == "qtable":
            table = token.text[1:-1].replace("''", "'")
            if self.peek().kind == "bracket":
                return Column(table, _unbracket(self.take().text))
            return Table(table)
        if token.kind == "ident":
            if self.at_op("("):
                self.take()
                return Call(token.text.upper(), self.arguments())
            if self.peek().kind == "bracket":
                return Column(token.text, _unbracket(self.take().text))
            return Name(token.text)
        if token.kind == "op" and token.text == "(":
            node = self.expression()
            if not self.at_op(","):
                self.expect_op(")")
                return node
            values = [node]
            while self.at_op(","):
                self.take()
                values.append(self.expression())
            self.expect_op(")")
            return Row(tuple(values))
        if token.kind == "op" and token.text == "{":
            return self.table_constructor()
        raise DaxParseError(f"unexpected {token.text or 'end of input'!r}")

    def arguments(self):
        args = []
        if self.at_op(")"):
            self.take()
            return ()
        while True:
            if self.at_op(",", ")"):
                args.append(Empty())
            else:
                args.append(self.expression())
            if self.at_op(","):
                self.take()
                continue
            self.expect_op(")")
            return tuple(args)

    def table_constructor(self):
        rows = []
        if not self.at_op("}"):
            while True:
                rows.append(self.expression())
                if not self.at_op(","):
                    break
                self.take()
        self.expect_op("}")
        return TableConstructor(tuple(rows))


def _unbracket(text):
    return text[1:-1].replace("]]", "]")


def parse_dax(text):
    """Parse a DAX expression (an optional leading ``=`` is ignored) into a syntax tree.

    Raises ``DaxParseError`` when the text is not a DAX expression.
    """
    body = (text or "").strip()
    if body.startswith("="):
        body = body[1:]
    if not body.strip():
        raise DaxParseError("the expression is empty")
    return _Parser(body).parse()
