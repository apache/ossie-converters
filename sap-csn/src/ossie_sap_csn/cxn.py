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

"""Translate between CSN expressions (CXN) and ANSI SQL text.

CXN is the JSON expression notation CSN uses for calculated elements and view
columns: ``{"ref": [...]}``, ``{"val": ...}``, ``{"func": ..., "args": [...]}``
and ``{"xpr": [...]}`` (a token list in which a nested ``xpr`` is a
parenthesised group). Only the subset that has a faithful SQL rendering is
handled; anything else raises :class:`UnsupportedExpression` so the caller can
report it instead of guessing.
"""

import math
import re
from typing import Any, Callable, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from ossie_sap_csn.datatypes import CAST_FACETS, CDS_TO_SQL_TYPE, SQL_TYPE_TO_CDS


class UnsupportedExpression(Exception):
    """The expression uses a construct this converter does not translate."""


# Resolves a CXN path `["assoc", "col"]` (more than one segment) to SQL text.
PathResolver = Callable[[list[str]], str]


def _no_paths(path: list[str]) -> str:
    raise UnsupportedExpression(f"path expression {'.'.join(path)!r} cannot be resolved")


# --------------------------------------------------------------------------------------
# CXN -> SQL
# --------------------------------------------------------------------------------------


_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def quote_identifier(name: str) -> str:
    """``name`` as an SQL identifier, double-quoted unless it is a plain non-keyword word."""
    if _PLAIN_IDENTIFIER.fullmatch(name):
        try:
            tokens = sqlglot.tokenize(name)
        except SqlglotError:
            tokens = []
        if len(tokens) == 1 and tokens[0].token_type == TokenType.VAR:
            return name
    return '"' + name.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        raise UnsupportedExpression(f"literal {value!r} has no SQL rendering")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise UnsupportedExpression(f"literal {value!r} has no SQL rendering")


# Tokens of an `xpr` list that have an SQL rendering; anything else is refused.
_KEYWORDS = frozenset(
    {"and", "or", "not", "is", "null", "in", "like", "escape", "between", "case", "when", "then", "else", "end", "distinct"}
)
_OPERATORS = frozenset({"=", "!=", "<>", "<", ">", "<=", ">=", "+", "-", "*", "/", "||"})
# Keys a node may carry besides the one that says what it is (`ref`, `val`, ...).
_NODE_EXTRAS = {"val": {"literal"}, "func": {"args"}}


def _cast_sql(cast: Any) -> str:
    """The SQL type for a CXN ``cast`` object, with its facets as type parameters."""
    cds = cast.get("type") if isinstance(cast, dict) else None
    if not isinstance(cds, str) or cds not in CDS_TO_SQL_TYPE:
        raise UnsupportedExpression(f"cast to {cds!r}")
    facets = CAST_FACETS.get(cds, ())
    unknown = sorted(set(cast) - {"type", *facets})
    given = [f for f in facets if f in cast]
    values = [cast[f] for f in given]
    # Facets are positional in SQL, so `scale` cannot be given without `precision`.
    if unknown or given != list(facets[: len(given)]) or any(type(v) is not int or v < 0 for v in values):
        raise UnsupportedExpression(f"cast to {cds} with {', '.join(unknown or given)}")
    sql_type = CDS_TO_SQL_TYPE[cds]
    return f"{sql_type}({', '.join(map(str, values))})" if values else sql_type


def _ref_to_sql(ref: Any, resolve_path: PathResolver) -> str:
    if not isinstance(ref, list) or not ref or not all(isinstance(s, str) for s in ref):
        # A segment may be an object carrying a filter or arguments.
        raise UnsupportedExpression("reference with a filtered or parameterised segment")
    if ref[0] in ("$self", "$projection"):
        ref = ref[1:]
    if not ref:
        raise UnsupportedExpression("bare $self reference")
    if ref[0].startswith("$"):
        raise UnsupportedExpression(f"{ref[0]!r} has no SQL rendering")
    if len(ref) == 1:
        return quote_identifier(ref[0])
    return resolve_path(list(ref))


def _token(token: str) -> str:
    if token in _OPERATORS:
        return token
    words = token.lower().split()
    if words and all(w in _KEYWORDS for w in words):
        return " ".join(words).upper()
    raise UnsupportedExpression(f"token {token!r} has no SQL rendering")


def _tokens(tokens: Any, resolve_path: PathResolver) -> str:
    if not isinstance(tokens, list):
        raise UnsupportedExpression(f"expression token list {tokens!r} is not a list")
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, str):
            parts.append(_token(token))
        else:
            parts.append(_node(token, resolve_path, nested_group=True))
    return " ".join(parts)


def _node(node: Any, resolve_path: PathResolver, nested_group: bool = False) -> str:
    if not isinstance(node, dict):
        raise UnsupportedExpression(f"unexpected expression node {node!r}")
    if "cast" in node:
        sql_type = _cast_sql(node["cast"])
        inner = {k: v for k, v in node.items() if k != "cast"}
        return f"CAST({_node(inner, resolve_path)} AS {sql_type})"
    # Anything more (a window function's `xpr` on a `func`, a `param` flag on a `ref`)
    # changes what the node means, so it is refused rather than rendered without it.
    kinds = [k for k in ("func", "ref", "val", "xpr", "list") if k in node]
    if kinds:
        extras = {k for k in node if not k.startswith("$")} - {kinds[0]} - _NODE_EXTRAS.get(kinds[0], set())
        if extras:
            raise UnsupportedExpression(f"{kinds[0]} node with {', '.join(sorted(extras))}")
    if "ref" in node:
        return _ref_to_sql(node["ref"], resolve_path)
    if "val" in node:
        return _literal(node["val"])
    if "xpr" in node:
        body = _tokens(node["xpr"], resolve_path)
        return f"({body})" if nested_group else body
    if "list" in node:
        if not isinstance(node["list"], list):
            raise UnsupportedExpression(f"list {node['list']!r} is not a list")
        return "(" + ", ".join(_node(i, resolve_path) for i in node["list"]) + ")"
    if "func" in node:
        args = node.get("args", [])
        if not isinstance(node["func"], str):
            raise UnsupportedExpression(f"function name {node['func']!r}")
        if not isinstance(args, list):
            raise UnsupportedExpression(f"function {node['func']!r} with named arguments")
        rendered = []
        for arg in args:
            if arg == "*":
                rendered.append("*")
            elif isinstance(arg, dict) and "xpr" in arg:
                # An argument-level xpr is the argument itself (`DISTINCT x`), not a group.
                rendered.append(_tokens(arg["xpr"], resolve_path))
            else:
                rendered.append(_node(arg, resolve_path))
        return f"{node['func'].upper()}({', '.join(rendered)})"
    raise UnsupportedExpression(f"expression node with keys {sorted(node)}")


def cxn_to_sql(node: Any, resolve_path: PathResolver = _no_paths) -> str:
    """Render one CXN expression as ANSI SQL text."""
    return _node(node, resolve_path)


# --------------------------------------------------------------------------------------
# SQL -> CXN
# --------------------------------------------------------------------------------------

_BINARY_OPS: dict[type, str] = {
    exp.Add: "+",
    exp.Sub: "-",
    exp.Mul: "*",
    exp.Div: "/",
    exp.EQ: "=",
    exp.NEQ: "!=",
    exp.GT: ">",
    exp.GTE: ">=",
    exp.LT: "<",
    exp.LTE: "<=",
    exp.And: "and",
    exp.Or: "or",
    exp.DPipe: "||",
}

def _cast_type(to: exp.DataType) -> dict[str, Any]:
    """The CXN ``cast`` object for an SQL type; the inverse of :func:`_cast_sql`."""
    name = to.this.name if isinstance(to.this, exp.DataType.Type) else None
    params: list[int] = []
    for param in to.expressions:
        value = param.this if isinstance(param, exp.DataTypeParam) else param
        if not (isinstance(value, exp.Literal) and value.is_int):
            raise UnsupportedExpression(f"cast to {to.sql()}")
        params.append(int(value.this))
    if name == "TIMESTAMP" and params == [0]:
        name, params = "TIMESTAMP(0)", []
    cds = SQL_TYPE_TO_CDS.get(name or "")
    facets = CAST_FACETS.get(cds or "", ())
    if cds is None or len(params) > len(facets):
        raise UnsupportedExpression(f"cast to {to.sql()}")
    return {"type": cds, **dict(zip(facets, params))}


def parse_sql(sql: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql)
    except SqlglotError as exc:
        # sqlglot underlines the error position with ANSI escapes; keep them out of issue text.
        raise UnsupportedExpression(f"not parseable as SQL: {_ANSI_ESCAPE.sub('', str(exc))}") from exc


# Resolves a SQL column `table.col` to a CXN `ref` list.
ColumnResolver = Callable[[Optional[str], str], list[str]]


def _to_cxn(node: exp.Expression, resolve_column: ColumnResolver) -> Any:
    if isinstance(node, exp.Paren):
        inner = _to_cxn(node.this, resolve_column)
        return inner if isinstance(inner, dict) and "xpr" in inner else {"xpr": [inner]}
    if isinstance(node, exp.Column):
        table = node.table or None
        return {"ref": resolve_column(table, node.name)}
    if isinstance(node, exp.Null):
        return {"val": None}
    if isinstance(node, exp.Boolean):
        return {"val": bool(node.this)}
    if isinstance(node, exp.Literal):
        if node.is_string:
            return {"val": node.this}
        text = node.this
        return {"val": int(text) if text.lstrip("-").isdigit() else float(text)}
    if isinstance(node, exp.Neg):
        return {"xpr": ["-", _to_cxn(node.this, resolve_column)]}
    if isinstance(node, exp.Not):
        return {"xpr": ["not", _to_cxn(node.this, resolve_column)]}
    for node_type, op in _BINARY_OPS.items():
        if type(node) is node_type:
            return {"xpr": [_to_cxn(node.left, resolve_column), op, _to_cxn(node.right, resolve_column)]}
    if isinstance(node, exp.Is):
        right = node.expression
        if isinstance(right, exp.Null):
            return {"xpr": [_to_cxn(node.this, resolve_column), "is", "null"]}
        raise UnsupportedExpression("IS with a non-NULL right-hand side")
    if isinstance(node, exp.Like):
        return {"xpr": [_to_cxn(node.this, resolve_column), "like", _to_cxn(node.expression, resolve_column)]}
    if isinstance(node, exp.In):
        if node.args.get("query") is not None or node.args.get("unnest") is not None:
            raise UnsupportedExpression("IN with a subquery")
        items = [_to_cxn(e, resolve_column) for e in node.expressions]
        return {"xpr": [_to_cxn(node.this, resolve_column), "in", {"list": items}]}
    if isinstance(node, exp.Between):
        return {
            "xpr": [
                _to_cxn(node.this, resolve_column),
                "between",
                _to_cxn(node.args["low"], resolve_column),
                "and",
                _to_cxn(node.args["high"], resolve_column),
            ]
        }
    if isinstance(node, exp.Case):
        tokens: list[Any] = ["case"]
        if node.this is not None:
            tokens.append(_to_cxn(node.this, resolve_column))
        for branch in node.args.get("ifs") or []:
            tokens += ["when", _to_cxn(branch.this, resolve_column), "then", _to_cxn(branch.args["true"], resolve_column)]
        if node.args.get("default") is not None:
            tokens += ["else", _to_cxn(node.args["default"], resolve_column)]
        tokens.append("end")
        return {"xpr": tokens}
    if isinstance(node, (exp.Cast, exp.TryCast)):
        cast = _cast_type(node.args["to"])
        inner = _to_cxn(node.this, resolve_column)
        if not isinstance(inner, dict) or "xpr" in inner or "cast" in inner:
            raise UnsupportedExpression("cast of a compound or already cast expression")
        return {**inner, "cast": cast}
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Distinct):
        distinct_args = [_to_cxn(e, resolve_column) for e in node.this.expressions]
        return {"func": "count", "args": [{"xpr": ["distinct", *distinct_args]}]}
    if isinstance(node, exp.Star):
        return "*"
    if isinstance(node, (exp.Subquery, exp.Select, exp.Window)):
        raise UnsupportedExpression(f"{type(node).__name__} has no CXN translation here")
    if isinstance(node, exp.Func):
        args: list[Any] = []
        if isinstance(node, exp.Anonymous):
            name = node.name.lower()
            operands = list(node.expressions)
        else:
            name = node.sql_name().lower()
            operands = []
            for key in node.arg_types:
                value = node.args.get(key)
                if value is None:
                    continue
                if isinstance(value, list):
                    operands.extend(value)
                elif isinstance(value, exp.Expression):
                    operands.append(value)
                else:
                    raise UnsupportedExpression(f"function {name!r} with a non-expression argument")
        for operand in operands:
            if isinstance(operand, exp.Star):
                args.append("*")
            else:
                args.append(_to_cxn(operand, resolve_column))
        return {"func": name, "args": args}
    raise UnsupportedExpression(f"{type(node).__name__} has no CXN translation")


def sql_to_cxn(sql: str, resolve_column: ColumnResolver) -> dict[str, Any]:
    """Convert ANSI SQL text into one CXN expression (always an object)."""
    result = _to_cxn(parse_sql(sql), resolve_column)
    if isinstance(result, str):
        raise UnsupportedExpression("a bare '*' is not an expression")
    return result
