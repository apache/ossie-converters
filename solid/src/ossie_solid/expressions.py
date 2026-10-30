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

"""Metric expression rewriting between Solid and Apache Ossie conventions.

Solid stores a metric formula against **bare** column names and records the owning table
separately (`tables: [shop.sales.orders]`); solid-server's `is_valid_metric_formula`
actively strips alias prefixes before saving. Apache Ossie instead expects a metric to be
self-contained, qualifying each column with its dataset
(`SUM(store_sales.ss_ext_sales_price)`), so the two directions here add and remove that
qualifier.

The rewrite is a **surgical splice**, not a re-render. Every column occurrence is located
by sqlglot's tokenizer, which reports character positions and keeps string literals and
quoted identifiers in token types of their own, and the qualifier is inserted or removed
at those offsets. Everything else in the expression is left character-for-character
intact.

That matters because round-tripping a parsed tree through `Expression.sql()` does not
return the author's SQL -- sqlglot canonicalizes as it generates, so
`CAST(x AS FLOAT)` comes back as `CAST(x AS DOUBLE)` and
`EXTRACT(year FROM d)` as `DATE_PART(YEAR, d)`. Those are semantically equal and
textually different, and a converter has no business rewriting SQL it was only asked to
qualify.

The parser is still used, as a cross-check: it decides which names are genuinely bare
column references, and a token scan that disagrees with it means some occurrence is
something else (`EXTRACT(year FROM ...)` where a column is also called `year`). On any
disagreement the expression is left exactly as written -- an unqualified metric is a far
smaller problem than a corrupted one.
"""

import re
from collections import Counter

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from . import _reserved_words
from ._common import (
    DIALECT_ANSI,
    DIALECT_BIGQUERY,
    DIALECT_DATABRICKS,
    DIALECT_OSSIE_SQL,
    DIALECT_SNOWFLAKE,
    SQLGLOT_DIALECTS,
)

# Token types that always carry an identifier: an unquoted name, or a quoted one whose
# offsets span the quote characters as well.
_NAME_TOKENS = (TokenType.VAR, TokenType.IDENTIFIER)

# Token types whose text is a value, never a name.
_LITERAL_TOKENS = frozenset(
    t for t in TokenType if t.name.endswith("STRING") or t.name == "NUMBER")

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _is_name_token(token):
    """True if `token` could be a column or dataset name.

    sqlglot tokenizes many ordinary names as keywords -- a column called `date`,
    `comment` or `first`, a table called `session` -- even where the parser goes on to
    read them as columns, so a keyword token spelled like a name counts too. The AST
    cross-check in the callers decides whether each match really is a column, and
    leaves the text alone when it is not.
    """
    if token.token_type in _NAME_TOKENS:
        return True
    return token.token_type not in _LITERAL_TOKENS and bool(_WORD.fullmatch(token.text))

# Conversion outcomes, returned alongside the expression.
QUALIFIED = "qualified"
UNQUALIFIED = "unqualified"
UNCHANGED = "unchanged"
UNPARSED = "unparsed"
AMBIGUOUS = "ambiguous"
SHARED = "shared"


def _read(dialect):
    """Apache Ossie dialect name -> the sqlglot dialect to parse/tokenize with."""
    return SQLGLOT_DIALECTS.get(dialect)


def _parse(expression, dialect):
    """Parse a scalar/aggregate expression, or return None if it does not parse.

    Metric expressions are fragments rather than statements, so `parse_one` is used
    directly; a fragment sqlglot has no grammar for yields None and the caller leaves the
    text alone.
    """
    try:
        return sqlglot.parse_one(expression, read=_read(dialect)) or None
    except (SqlglotError, ValueError, RecursionError):
        return None


def _tokenize(expression, dialect):
    """Tokenize an expression, or return None if it cannot be tokenized."""
    try:
        return sqlglot.tokenize(expression, read=_read(dialect))
    except (SqlglotError, ValueError, RecursionError):
        return None


def _unquote(text):
    """Strip the quoting a tokenizer may have left on an identifier's text."""
    return text.strip().strip('"`[]')


# Reserved words per dialect (see _reserved_words for where each list comes from).
_RESERVED = {
    DIALECT_SNOWFLAKE: _reserved_words.SNOWFLAKE,
    DIALECT_DATABRICKS: _reserved_words.DATABRICKS,
    DIALECT_BIGQUERY: _reserved_words.BIGQUERY,
    DIALECT_ANSI: _reserved_words.ANSI_SQL,
    DIALECT_OSSIE_SQL: _reserved_words.ANSI_SQL,
}

# The shape of a name that needs no quoting. Snowflake also allows `$` after the first
# character.
_REGULAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REGULAR_NAME_BY_DIALECT = {DIALECT_SNOWFLAKE: re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")}


def needs_quoting(name, dialect):
    """True if `name` cannot be written bare in `dialect`: it is not a regular
    identifier, or it is a reserved word there."""
    pattern = _REGULAR_NAME_BY_DIALECT.get(dialect, _REGULAR_NAME)
    if not pattern.fullmatch(name):
        return True
    return name.upper() in _RESERVED.get(dialect, _reserved_words.ANSI_SQL)


def column_reference(name, dialect):
    """Render a column or dataset name as an identifier in `dialect`.

    A Solid field's expression is just its own column, so the usual result is the bare
    name. A name that is not a regular identifier, or is a reserved word in the dialect,
    is quoted with that dialect's rules so the expression stays valid SQL.

    On Snowflake a quoted name matches exactly while a bare one folds to upper case, so
    a reserved word is quoted in upper case: `order` becomes `"ORDER"`, the column it
    would name if it could be written bare, as every other name in the model is, and as
    Solid's own formulas are. A name that is not a regular identifier has no bare form
    to follow, and keeps its case.
    """
    if not isinstance(name, str) or not name.strip():
        return name
    if not needs_quoting(name, dialect):
        return name
    if dialect == DIALECT_SNOWFLAKE and _REGULAR_NAME_BY_DIALECT[dialect].fullmatch(name):
        name = name.upper()
    quoted = exp.to_identifier(name, quoted=True)
    return quoted.sql(dialect=_read(dialect)) if quoted is not None else name


def is_column_reference(expression, name, dialect):
    """True if `expression` is nothing but a reference to the column `name`, bare or
    quoted the dialect's way (`"a""b"` is the column a"b on Snowflake)."""
    text = expression.strip()
    if text.strip('"`[]').lower() == name.strip().lower():
        return True
    tree = _parse(text, dialect)
    return (isinstance(tree, exp.Column) and not tree.table
            and tree.name.lower() == name.strip().lower())


def _path(column):
    """A column reference's dotted path, lower-cased: `a.b.c` -> ["a", "b", "c"]."""
    return [part.name.lower() for part in column.parts]


def _bare_columns(tree, known):
    """Count, per name, the column references the AST reports that start with a column
    in `known`: a bare column, or the head of a struct path (`customer.id`, where
    `customer` is a STRUCT column). Solid stores formulas against bare names, so a path
    that starts with one of the table's columns is a field access into it, and the
    dataset qualifier belongs in front of the whole path.
    """
    counts = Counter()
    for column in tree.find_all(exp.Column):
        head = _path(column)[0]
        if head in known:
            counts[head] += 1
    return counts


def _qualified_columns(tree, known, owned):
    """Count, per (qualifier, name), the column references qualified by a dataset in
    `known`: `orders.amount`, and `orders.customer.id` when `customer` is a field of
    `orders` (`owned` maps each dataset to its field names). Any other longer path
    (`db.table.column`) starts with a schema, not a dataset, and is left out."""
    counts = Counter()
    for column in tree.find_all(exp.Column):
        path = _path(column)
        if len(path) < 2 or path[0] not in known:
            continue
        if len(path) == 2 or path[1] in owned.get(path[0], ()):
            counts[(path[0], path[1])] += 1
    return counts


def _splice(expression, edits):
    """Apply (start, end, replacement) edits to `expression`, right to left.

    `end` is exclusive. Applying in reverse order keeps earlier offsets valid.
    """
    result = expression
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def qualify_metric(expression, dialect, dataset, columns):
    """Prefix every bare column in `expression` with `dataset`.

    `columns` is the set of column names the dataset owns, compared case-insensitively
    (Solid models routinely mix `ACCOUNT_ID` in the column list with `account_id` in the
    formula). A column that already carries a qualifier is left as the author wrote it.

    Returns (expression, status):
      "qualified"  -- at least one column was prefixed, by splicing into the original text
      "unchanged"  -- nothing needed prefixing; the text is returned verbatim
      "unparsed"   -- sqlglot could not read it; the text is returned verbatim
      "ambiguous"  -- the token scan and the parse disagree on which names are columns,
                      so the text is returned verbatim rather than risk a bad edit
    """
    return qualify_by_owner(expression, dialect, {c.lower(): dataset for c in columns})


def qualify_by_owner(expression, dialect, owners, shared=()):
    """Prefix every bare column in `expression` with the dataset that owns it.

    `owners` maps a lower-cased column name to its dataset, for a metric that reads
    several. `shared` names columns more than one of those datasets has: nothing in the
    file says which one the formula means, so an expression reading one is returned
    verbatim with the status "shared". Otherwise as `qualify_metric`.
    """
    if not expression or not expression.strip():
        return expression, UNCHANGED
    shared = {c.lower() for c in shared}
    known = set(owners) | shared
    if not known:
        return expression, UNCHANGED

    tree = _parse(expression, dialect)
    tokens = _tokenize(expression, dialect)
    if tree is None or tokens is None:
        return expression, UNPARSED

    expected = _bare_columns(tree, known)
    if not expected:
        return expression, UNCHANGED
    if shared & set(expected):
        return expression, SHARED

    targets = []
    for index, token in enumerate(tokens):
        if not _is_name_token(token):
            continue
        if _unquote(token.text).lower() not in known:
            continue
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        # Already qualified (`t.col`), or a function call (`col(...)`) -- neither is a
        # reference to the column. One followed by a dot (`col.x`) is the head of a
        # struct path, and is qualified like a bare column.
        if previous is not None and previous.token_type is TokenType.DOT:
            continue
        if following is not None and following.token_type is TokenType.L_PAREN:
            continue
        targets.append(token)

    if Counter(_unquote(t.text).lower() for t in targets) != expected:
        return expression, AMBIGUOUS

    return _splice(expression, [
        (t.start, t.start,
         f"{column_reference(owners[_unquote(t.text).lower()], dialect)}.")
        for t in targets
    ]), QUALIFIED


def unqualify_metric(expression, dialect, datasets, fields=None):
    """Strip a dataset qualifier off every column in `expression`.

    The inverse of `qualify_metric`, used when exporting back to Solid, whose formulas are
    stored bare. `datasets` is the set of dataset names that may legitimately appear as a
    qualifier; any other qualifier -- a genuine table alias an author wrote by hand -- is
    preserved.

    Stripping is only safe while every bare name still means one column. It is not when
    two datasets the expression reads share a column name: `SUM(orders.amount) -
    SUM(returns.amount)` would become `SUM(amount) - SUM(amount)`. So the result is
    "ambiguous", and the text is left as written, when the same name is read from two
    datasets, or when `fields` -- each dataset's field names -- shows that a stripped
    name is also a field of another dataset the expression reads.

    Returns (expression, status) with the same vocabulary as `qualify_metric`, except that
    a successful edit reports "unqualified".
    """
    if not expression or not expression.strip():
        return expression, UNCHANGED
    known = {d.lower() for d in datasets}
    if not known:
        return expression, UNCHANGED

    tree = _parse(expression, dialect)
    tokens = _tokenize(expression, dialect)
    if tree is None or tokens is None:
        return expression, UNPARSED

    owned = {d.lower(): {n.lower() for n in names} for d, names in (fields or {}).items()}
    expected = _qualified_columns(tree, known, owned)
    if not expected:
        return expression, UNCHANGED

    edits, found = [], Counter()
    for index, token in enumerate(tokens):
        if not _is_name_token(token):
            continue
        if _unquote(token.text).lower() not in known:
            continue
        dot = tokens[index + 1] if index + 1 < len(tokens) else None
        name = tokens[index + 2] if index + 2 < len(tokens) else None
        if dot is None or dot.token_type is not TokenType.DOT:
            continue
        if name is None or not _is_name_token(name):
            continue
        previous = tokens[index - 1] if index else None
        # Inside a longer path, so not its leading qualifier.
        if previous is not None and previous.token_type is TokenType.DOT:
            continue
        qualifier, field = _unquote(token.text).lower(), _unquote(name.text).lower()
        after = tokens[index + 3] if index + 3 < len(tokens) else None
        # A longer path is `dataset.struct.member` only when the middle name is one of
        # the dataset's fields; otherwise (`db.table.column`) it starts with a schema.
        if (after is not None and after.token_type is TokenType.DOT
                and field not in owned.get(qualifier, ())):
            continue
        found[(qualifier, field)] += 1
        # Delete the qualifier and its dot; `end` is inclusive on a sqlglot token.
        edits.append((token.start, dot.end + 1, ""))

    if found != expected or _names_collide(found, owned):
        return expression, AMBIGUOUS
    return _splice(expression, edits), UNQUALIFIED


def _names_collide(references, owned):
    """True if stripping the qualifiers from `references`, a Counter of (dataset, name),
    would leave a bare name that more than one of those datasets could own."""
    readers = {}
    for dataset, name in references:
        readers.setdefault(name, set()).add(dataset)
    if any(len(datasets) > 1 for datasets in readers.values()):
        return True
    referenced = {dataset for dataset, _ in references}
    return any(
        name in owned.get(other, ())
        for dataset, name in references
        for other in referenced - {dataset}
    )


def referenced_datasets(expression, dialect, datasets, fields=None):
    """Return the dataset names a metric expression qualifies columns with.

    Used on export to reconstruct Solid's `metrics[].tables` when an Apache Ossie model
    carries no SOLID stash. Order follows `datasets`, so the result is deterministic.
    `fields` maps each dataset to its field names, so `orders.customer.id` counts as
    reading `orders` when `customer` is one of its fields.
    """
    tree = _parse(expression, dialect)
    if tree is None:
        return []
    known = {d.lower() for d in datasets}
    owned = {d.lower(): {n.lower() for n in names} for d, names in (fields or {}).items()}
    seen = {qualifier for qualifier, _ in _qualified_columns(tree, known, owned)}
    return [d for d in datasets if d.lower() in seen]

