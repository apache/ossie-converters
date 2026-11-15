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

"""Shared constants, YAML I/O, and typed accessors for the Ossie <-> Metric View converter.

The direction-specific logic lives in :mod:`ossie_databricks.ossie_to_metric_view` (export) and
:mod:`ossie_databricks.metric_view_to_ossie` (import). The public entry points and shared types are
re-exported through :mod:`ossie_databricks.ossie_converter`.

Messages are kept byte-identical to the Java converter, so a few helpers here reproduce Java's
string conversions (``null``, ``true``, ``[a, b]``) rather than Python's.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

import yaml

from ossie_databricks.ossie_converter import (
    MV_VERSION,
    OSSIE_VERSION,
    ConversionError,
    Notices,
)

__all__ = [
    "MV_VERSION",
    "OSSIE_VERSION",
]

# -- constants -------------------------------------------------------------
VENDOR = "DATABRICKS"
DIALECT_DATABRICKS = "DATABRICKS"
DIALECT_ANSI = "ANSI_SQL"
# Ossie's portable, ANSI-SQL-compatible dialect; treated as an ANSI_SQL-equivalent fallback.
DIALECT_OSSIE_SQL = "OSSIE_SQL_2026"
SYNONYM_LIMIT = 10
STASH_VERSION = 1
STASH_SOURCE_KEY = "source_dataset"
CARD_ONE_TO_MANY = "one_to_many"
CARD_MANY_TO_ONE = "many_to_one"
MAX_JOIN_NODES = 200

# Java's regex classes (\w, \b, \s, case folding) are ASCII-only by default; re.ASCII matches that.
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
SELECT_WITH_RE = re.compile(r"(select|with)\b", re.IGNORECASE | re.ASCII)


# -- Java-compatible string conversions -------------------------------------
def jstr(x: Any) -> str | None:
    """``String.valueOf`` semantics for parsed YAML values, with ``None`` kept as ``None``."""
    if x is None:
        return None
    return _java_string(x)


def js(x: Any) -> str:
    """The text Java string concatenation produces for ``x`` (``null`` for ``None``)."""
    return "null" if x is None else _java_string(x)


def _java_string(x: Any) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, list):
        return jlist(x)
    if isinstance(x, dict):
        return "{" + ", ".join(f"{js(k)}={js(v)}" for k, v in x.items()) + "}"
    return str(x)


def jlist(items: Iterable[Any]) -> str:
    """Java ``List.toString``: ``[a, b]``."""
    return "[" + ", ".join(js(i) for i in items) + "]"


def java_type_name(x: Any) -> str:
    """The Java simple class name SnakeYAML would have produced for a parsed value."""
    if isinstance(x, bool):
        return "Boolean"
    if isinstance(x, int):
        if -(2**31) <= x < 2**31:
            return "Integer"
        if -(2**63) <= x < 2**63:
            return "Long"
        return "BigInteger"
    if isinstance(x, float):
        return "Double"
    if isinstance(x, dict):
        return "LinkedHashMap"
    if isinstance(x, list):
        return "ArrayList"
    if isinstance(x, (datetime.date, datetime.datetime)):
        return "Date"
    if isinstance(x, bytes):
        return "byte[]"
    return type(x).__name__


def java_split(pattern: re.Pattern[str], s: str) -> list[str]:
    """Java ``String.split(regex)``: trailing empty strings are removed."""
    parts = pattern.split(s)
    if len(parts) == 1:
        return parts
    while parts and parts[-1] == "":
        parts.pop()
    return parts


# -- YAML I/O ----------------------------------------------------------------
class _Yaml12Loader(yaml.SafeLoader):
    """A safe loader with YAML 1.2 boolean semantics that rejects duplicate keys.

    Only true/false are booleans, NOT the YAML 1.1 tokens on/off/yes/no/y/n. A metric view join's
    bare ``on:`` key, and any "on"/"off" string value, must read back as a string rather than
    silently becoming a boolean (a YAML 1.1 reader would coerce it).
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key}",
                        key_node.start_mark,
                    )
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [
        (tag, rx)
        for tag, rx in resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:value")
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class _Dumper(yaml.SafeDumper):
    """Block-style writer matching the Java (Jackson YAML, quotes not minimized) output.

    String values are double-quoted so a string like "1.1" or a bool-like token is never re-parsed
    as a number/boolean; mapping keys stay plain. Anchors/aliases are never emitted.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def represent_str(self, data: str) -> yaml.ScalarNode:
        return self.represent_scalar("tag:yaml.org,2002:str", data, style='"')

    def represent_dict(self, data: dict[Any, Any]) -> yaml.MappingNode:
        value: list[tuple[yaml.Node, yaml.Node]] = []
        node = yaml.MappingNode("tag:yaml.org,2002:map", value, flow_style=False)
        for key, item in data.items():
            key_node = (
                self.represent_scalar("tag:yaml.org,2002:str", key)
                if isinstance(key, str)
                else self.represent_data(key)
            )
            value.append((key_node, self.represent_data(item)))
        return node


_Dumper.add_representer(str, _Dumper.represent_str)
_Dumper.add_representer(dict, _Dumper.represent_dict)
# Same YAML 1.2 resolution as the loader, so keys such as a join's `on` are written plain.
_Dumper.yaml_implicit_resolvers = {
    first: list(resolvers) for first, resolvers in _Yaml12Loader.yaml_implicit_resolvers.items()
}


def load_yaml(s: str) -> Any:
    return yaml.load(s, Loader=_Yaml12Loader)  # noqa: S506 - a SafeLoader subclass


def parse_yaml(s: str) -> Any:
    """Parse YAML text into a plain value (YAML 1.2 booleans, matching the converter)."""
    try:
        return load_yaml(s)
    except Exception as e:  # noqa: BLE001 - any parser failure is invalid input
        message = f"failed to parse YAML: {e}"
        raise ConversionError.invalid_input(message, message) from e


def write_yaml(obj: Any) -> str:
    """Serialize ``obj`` with the converter's writer; serializer errors propagate unchanged."""
    return yaml.dump(
        obj,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def dump_yaml(obj: Any) -> str:
    """Serialize a value to YAML using the converter's writer."""
    try:
        return write_yaml(obj)
    except Exception as e:  # noqa: BLE001
        raise ConversionError.internal_error(f"failed to serialize YAML: {e}") from e


# -- typed accessors over parsed YAML ----------------------------------------
def as_map(x: Any) -> dict[Any, Any]:
    return x if isinstance(x, dict) else {}


def as_list(x: Any) -> list[Any]:
    return x if isinstance(x, list) else []


def str_list(x: Any) -> list[str]:
    return [jstr(o) for o in as_list(x) if o is not None]  # type: ignore[misc]


# -- helpers -------------------------------------------------------------------
def is_simple_identifier(expr: Any) -> bool:
    return isinstance(expr, str) and IDENTIFIER_RE.fullmatch(expr.strip()) is not None


def replace_outside_literals(
    sql: str,
    pattern: re.Pattern[str],
    replacement: str | Callable[[re.Match[str]], str],
) -> str:
    """Applies ``pattern -> replacement`` to ``sql``, but only to the parts that are SQL code.

    Spans inside string literals (``'...'``, ``"..."``), backquoted identifiers, ``-- line``
    comments, and ``/* block */`` comments are copied through untouched.

    Measure expressions are rewritten to add or strip a fact qualifier, and a blind substitution
    over the raw text also rewrites any occurrence inside a literal: a measure such as
    ``SUM(IF(source.region = 'source.us', amt, 0))`` would silently become ``... = 'us'``, changing
    the predicate and therefore the measure's value. Only code spans may be rewritten.

    ``replacement`` is either a literal string or a callable receiving the match. Match offsets
    refer to the full SQL string. Replacement text is inserted literally.
    """
    replacer = replacement if callable(replacement) else (lambda _m: replacement)
    out: list[str] = []
    code_start = 0
    for start, end in _literal_spans(sql):
        # Rewrite the code span that precedes this literal/comment, then copy the span verbatim.
        _rewrite_literally(sql, code_start, start, pattern, replacer, out)
        out.append(sql[start:end])
        code_start = end
    _rewrite_literally(sql, code_start, len(sql), pattern, replacer, out)
    return "".join(out)


def find_outside_literals(sql: str, pattern: re.Pattern[str]) -> bool:
    """True when ``pattern`` matches the SQL code of ``sql``, ignoring literals and comments.

    Needed wherever a decision is made from an expression's text: a bare search also sees inside
    literals, so a column name that only occurs in a string (as in
    ``SUM(IF(region = 'us', amt, 0))``) reads as a reference to a column named ``us``.
    """
    spans = _literal_spans(sql)
    if not spans:
        return pattern.search(sql) is not None
    # Blank the literal/comment spans rather than removing them, so the surrounding code keeps its
    # token boundaries (the pattern may rely on \b or a look-around).
    code = list(sql)
    for start, end in spans:
        for i in range(start, end):
            code[i] = " "
    return pattern.search("".join(code)) is not None


def _literal_spans(sql: str) -> list[tuple[int, int]]:
    """Start (inclusive) and end (exclusive) offsets of every span that is not SQL code."""
    spans: list[tuple[int, int]] = []
    n = len(sql)
    i = 0
    while i < n:
        c = sql[i]
        skip_to = -1
        if c in ("'", '"', "`"):
            skip_to = _end_of_quoted(sql, i, c)
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i)
            skip_to = n if nl < 0 else nl
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            skip_to = n if end < 0 else end + 2
        if skip_to < 0:
            i += 1
            continue
        spans.append((i, skip_to))
        i = skip_to
    return spans


def _end_of_quoted(sql: str, start: int, quote: str) -> int:
    """Index just past the quoted span starting at ``start``; handles doubled-quote escapes."""
    n = len(sql)
    i = start + 1
    while i < n:
        c = sql[i]
        if c == "\\" and quote != "`" and i + 1 < n:
            i += 2
            continue
        if c == quote:
            # A doubled quote is an escaped quote, not the end of the span.
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    # Unterminated literal: treat the remainder as part of the span rather than rewriting it.
    return n


def _rewrite_literally(
    sql: str,
    start: int,
    end: int,
    pattern: re.Pattern[str],
    replacer: Callable[[re.Match[str]], str],
    out: list[str],
) -> None:
    # Search a bounded copy of the region, as Java's opaque region bounds do, then translate the
    # match back so the replacer sees offsets into the full SQL string.
    pos = start
    for m in pattern.finditer(sql, start, end):
        out.append(sql[pos : m.start()])
        out.append(replacer(m))
        pos = m.end()
    out.append(sql[pos:end])


def qualifier_chain_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """A pattern matching a maximal dotted run of ``names`` that qualifies a column reference.

    ``parent.child.`` -- group 1 holds the run without its trailing dot. None when ``names`` is
    empty.

    The whole run is matched at once, rather than one name at a time, so a rewrite driven by this
    pattern can never re-match a qualifier it has just produced: rewriting ``customer.region.``
    name-by-name would see the ``region.`` inside its own output and qualify it a second time. The
    required trailing dot keeps a column that merely shares a name with a dataset
    (``SUM(customer.region)``) from being read as a qualifier.
    """
    # Longest name first: regex alternation is ordered, so at a given position the longest name
    # wins (`nation_x` must never match as `nation`). sorted() is stable, like Java's List.sort.
    ordered = sorted(names, key=len, reverse=True)
    if not ordered:
        return None
    one = "(?:" + "|".join(re.escape(name) for name in ordered) + ")"
    return re.compile(r"\b(" + one + r"(?:\." + one + r")*)\.", re.ASCII)


def rewrite_qualifiers(
    expr: str, chain: re.Pattern[str] | None, resolve: Callable[[str], str | None]
) -> str:
    """Rewrites each qualifier run ``chain`` matches by mapping the run's LAST name.

    The name closest to the column is the one being addressed, as ``resolve_column`` treats a
    dimension's alias path. A run whose leaf ``resolve`` does not know is left alone. Literals and
    comments are skipped, and the pass is single, so the result is independent of the order the
    names happen to be in.
    """
    if chain is None:
        return expr

    def replace(m: re.Match[str]) -> str:
        leaf = last_identifier(m.group(1))
        resolved = None if leaf is None else resolve(leaf)
        return m.group() if resolved is None else resolved + "."

    return replace_outside_literals(expr, chain, replace)


def require(obj: dict[Any, Any], key: str, what: str) -> Any:
    """Presence is tested by key, so a legitimately falsy non-string value (0, false) is returned.

    A missing key, a null, or an empty/whitespace-only string is rejected.
    """
    if obj.get(key) is None:
        raise ConversionError(f"{what} is missing required '{key}'")
    value = obj[key]
    if isinstance(value, str) and value.strip() == "":
        raise ConversionError(f"{what} has an empty '{key}'")
    return value


def require_str(obj: dict[Any, Any], key: str, what: str) -> str:
    """Like :func:`require`, but the value must be a string."""
    v = require(obj, key, what)
    if isinstance(v, str):
        return v
    raise ConversionError(f"{what}: '{key}' must be a string, got {java_type_name(v)}")


def validate_source(source: Any, dataset_name: str) -> str:
    s = "" if source is None else js(source).strip()
    if s == "":
        raise ConversionError(f"Dataset '{dataset_name}': missing/empty 'source'")
    if SELECT_WITH_RE.match(s):
        return s
    parts = s.split(".")
    ok = len(parts) == 3 and all(p != "" and not _contains_whitespace(p) for p in parts)
    if ok:
        return s
    raise ConversionError(
        f"Dataset '{dataset_name}': source '{js(source)}' must be a 3-part "
        "catalog.schema.table identifier or a SELECT/WITH subquery"
    )


def _contains_whitespace(p: str) -> bool:
    return any(ch.isspace() for ch in p)


def pick_expression(ossie_expression: Any, scope: str) -> str | None:
    """The SQL text to export for an Ossie expression, or None when no supported entry exists.

    Prefer native SQL (DATABRICKS), then the primary ``expression.sql``, then the legacy ANSI_SQL
    and OSSIE_SQL_2026 entries. Keep the raw value so a non-string chosen expression is rejected
    instead of coerced.
    """
    if ossie_expression is None:
        return None
    if not isinstance(ossie_expression, dict):
        reason = f"{scope}: 'expression' must be a mapping"
        raise ConversionError.invalid_input(reason, reason)
    expression = ossie_expression
    dialect_values = expression.get("dialects")
    if dialect_values is not None and not isinstance(dialect_values, list):
        reason = f"{scope}: 'expression.dialects' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    dialects: dict[str | None, Any] = {}
    for index, d in enumerate(as_list(dialect_values)):
        if not isinstance(d, dict):
            reason = f"{scope}: 'expression.dialects[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        dialects[jstr(d.get("dialect"))] = d.get("expression")
    chosen = None
    for candidate in (
        dialects.get(DIALECT_DATABRICKS),
        expression.get("sql"),
        dialects.get(DIALECT_ANSI),
        dialects.get(DIALECT_OSSIE_SQL),
    ):
        if truthy(candidate) and not (isinstance(candidate, str) and candidate.strip() == ""):
            chosen = candidate
            break
    if chosen is not None and not isinstance(chosen, str):
        raise ConversionError(f"expression must be a string, got {java_type_name(chosen)}")
    # None when no supported dialect is present -> the caller warns and skips.
    return chosen


def truthy(v: Any) -> bool:
    """Emptiness test used throughout the converter for optional YAML values.

    None, the empty string, an empty list/map, numeric zero, and boolean false are all treated as
    absent; everything else is present. Used for the expression fallthrough (so an empty or absent
    DATABRICKS expr falls through to the next entry) and for optional-field mapping (so an empty
    ``comment``/``synonyms`` is dropped rather than emitted as an empty value).
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (str, list, dict, set, tuple)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return float(v) != 0.0
    return True


def synonyms_of(ai_context: Any) -> list[str]:
    if isinstance(ai_context, dict):
        return str_list(ai_context.get("synonyms"))
    return []


def merge_description(description: Any, ai_context: Any) -> str | None:
    desc = jstr(description)
    if isinstance(ai_context, str) and ai_context.strip() != "":
        # When both are present they are joined with a newline; an empty (or null) description
        # returns the ai_context alone rather than prepending a stray newline.
        return f"{desc}\n{ai_context}" if desc else ai_context
    return desc


def _custom_extensions(obj: dict[Any, Any]) -> list[dict[Any, Any]]:
    value = obj.get("custom_extensions")
    if value is None:
        return []
    if not isinstance(value, list):
        reason = "'custom_extensions' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    extensions = []
    for index, extension in enumerate(value):
        if not isinstance(extension, dict):
            reason = f"'custom_extensions[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        extensions.append(extension)
    return extensions


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"Duplicate field '{key}'")
        out[key] = value
    return out


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"Non-standard token '{name}'")


def _read_json(data: str) -> Any:
    """Strict JSON: duplicate keys, trailing tokens, and NaN/Infinity are all rejected."""
    return json.loads(
        data,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def read_stash(obj: dict[Any, Any], notices: Notices, scope: str) -> dict[str, Any]:
    stash: dict[str, Any] | None = None
    saw_extra = False
    for ext in _custom_extensions(obj):
        if jstr(ext.get("vendor_name")) != VENDOR:
            continue
        if stash is not None:
            # First-wins: keep the first DATABRICKS entry and ignore later ones, but warn so a
            # dropped entry is not lost silently.
            saw_extra = True
            continue
        # A null or empty-string `data` is treated as an empty object rather than a parse error.
        data = ext.get("data")
        if data is not None and not isinstance(data, str):
            reason = "DATABRICKS custom_extensions data must be a string"
            raise ConversionError.invalid_input(reason, reason)
        if not data:
            data = "{}"
        try:
            parsed = _read_json(data)
        except ValueError as e:
            reason = f"DATABRICKS custom_extensions data is not valid JSON: {e}"
            raise ConversionError.invalid_input(reason, reason) from e
        if not isinstance(parsed, dict):
            reason = "DATABRICKS custom_extensions data must be a JSON object"
            raise ConversionError.invalid_input(reason, reason)
        parsed.pop("_v", None)
        stash = parsed
    if saw_extra:
        notices.warn(
            scope,
            "found more than one DATABRICKS custom_extensions entry; "
            "using the first and ignoring the rest",
        )
    return stash if stash is not None else {}


def cube_integer_sum_metadata(metric: dict[Any, Any]) -> dict[str, Any]:
    """Reads Cube's aggregate grain hint without imposing validation on other foreign metadata."""
    for extension in _custom_extensions(metric):
        data = extension.get("data")
        if extension.get("vendor_name") == "CUBE" and isinstance(data, str):
            try:
                parsed = as_map(_read_json(data))
            except ValueError:
                # Foreign metadata is opaque. A malformed entry does not hide later hints.
                continue
            if str_list(parsed.get("integer_sum_grain")):
                return parsed
    return {}


def foreign_vendor_extensions(obj: dict[Any, Any]) -> list[dict[Any, Any]]:
    return [e for e in _custom_extensions(obj) if jstr(e.get("vendor_name")) != VENDOR]


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def write_stash(obj: dict[Any, Any], data: dict[str, Any], vendor: str = VENDOR) -> None:
    """Attach a ``vendor`` custom_extensions entry holding ``data``; no-op when empty.

    The blob is pure ASCII JSON with ``": "`` / ``", "`` separators and lowercase ``\\uXXXX``
    escapes, as in ``{"_v": 1, "filter": "x"}`` -- the byte format the fixtures pin.
    """
    if not data:
        return
    payload = {"_v": STASH_VERSION, **data}
    try:
        blob = json.dumps(payload, ensure_ascii=True, default=_json_default)
    except (TypeError, ValueError) as e:
        raise ConversionError.internal_error(f"failed to serialize stash: {e}") from e
    exts = obj.setdefault("custom_extensions", [])
    for ext in exts:
        if isinstance(ext, dict) and jstr(ext.get("vendor_name")) == vendor:
            ext["data"] = blob
            return
    exts.append({"vendor_name": vendor, "data": blob})


def last_identifier(source: Any) -> str | None:
    """Last dotted part of a table reference: ``samples.tpch.lineitem`` -> ``lineitem``.

    Trim the whole reference, take the final dotted segment, then strip any surrounding backticks
    (so ``cat.sch.`t``` -> ``t``).
    """
    if source is None:
        return None
    s = js(source).strip()
    last = s[s.rfind(".") + 1 :]
    return last.strip("`")
