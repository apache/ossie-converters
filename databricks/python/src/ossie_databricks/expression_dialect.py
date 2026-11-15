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

"""Labels a Databricks SQL expression with the most portable Apache Ossie dialect it can carry.

On import: ``OSSIE_SQL_2026`` when it uses only the Ossie expression language
(core-spec/expression_language.md), else ``ANSI_SQL`` when it uses only standard SQL, else
``DATABRICKS``.

A lexical check against allowlists, not a parser, and deliberately conservative: a construct
counts as portable only if it is listed here and, where the Ossie spec defines its behavior,
Databricks evaluates it that way. Anything else stays ``DATABRICKS``, which is always correct
because the expression came from a Metric View. The traps it guards against include Databricks
reading ``"x"`` as a string where portable SQL reads an identifier, a doubled quote in ``'a''b'``
that a legacy Databricks setting reads as two concatenated literals, backslash escapes in strings,
``TIMESTAMP`` carrying the session time zone, and same-named functions with other Databricks
meanings. It does not check behavior the spec leaves open, such as integer division, NULL handling
in CONCAT or GREATEST, rounding of ties, or the timestamp type DATE_TRUNC and DATEADD return. The
export also uses these token positions to qualify source columns in joined dimensions.
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import Enum

from ossie_databricks.ossie_converter_common import (
    DIALECT_ANSI,
    DIALECT_DATABRICKS,
    DIALECT_OSSIE_SQL,
)

# Functions in the Ossie expression language whose Databricks meaning matches. Left out on
# purpose: TRUNC/TRUNCATE (date truncation in Databricks), DATEDIFF (counts whole elapsed units in
# Databricks, unit boundaries crossed elsewhere), REGEXP_* (regex flavors differ), TO_CHAR and the
# format-string TO_DATE (format tokens differ), TO_TIMESTAMP (session-time-zone result), and window
# functions (OVER is not portable: Databricks sorts NULLs first by default, and a window over a
# measure depends on the query's grain).
OSSIE_FUNCTIONS = frozenset({
    "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE", "CURRENT_DATE", "CURRENT_TIMESTAMP", "YEAR",
    "QUARTER", "MONTH", "DAY", "DAYOFYEAR", "HOUR", "MINUTE", "SECOND", "EXTRACT", "DATE_PART",
    "DATE_TRUNC", "DATEADD", "TO_DATE", "CONCAT", "LENGTH", "LOWER", "UPPER", "TRIM", "LTRIM",
    "RTRIM", "LEFT", "RIGHT", "SUBSTRING", "REPLACE", "SPLIT_PART", "POSITION", "CHARINDEX",
    "CONTAINS", "STARTSWITH", "ENDSWITH", "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "MOD",
    "SIGN", "POWER", "SQRT", "EXP", "LN", "LOG", "LOG10", "SIN", "COS", "TAN", "ASIN", "ACOS",
    "ATAN", "ATAN2", "RADIANS", "DEGREES", "PI", "GREATEST", "LEAST", "IF", "IFF", "NULLIF",
    "COALESCE", "IFNULL", "NVL", "NVL2", "ZEROIFNULL", "NULLIFZERO", "CAST", "TRY_CAST",
})  # fmt: skip

# Standard SQL functions whose Databricks meaning matches. Those outside OSSIE_FUNCTIONS (CORR,
# COVAR_*, EVERY, the *_LENGTH family) make an expression ANSI_SQL rather than Ossie.
ANSI_FUNCTIONS = frozenset({
    "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV_POP", "STDDEV_SAMP", "VAR_POP", "VAR_SAMP",
    "CORR", "COVAR_POP", "COVAR_SAMP", "EVERY", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "CURRENT_DATE", "CURRENT_TIMESTAMP", "EXTRACT", "CAST", "COALESCE", "NULLIF", "UPPER",
    "LOWER", "TRIM", "SUBSTRING", "POSITION", "CHAR_LENGTH", "CHARACTER_LENGTH", "OCTET_LENGTH",
    "ABS", "MOD", "LN", "EXP", "POWER", "SQRT", "FLOOR", "CEIL", "CEILING",
})  # fmt: skip

# Aggregate calls (and the FILTER / WITHIN GROUP clauses that qualify one): in a measure, a bare
# name inside one is a column, while outside one it refers to another measure.
AGGREGATES = frozenset({
    "SUM", "COUNT", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
    "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT", "PERCENTILE_DISC",
    "APPROX_COUNT_DISTINCT", "APPROX_PERCENTILE", "CORR", "COVAR_POP", "COVAR_SAMP", "EVERY",
    "FILTER", "GROUP",
})  # fmt: skip

# Argument counts the portable form allows, for functions whose other Databricks forms mean
# something else (one-argument LOG = LN, COUNT(a, b) counting rows where all are non-null,
# CEIL(x, scale), and so on).
ARITY: dict[str, tuple[int, int]] = {
    "COUNT": (1, 1),
    "CEIL": (1, 1),
    "CEILING": (1, 1),
    "FLOOR": (1, 1),
    "DATEADD": (3, 3),
    "LOG": (2, 2),
    "TO_DATE": (1, 1),
    "APPROX_COUNT_DISTINCT": (1, 1),
    "APPROX_PERCENTILE": (2, 2),
    "TRIM": (1, 1),
    "LTRIM": (1, 1),
    "RTRIM": (1, 1),
    "REPLACE": (3, 3),
    "CHARINDEX": (2, 2),
}

# CAST target types whose Databricks meaning matches. Left out: VARCHAR/CHAR (a plain STRING in
# Databricks, without length semantics), FLOAT/REAL (32-bit in Databricks), the integer types
# (Databricks truncates a fractional value where others round), TIME, TIMESTAMP (session time
# zone), and DECIMAL/NUMERIC without (p, s) (DECIMAL(10, 0) in Databricks).
OSSIE_TYPES = frozenset({"STRING", "DECIMAL", "NUMERIC", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP_NTZ"})
ANSI_TYPES = frozenset({"DECIMAL", "NUMERIC", "BOOLEAN", "DATE"})
PARAMETERIZED_TYPES = frozenset({"DECIMAL", "NUMERIC"})

# Keywords valid in both dialects wherever they appear (the grammar itself is not checked).
KEYWORDS = frozenset({
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "BETWEEN", "LIKE", "IS",
    "NULL", "TRUE", "FALSE", "DISTINCT", "ORDER", "BY", "ASC", "DESC", "WITHIN",
})  # fmt: skip

# Keywords that may directly precede an opening parenthesis.
KEYWORDS_BEFORE_PAREN = frozenset({
    "AND", "OR", "NOT", "WHEN", "THEN", "ELSE", "CASE", "IN", "LIKE", "ILIKE", "BETWEEN",
    "DISTINCT", "BY", "FILTER", "GROUP",
})  # fmt: skip

# Keywords only valid in a specific position, checked in _Scan._word().
CONTEXT_KEYWORDS = frozenset({
    "AS", "FROM", "FOR", "ESCAPE", "WHERE", "NULLS", "BOTH", "LEADING", "TRAILING",
})  # fmt: skip

# Keywords that are themselves a complete operand.
VALUE_KEYWORDS = frozenset({"NULL", "TRUE", "FALSE"})

# Typed-literal prefixes: DATE '2024-01-15'. TIMESTAMP is left out (session time zone).
LITERAL_PREFIXES = frozenset({"DATE", "TIMESTAMP_NTZ"})

# Words that mean an expression is not a plain portable one (subqueries, intervals, windows,
# session functions, Databricks-only operators and constructors).
REJECTED_WORDS = frozenset({
    "SELECT", "WITH", "UNION", "EXCEPT", "INTERSECT", "HAVING", "JOIN", "LATERAL", "INTERVAL",
    "EXISTS", "ARRAY", "MAP", "STRUCT", "RLIKE", "REGEXP", "DIV", "ANY", "SOME", "ALL",
    "QUALIFY", "LIMIT", "WINDOW", "VALUES", "TABLE", "OVER", "FILTER", "GROUP", "CURRENT_USER",
    "USER", "SESSION_USER", "CURRENT_TIME", "LOCALTIME", "GROUPING__ID",
})  # fmt: skip

SYMBOLS = (
    "<>", "<=", ">=", "!=", "||", "<", ">", "=", "+", "-", "*", "/", "%", "(", ")", ",", ".",
)  # fmt: skip
# Databricks-only operators (shifts included) and comments, checked before SYMBOLS can match a
# prefix of them.
REJECTED_SYMBOLS = ("<=>", "==", "->", "=>", "<<", ">>", "--", "/*")
NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def classify(
    expr: str | None,
    datasets: AbstractSet[str],
    opaque_names: AbstractSet[str],
    measure: bool,
) -> str:
    """The most portable dialect ``expr`` can carry: OSSIE_SQL_2026, ANSI_SQL, or DATABRICKS.

    :param datasets: lower-cased dataset names of the Apache Ossie model; ``a.b`` is portable only
        when ``a`` is one of them (not ``source.``, a join alias, or a struct)
    :param opaque_names: lower-cased names whose meaning comes from the Metric View rather than a
        column (parameters, dimensions shadowing a column); a reference to one, bare or as
        ``dataset.name``, is not portable
    :param measure: whether ``expr`` is a measure, where a name outside an aggregate call refers
        to another measure rather than a column, so it is not portable
    """
    tokens = None if expr is None else _tokenize(expr)
    if not tokens:
        return DIALECT_DATABRICKS
    scan = _Scan(tokens, datasets, opaque_names, measure, None, None)
    if not scan.run():
        return DIALECT_DATABRICKS
    if scan.ossie:
        return DIALECT_OSSIE_SQL
    return DIALECT_ANSI if scan.ansi else DIALECT_DATABRICKS


def qualify_field_columns(expr: str, join_path: str) -> str | None:
    """Qualifies source columns in a scalar field expression.

    Returns None if the scanner cannot identify all column positions. Columns already qualified
    with this join path are preserved.
    """
    tokens = _tokenize(expr)
    if not tokens:
        return None
    columns: list[int] = []
    scan = _Scan(tokens, frozenset(), frozenset(), False, columns, join_path)
    if not scan.run():
        return None
    parts: list[str] = []
    start = 0
    for column in columns:
        parts.append(expr[start:column])
        parts.append(join_path)
        parts.append(".")
        start = column
    parts.append(expr[start:])
    return "".join(parts)


class _Kind(Enum):
    IDENT = 1
    NUMBER = 2
    STRING = 3
    SYMBOL = 4


@dataclass(frozen=True)
class _Token:
    kind: _Kind
    text: str
    start: int

    def is_(self, symbol: str) -> bool:
        return self.kind is _Kind.SYMBOL and self.text == symbol

    def is_word(self, word: str) -> bool:
        return self.kind is _Kind.IDENT and self.text.upper() == word.upper()

    def upper(self) -> str:
        return self.text.upper()


def _is_letter(c: str) -> bool:
    return c.isalpha()


def _is_digit(c: str) -> bool:
    return c.isdecimal()


def _is_ident_part(c: str) -> bool:
    return c.isalpha() or c.isdecimal() or c == "_"


def _tokenize(s: str) -> list[_Token] | None:
    """Splits ``s`` into tokens, or returns None on any construct this check does not handle."""
    out: list[_Token] = []
    n = len(s)
    i = 0
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c == "'":
            end = s.find("'", i + 1)
            if end < 0:
                return None
            body = s[i + 1 : end]
            # A backslash escape reads differently, and so can a doubled quote: 'a''b' is one
            # literal with a quote in standard SQL, but two concatenated literals under a legacy
            # Databricks setting.
            if "\\" in body or (end + 1 < n and s[end + 1] == "'"):
                return None
            out.append(_Token(_Kind.STRING, body, i))
            i = end + 1
        elif _is_letter(c) or c == "_":
            j = i + 1
            while j < n and _is_ident_part(s[j]):
                j += 1
            out.append(_Token(_Kind.IDENT, s[i:j], i))
            i = j
        elif _is_digit(c) or (c == "." and i + 1 < n and _is_digit(s[i + 1])):
            m = NUMBER.match(s, i)
            if m is None:
                return None
            j = m.end()
            # A trailing letter is a Databricks typed-number suffix (1L, 2BD) or a malformed number.
            if j < n and _is_ident_part(s[j]):
                return None
            out.append(_Token(_Kind.NUMBER, s[i:j], i))
            i = j
        else:
            sym = _symbol_at(s, i)
            if sym is None:
                return None
            out.append(_Token(_Kind.SYMBOL, sym, i))
            i += len(sym)
    return out


def _symbol_at(s: str, i: int) -> str | None:
    for rejected in REJECTED_SYMBOLS:
        if s.startswith(rejected, i):
            return None
    for sym in SYMBOLS:
        if s.startswith(sym, i):
            return sym
    return None  # backticks, double quotes, ':', '[', '!', '|', ...


def _is_keyword(u: str) -> bool:
    return u in KEYWORDS or u in KEYWORDS_BEFORE_PAREN or u in CONTEXT_KEYWORDS


class _Frame:
    """A parenthesized group: the word that opened it, and its top-level argument shape."""

    __slots__ = ("opener", "aggregate", "commas", "empty", "keyword_form")

    def __init__(self, opener: str) -> None:
        self.opener = opener
        self.aggregate = opener in AGGREGATES
        self.commas = 0
        self.empty = True
        # EXTRACT ... FROM, POSITION ... IN, FILTER (WHERE ...), and so on
        self.keyword_form = False


class _Scan:
    """One pass over the tokens, narrowing which portable dialects the expression still fits."""

    def __init__(
        self,
        tokens: list[_Token],
        datasets: AbstractSet[str],
        opaque_names: AbstractSet[str],
        measure: bool,
        column_starts: list[int] | None,
        field_join_path: str | None,
    ) -> None:
        self.tokens = tokens
        self.datasets = datasets
        self.opaque_names = opaque_names
        self.measure = measure
        self.column_starts = column_starts
        self.field_join_path = field_join_path
        self.frames: list[_Frame] = []  # stack; the top is the last element
        self.pending_opener = ""
        self.ossie = True
        self.ansi = True

    def run(self) -> bool:
        tokens = self.tokens
        for i, t in enumerate(tokens):
            prev = tokens[i - 1] if i > 0 else None
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if (
                prev is not None
                and _ends_operand(prev)
                and _starts_operand(t)
                and not _is_typed_literal(prev, t)
            ):
                return False  # two operands side by side: 'a' 'b', or an alias as in SUM(x) total
            top = self.frames[-1] if self.frames else None
            if top is not None and not t.is_(")"):
                top.empty = False
            if t.kind in (_Kind.STRING, _Kind.NUMBER):
                ok = True
            elif t.kind is _Kind.SYMBOL:
                ok = self._symbol(t, prev, nxt, top)
            else:
                ok = self._ident(i, t, prev, nxt, top)
            if not ok:
                return False
        return not self.frames

    def _symbol(
        self, t: _Token, prev: _Token | None, nxt: _Token | None, top: _Frame | None
    ) -> bool:
        text = t.text
        if text == "(":
            self.frames.append(_Frame(self.pending_opener))
            self.pending_opener = ""
            return True
        if text == ")":
            return bool(self.frames) and self._close(self.frames.pop())
        if text == ",":
            if top is None:
                return False
            top.commas += 1
            return True
        if text == ".":
            # Only between two identifiers; `t.*` and a dot after a call are Databricks-only here.
            return (
                prev is not None
                and prev.kind is _Kind.IDENT
                and nxt is not None
                and nxt.kind is _Kind.IDENT
            )
        if text in ("%", "!="):
            self.ansi = False  # listed by the Ossie language but not standard SQL
        return True

    def _close(self, f: _Frame) -> bool:
        if f.keyword_form and f.commas > 0:
            return False  # a keyword form takes no extra arguments: TRIM(BOTH FROM s, 'x')
        if f.opener == "POSITION":
            return f.keyword_form  # POSITION(sub IN str), not POSITION(sub, str)
        if f.opener == "FILTER":
            return f.keyword_form  # FILTER (WHERE ...)
        if f.opener == "SUBSTRING" and not f.keyword_form:
            self.ansi = False  # the comma form is the Ossie language; standard SQL uses FROM/FOR
        arity = ARITY.get(f.opener)
        if arity is None or f.keyword_form:
            return True
        args = 0 if f.empty else f.commas + 1
        return arity[0] <= args <= arity[1]

    def _ident(
        self,
        i: int,
        t: _Token,
        prev: _Token | None,
        nxt: _Token | None,
        top: _Frame | None,
    ) -> bool:
        if prev is not None and prev.is_("."):
            return True  # a later part of a qualified name, already checked at its first part
        u = t.upper()
        if nxt is not None and nxt.is_("."):
            if self.field_join_path is None:
                return self._qualified_name(i, t)
            return self._belongs_to_join_path(i)
        if top is not None and _is_cast(top) and prev is not None and prev.is_word("AS"):
            return self._cast_type(u, nxt)
        if nxt is not None and nxt.is_("("):
            return self._call(u, prev)
        if nxt is not None and nxt.kind is _Kind.STRING and not _is_keyword(u):
            return self._typed_literal(u)
        return self._word(t, u, prev, nxt, top)

    def _belongs_to_join_path(self, i: int) -> bool:
        tokens = self.tokens
        part = i
        path: list[str] = []
        while (
            part + 2 < len(tokens)
            and tokens[part + 1].is_(".")
            and tokens[part + 2].kind is _Kind.IDENT
        ):
            path.append(tokens[part].text + ".")
            part += 2
        call = part + 1 < len(tokens) and tokens[part + 1].is_("(")
        return not call and (self.field_join_path + ".").upper() == "".join(path).upper()  # type: ignore[operator]

    def _qualified_name(self, i: int, head: _Token) -> bool:
        """``dataset.field`` is portable; any other head, deeper paths, or calls are not."""
        tokens = self.tokens
        if head.text.lower() not in self.datasets or not self._is_column_position():
            return False
        two_parts = i + 2 < len(tokens) and tokens[i + 2].kind is _Kind.IDENT
        deeper = i + 3 < len(tokens) and tokens[i + 3].is_(".")
        call = i + 3 < len(tokens) and tokens[i + 3].is_("(")
        # `source.col` reads the raw column in Databricks, but `dataset.col` names the field when a
        # dimension of that name shadows it.
        return (
            two_parts
            and not deeper
            and not call
            and tokens[i + 2].text.lower() not in self.opaque_names
        )

    def _is_column_position(self) -> bool:
        """False for a name in a measure outside an aggregate call, which refers to a measure."""
        if not self.measure:
            return True
        return any(f.aggregate for f in self.frames)

    def _call(self, u: str, prev: _Token | None) -> bool:
        in_ossie = u in OSSIE_FUNCTIONS
        in_ansi = u in ANSI_FUNCTIONS
        if in_ossie or in_ansi:
            self.ossie &= in_ossie
            # Standard SQL writes CURRENT_DATE and CURRENT_TIMESTAMP without parentheses.
            self.ansi &= in_ansi and not u.startswith("CURRENT_")
            self.pending_opener = u
            return True
        if u not in KEYWORDS_BEFORE_PAREN:
            return False  # a function neither dialect lists, MEASURE(...) and window OVER included
        if u == "FILTER":
            if prev is None or not prev.is_(")"):
                return False  # FILTER only qualifies an aggregate call: SUM(x) FILTER (WHERE ...)
            self.ossie = False
        elif u == "ILIKE":
            self.ansi = False
        elif u == "GROUP" and (prev is None or not prev.is_word("WITHIN")):
            return False
        self.pending_opener = u
        return True

    def _typed_literal(self, u: str) -> bool:
        if u not in LITERAL_PREFIXES:
            return False  # TIMESTAMP '...' (session time zone), X'..', and other prefixes
        self.ansi &= u == "DATE"
        return True

    def _cast_type(self, u: str, nxt: _Token | None) -> bool:
        params = nxt is not None and nxt.is_("(")
        if (u in PARAMETERIZED_TYPES) != params:
            return False  # DECIMAL needs (p, s); other types take none
        if params:
            self.pending_opener = "TYPE"
        in_ossie = u in OSSIE_TYPES
        in_ansi = u in ANSI_TYPES
        self.ossie &= in_ossie
        self.ansi &= in_ansi
        return in_ossie or in_ansi

    def _word(
        self,
        t: _Token,
        u: str,
        prev: _Token | None,
        nxt: _Token | None,
        top: _Frame | None,
    ) -> bool:
        opener = "" if top is None else top.opener
        if u == "AS":
            return _is_cast(top)
        if u == "FROM":
            if opener == "EXTRACT":
                top.keyword_form = True  # type: ignore[union-attr]
                return True
            if prev is not None and prev.is_word("DISTINCT"):
                return True  # IS [NOT] DISTINCT FROM
            return self._standard_keyword_form(top, "SUBSTRING", "TRIM")
        if u == "FOR":
            return self._standard_keyword_form(top, "SUBSTRING")
        if u in ("BOTH", "LEADING", "TRAILING"):
            return self._standard_keyword_form(top, "TRIM")
        if u == "WHERE":
            if opener == "FILTER" and prev is not None and prev.is_("("):
                top.keyword_form = True  # type: ignore[union-attr]
                return True
            return False
        if u == "IN":
            if opener == "POSITION":
                top.keyword_form = True  # type: ignore[union-attr]
            return True
        if u == "ILIKE":
            self.ansi = False
            return True
        if u == "ESCAPE":
            self.ossie = False
            return True
        if u == "NULLS":
            if nxt is not None and (nxt.is_word("FIRST") or nxt.is_word("LAST")):
                self.ossie = False
                return True
            return False
        if u in REJECTED_WORDS:
            return False
        if _is_keyword(u):
            return True
        if not self._is_column_position() or t.text.lower() in self.opaque_names:
            return False
        if self.column_starts is not None and not _is_non_column_word(u, top):
            self.column_starts.append(t.start)
        return True

    def _standard_keyword_form(self, top: _Frame | None, *openers: str) -> bool:
        """FROM / FOR / BOTH inside SUBSTRING or TRIM: standard SQL, not the Ossie language."""
        if top is None:
            return False
        if top.opener in openers:
            top.keyword_form = True
            self.ossie = False
            return True
        return False


def _ends_operand(t: _Token) -> bool:
    if t.kind is _Kind.IDENT:
        u = t.upper()
        return not _is_keyword(u) or u in VALUE_KEYWORDS or u == "END"
    return t.kind in (_Kind.NUMBER, _Kind.STRING) or t.is_(")")


def _starts_operand(t: _Token) -> bool:
    if t.kind is _Kind.IDENT:
        u = t.upper()
        return not _is_keyword(u) or u in VALUE_KEYWORDS
    return t.kind in (_Kind.NUMBER, _Kind.STRING)


def _is_typed_literal(prev: _Token, t: _Token) -> bool:
    return t.kind is _Kind.STRING and prev.kind is _Kind.IDENT and prev.upper() in LITERAL_PREFIXES


def _is_non_column_word(word: str, frame: _Frame | None) -> bool:
    if word in ("CURRENT_DATE", "CURRENT_TIMESTAMP"):
        return True
    return frame is not None and (
        (frame.opener == "EXTRACT" and not frame.keyword_form)
        or (frame.opener == "DATEADD" and frame.commas == 0)
    )


def _is_cast(f: _Frame | None) -> bool:
    return f is not None and f.opener in ("CAST", "TRY_CAST")
