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

"""
VQL parser for Denodo Metric View and Association statements.

Parses a reduced subset of VQL sufficient for Metric View conversion:
  - CREATE [OR REPLACE] ASSOCIATION statements
  - CREATE [OR REPLACE] METRIC VIEW statements

Expressions are stored as raw strings (no deep AST).
"""

from __future__ import annotations

import re

from ossie_denodo.models import (
    AssociationModel,
    AssociationRefModel,
    DimensionFieldModel,
    DimensionGroupModel,
    EndpointModel,
    MetricFieldModel,
    MetricViewModel,
    SourceModel,
)

AS_REGEX_PATTERN = r'\s*AS\s+'


class VQLParseError(Exception):
    """Raised when a VQL string cannot be parsed."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_vql(vql_str: str) -> tuple[list[AssociationModel], MetricViewModel]:
    """
    Parses a VQL string containing zero or more CREATE ASSOCIATION statements
    followed by one CREATE METRIC VIEW statement.

    Returns:
        A tuple (associations, metric_view).

    Raises:
        VQLParseError: If the VQL cannot be parsed or no METRIC VIEW is found.
    """
    # Clean up comments that might interfere with splitting
    vql_str = re.sub(r'--.*$', '', vql_str, flags=re.MULTILINE)

    statements = _split_statements(vql_str)

    associations: list[AssociationModel] = []
    metric_view: MetricViewModel | None = None

    connectVDBStmt = None
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue

        # If it's a long block that might contain multiple statements without semicolons
        # we try to see if it starts with CREATE ASSOCIATION but also contains CREATE METRIC VIEW
        if re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?ASSOCIATION\b', stmt, re.I) and \
           re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?METRIC\s+VIEW\b', stmt, re.I):
            # This is likely a case where statements weren't split correctly.
            # We can try a more aggressive split based on the CREATE keywords if needed,
            # but let's first try to handle the specific case.
            parts = re.split(r'(?=CREATE\s+(?:OR\s+REPLACE\s+)?(?:ASSOCIATION|METRIC\s+VIEW)\b)', stmt, flags=re.I)
            for part in parts:
                part = part.strip()
                if not part: continue
                upper_part = part.upper()
                if re.match(r'CREATE\s+(OR\s+REPLACE\s+)?ASSOCIATION\b', upper_part):
                    associations.append(_parse_association(connectVDBStmt, part))
                elif re.match(r'CREATE\s+(OR\s+REPLACE\s+)?METRIC\s+VIEW\b', upper_part):
                    if metric_view is not None:
                        raise VQLParseError("Multiple METRIC VIEW statements found; only one is supported")
                    metric_view = _parse_metric_view(part)
            continue

        upper = stmt.upper()
        if re.match(r'CONNECT\s+DATABASE\b', upper):
            connectVDBStmt = stmt
        elif re.match(r'CREATE\s+(OR\s+REPLACE\s+)?ASSOCIATION\b', upper):
            associations.append(_parse_association(connectVDBStmt,stmt))
        elif re.match(r'CREATE\s+(OR\s+REPLACE\s+)?METRIC\s+VIEW\b', upper):
            if metric_view is not None:
                raise VQLParseError("Multiple METRIC VIEW statements found; only one is supported")
            metric_view = _parse_metric_view(stmt)
        elif re.match(r'\s*CLOSE\b\s*;?', upper):
            connectVDBStmt=None

    if metric_view is None:
        raise VQLParseError("No CREATE METRIC VIEW statement found in VQL input")

    return associations, metric_view


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------

def _split_statements(vql_str: str) -> list[str]:
    """Splits VQL text into individual statements on top-level semicolons."""
    statements = []
    depth = 0
    current: list[str] = []
    i = 0
    in_single_quote = False
    in_double_quote = False

    while i < len(vql_str):
        ch = vql_str[i]

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif not in_single_quote and not in_double_quote:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ';' and depth == 0:
                statements.append(''.join(current).strip())
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    tail = ''.join(current).strip()
    if tail:
        statements.append(tail)

    return statements


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

def _read_identifier(text: str, pos: int) -> tuple[str, int]:
    """
    Reads one identifier (possibly quoted with double quotes) starting at pos.
    Returns (raw_token, new_pos).
    """
    text_len = len(text)
    while pos < text_len and text[pos].isspace():
        pos += 1

    if pos >= text_len:
        return '', pos

    if text[pos] == '"':
        end = text.index('"', pos + 1) + 1
        return text[pos:end], end
    else:
        start = pos
        while pos < text_len and text[pos] not in (' ', '\t', '\n', '\r', ',', '(', ')', ';'):
            pos += 1
        return text[start:pos], pos


_IDENT_PATTERN = r'("(?:[^"]|"")*"|\w+)'


# ---------------------------------------------------------------------------
# Association parser
# ---------------------------------------------------------------------------

_CONNECT_DB_RE = re.compile(
    r'CONNECT\s+DATABASE\s+([^\s;]+)\s*;?',
    re.IGNORECASE,
)

_ASSOC_HEADER_RE = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?ASSOCIATION\s+' + _IDENT_PATTERN + r'(\s+REFERENTIAL\s+CONSTRAINT)?',
    re.IGNORECASE,
)
_FOLDER_RE = re.compile(r"FOLDER\s*=\s*'([^']*)'", re.IGNORECASE)
_ENDPOINT_RE = re.compile(
    r'ENDPOINT\s+' + _IDENT_PATTERN + r'\s+' + _IDENT_PATTERN + r'(\s+PRINCIPAL)?\s+\(([^)]+)\)',
    re.IGNORECASE,
)
_MAPPING_RE = re.compile(r'ADD\s+MAPPING\s+([^=\s][^=]*)=([^=\s]\S*)', re.IGNORECASE)


def _parse_association(vdbConnectStmt: str, stmt: str) -> AssociationModel:

    vdb_m = None
    if vdbConnectStmt:
        connect_m = _CONNECT_DB_RE.search(vdbConnectStmt)
        vdb_m = connect_m.group(1)

    """Parses a single CREATE [OR REPLACE] ASSOCIATION statement."""
    m = _ASSOC_HEADER_RE.search(stmt)
    if not m:
        raise VQLParseError(f"Cannot parse ASSOCIATION header in: {stmt[:80]!r}")

    name = m.group(1)
    referential_constraint = bool(m.group(2))

    folder_m = _FOLDER_RE.search(stmt)
    folder = folder_m.group(1) if folder_m else None

    endpoints: list[EndpointModel] = []
    for em in _ENDPOINT_RE.finditer(stmt):
        endpoints.append(EndpointModel(
            role_name=em.group(1),
            view_name=em.group(2),
            is_principal=bool(em.group(3)),
            cardinality=em.group(4).strip(),
        ))

    mappings: list[tuple[str, str]] = []
    for mm in _MAPPING_RE.finditer(stmt):
        mappings.append((mm.group(1).strip(), mm.group(2).strip()))

    return AssociationModel(
        name=name,
        vdb=vdb_m,
        folder=folder,
        referential_constraint=referential_constraint,
        endpoints=endpoints,
        mappings=mappings,
    )


# ---------------------------------------------------------------------------
# Metric View parser
# ---------------------------------------------------------------------------

_MV_HEADER_RE = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?METRIC\s+VIEW\s+' + _IDENT_PATTERN,
    re.IGNORECASE,
)
_MV_DESCRIPTION_RE = re.compile(r"DESCRIPTION\s*=\s*'([^']*)'", re.IGNORECASE)
_MV_TAGS_RE = re.compile(r'TAGS\s*\(([^)]*)\)', re.IGNORECASE)
_MV_CONTEXT_RE = re.compile(r"CONTEXT\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)


def _parse_metric_view(stmt: str) -> MetricViewModel:
    """Parses a single CREATE [OR REPLACE] METRIC VIEW statement."""
    m = _MV_HEADER_RE.search(stmt)
    if not m:
        raise VQLParseError(f"Cannot parse METRIC VIEW header in: {stmt[:80]!r}")

    name = m.group(1)

    folder_m = _FOLDER_RE.search(stmt)
    folder = folder_m.group(1) if folder_m else None

    desc_m = _MV_DESCRIPTION_RE.search(stmt)
    description = desc_m.group(1) if desc_m else None

    tags_m = _MV_TAGS_RE.search(stmt)
    tags: list[str] = []
    if tags_m:
        tags = [t.strip() for t in tags_m.group(1).split(',') if t.strip()]

    sources = _parse_sources_block(stmt)
    associations = _parse_associations_block(stmt)
    dimensions = _parse_dimensions_block(stmt)
    metrics = _parse_metrics_block(stmt)
    context = _parse_context_block(stmt)
    filter_condition = _extract_block(stmt, 'FILTER')

    return MetricViewModel(
        name=name,
        folder=folder,
        description=description,
        tags=tags,
        sources=sources,
        associations=associations,
        dimensions=dimensions,
        metrics=metrics,
        context=context,
        filter_condition=filter_condition,
    )


# ---------------------------------------------------------------------------
# Block extraction helpers
# ---------------------------------------------------------------------------

def _extract_block(stmt: str, keyword: str) -> str | None:
    """
    Extracts the content inside the parentheses of a top-level clause like
    SOURCES (...), ASSOCIATIONS (...), DIMENSIONS (...), METRICS (...).
    Returns the inner content string, or None if the keyword is not found.
    """
    pattern = re.compile(r'\b' + re.escape(keyword) + r'\s*\(', re.IGNORECASE)
    m = pattern.search(stmt)
    if not m:
        return None

    start = m.end()
    depth = 1
    i = start
    while i < len(stmt) and depth > 0:
        ch = stmt[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        i += 1

    return stmt[start:i - 1]


# ---------------------------------------------------------------------------
# SOURCES block
# ---------------------------------------------------------------------------

_SOURCE_LINE_RE = re.compile(
    _IDENT_PATTERN + r'\s+AS\s+(' + _IDENT_PATTERN + r'(?:\.' + _IDENT_PATTERN + r')?)',
    re.IGNORECASE,
)


def _parse_sources_block(stmt: str) -> list[SourceModel]:
    block = _extract_block(stmt, 'SOURCES')
    if not block:
        return []

    sources: list[SourceModel] = []
    for line_m in _SOURCE_LINE_RE.finditer(block):
        alias = line_m.group(1)
        raw_source = line_m.group(2)
        if '.' in raw_source:
            database, view = raw_source.split('.', 1)
        else:
            database, view = None, raw_source
        sources.append(SourceModel(alias=alias, database=database, view=view))

    return sources


# ---------------------------------------------------------------------------
# ASSOCIATIONS block
# ---------------------------------------------------------------------------

def _parse_associations_block(stmt: str) -> list[AssociationRefModel]:
    block = _extract_block(stmt, 'ASSOCIATIONS')
    if not block:
        return []

    refs: list[AssociationRefModel] = []
    _JOIN_TYPES = {'LEFT', 'RIGHT', 'INNER', 'FULL'}

    for raw_line in block.split('\n'):
        line = raw_line.strip().rstrip(',')
        if not line:
            continue

        tokens: list[str] = []
        pos = 0
        while pos < len(line):
            tok, pos = _read_identifier(line, pos)
            if tok:
                tokens.append(tok)
            while pos < len(line) and line[pos] in (' ', '\t', ','):
                pos += 1

        if len(tokens) < 3:
            continue

        from_alias = tokens[0]
        to_alias = tokens[1]
        assoc_name = tokens[2]
        join_type = tokens[3].upper() if len(tokens) >= 4 and tokens[3].upper() in _JOIN_TYPES else None

        refs.append(AssociationRefModel(
            from_alias=from_alias,
            to_alias=to_alias,
            association_name=assoc_name,
            join_type=join_type,
        ))

    return refs


# ---------------------------------------------------------------------------
# DIMENSIONS block
# ---------------------------------------------------------------------------

def _parse_dimensions_block(stmt: str) -> list[DimensionGroupModel]:
    block = _extract_block(stmt, 'DIMENSIONS')
    if not block:
        return []
    return _parse_dimension_entries(block)


def _parse_dimension_entries(block: str) -> list[DimensionGroupModel]:
    """
    Parses the content of a DIMENSIONS block into DimensionGroupModel entries.
    Handles grouped: group_name(field AS expr, ...) and ungrouped: field AS expr.
    """
    groups: list[DimensionGroupModel] = []
    i = 0
    block = block.strip()
    length = len(block)

    while i < length:
        while i < length and block[i] in (' ', '\t', '\n', '\r', ','):
            i += 1
        if i >= length:
            break

        name_tok, i = _read_identifier(block, i)
        if not name_tok:
            break

        while i < length and block[i] in (' ', '\t', '\n', '\r'):
            i += 1

        if i < length and block[i] == '(':
            group_name = name_tok
            inner, i = _extract_inner(block, i)
            fields = _parse_field_list(inner)
            groups.append(DimensionGroupModel(group_name=group_name, fields=fields))
        else:
            as_match = re.match(AS_REGEX_PATTERN, block[i:], re.IGNORECASE)
            if not as_match:
                continue
            i += as_match.end()
            expr, i = _read_expression(block, i)
            field_name = name_tok
            groups.append(DimensionGroupModel(
                group_name=None,
                fields=[DimensionFieldModel(name=field_name, expression=expr.strip())],
            ))

    return groups


def _extract_inner(text: str, pos: int) -> tuple[str, int]:
    """Extracts content inside matching parentheses starting at pos."""
    assert text[pos] == '('
    depth = 1
    i = pos + 1
    while i < len(text) and depth > 0:
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
        i += 1
    return text[pos + 1:i - 1], i


def _read_expression(text: str, pos: int) -> tuple[str, int]:
    """
    Reads a raw expression from pos until a top-level comma or end of string.
    Respects nested parentheses and string literals (single and double quotes).
    """
    depth = 0
    start = pos
    length = len(text)
    while pos < length:
        ch = text[pos]
        if ch in ('"', "'"):
            # Skip over string literal
            quote = ch
            pos += 1
            while pos < length and text[pos] != quote:
                pos += 1
            pos += 1  # skip closing quote
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            if depth == 0:
                break
            depth -= 1
        elif ch == ',' and depth == 0:
            break
        pos += 1
    return text[start:pos], pos


def _parse_field_list(block: str) -> list[DimensionFieldModel]:
    """Parses a comma-separated list of 'field AS expression' entries."""
    fields: list[DimensionFieldModel] = []
    i = 0
    block = block.strip()
    length = len(block)

    while i < length:
        while i < length and block[i] in (' ', '\t', '\n', '\r', ','):
            i += 1
        if i >= length:
            break

        name_tok, i = _read_identifier(block, i)
        if not name_tok:
            break

        as_match = re.match(AS_REGEX_PATTERN, block[i:], re.IGNORECASE)
        if not as_match:
            continue
        i += as_match.end()

        expr, i = _read_expression(block, i)
        fields.append(DimensionFieldModel(
            name=name_tok,
            expression=expr.strip(),
        ))

    return fields


# ---------------------------------------------------------------------------
# METRICS block
# ---------------------------------------------------------------------------

def _parse_metrics_block(stmt: str) -> list[MetricFieldModel]:
    block = _extract_block(stmt, 'METRICS')
    if not block:
        return []

    metrics: list[MetricFieldModel] = []
    i = 0
    block = block.strip()
    length = len(block)

    while i < length:
        while i < length and block[i] in (' ', '\t', '\n', '\r', ','):
            i += 1
        if i >= length:
            break

        name_tok, i = _read_identifier(block, i)
        if not name_tok:
            break

        as_match = re.match(AS_REGEX_PATTERN, block[i:], re.IGNORECASE)
        if not as_match:
            continue
        i += as_match.end()

        expr, i = _read_expression(block, i)
        metrics.append(MetricFieldModel(
            name=name_tok,
            expression=expr.strip(),
        ))

    return metrics


# ---------------------------------------------------------------------------
# CONTEXT block
# ---------------------------------------------------------------------------

_CONTEXT_KV_RE = re.compile(r"'([^']+)'\s*=\s*'([^']*)'")


def _parse_context_block(stmt: str) -> dict[str, str]:
    ctx_m = _MV_CONTEXT_RE.search(stmt)
    if not ctx_m:
        return {}
    inner = ctx_m.group(1)
    return {m.group(1): m.group(2) for m in _CONTEXT_KV_RE.finditer(inner)}
