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

"""Tableau calculation <-> sqlglot (Snowflake dialect).

`translate` is a recursive-descent parser that builds a sqlglot tree from a Tableau formula; anything outside the grammar or
the catalogue raises `Untranslatable` (never guessed). `emit_tableau` is the reverse: a sqlglot tree to a Tableau formula.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

import sqlglot
from sqlglot import exp

from .calc_catalog import BY_NAME, for_dialect
from .formula import tokenize
from .warehouse import DEFAULT, Warehouse, needs_quote

UNITS = {"year", "quarter", "month", "week", "day", "hour", "minute", "second"}
Resolver = Callable[[str | None, str], "tuple[exp.Expression, str] | None"]


class Untranslatable(Exception):
    pass


@dataclass
class Translation:
    tree: exp.Expression
    type: str = "any"  # num | str | bool | date | any
    notes: list[str] = field(default_factory=list)
    aggregate: bool = False
    dialect: str = DEFAULT.sqlglot_dialect

    @property
    def sql(self) -> str:
        return self.tree.sql(self.dialect)


_KW = {"AND", "OR", "NOT", "IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "NULL", "TRUE", "FALSE", "IN"}
_CODE = re.compile(r"\s+|(?P<num>\d+\.\d*|\.\d+|\d+)|(?P<id>[A-Za-z_][A-Za-z_0-9]*)|(?P<date>#[^#]*#)|(?P<op><>|!=|==|<=|>=|[-+*/%^<>=(),.{}:])")


def _lex(f: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for t in tokenize(f):
        text = f[t.start:t.end]
        if t.kind == "comment":
            continue
        if t.kind == "ref":
            out.append(("ref", t.value))
        elif t.kind == "string":
            _q, body = text[0], text[1:-1] if len(text) > 1 and text[-1] == text[0] else text[1:]
            out.append(("str", re.sub(r"\\(.)", r"\1", body)))
        else:
            pos = 0
            while pos < len(text):
                m = _CODE.match(text, pos)
                if not m:
                    raise Untranslatable(f"unexpected character {text[pos]!r}")
                pos = m.end()
                if m.lastgroup:
                    out.append((m.lastgroup, m.group()))
    return out


@lru_cache(maxsize=None)
def _template(sql: str, dialect: str = DEFAULT.sqlglot_dialect) -> exp.Expression:
    # Templates are Snowflake SQL unless a warehouse override says otherwise.
    return sqlglot.parse_one(sql, read=dialect)


def _col(ds: str | None, name: str) -> exp.Column:
    return exp.Column(this=exp.to_identifier(name, quoted=needs_quote(name)),
                      table=exp.to_identifier(ds, quoted=needs_quote(ds)) if ds else None)


def _default_resolve(ds, name):
    return _col(ds, name), "any"


_AGG = (exp.AggFunc,)
_TABLE_SCOPE_PAIRS = {
    "TOTAL": (exp.Sum, exp.Avg, exp.Count, exp.Min, exp.Max),
    "WINDOW_SUM": (exp.Sum, exp.Count),
    "WINDOW_MIN": (exp.Min,),
    "WINDOW_MAX": (exp.Max,),
    "WINDOW_AVG": (),
}
_PREC = {exp.Or: 1, exp.And: 2, exp.Not: 3, exp.EQ: 4, exp.NEQ: 4, exp.LT: 4, exp.LTE: 4, exp.GT: 4, exp.GTE: 4, exp.In: 4,
         exp.Add: 5, exp.Sub: 5, exp.DPipe: 5, exp.Mul: 6, exp.Div: 6, exp.Mod: 6}


def _bin(cls, l, r):
    """Binary node with parentheses only where precedence needs them (keeps the output close to what a person would write)."""
    p = _PREC[cls]

    def wrap(n, right):
        q = _PREC.get(type(n))
        return exp.Paren(this=n) if q is not None and (q < p or (right and q == p)) else n

    return cls(this=wrap(l, False), expression=wrap(r, True))


class _P:
    def __init__(self, formula: str, resolve: Resolver, wh: Warehouse = DEFAULT,
                 order: list[exp.Expression] | None = None,
                 metric_names: set[str] | None = None):
        self.wh = wh
        self.toks = _lex(formula)
        self.i = 0
        self.resolve = resolve
        self.notes: list[str] = []
        self.order = list(order or [])
        self.metric_names = {n.lower(): n for n in (metric_names or ())}

    def _metric(self, name: str):
        orig = self.metric_names.get(name.lower())
        if orig is None:
            return None
        return exp.column(orig), "num"

    def peek(self, k=0):
        return self.toks[self.i + k] if self.i + k < len(self.toks) else ("eof", "")

    def kw(self, *names) -> bool:
        k, v = self.peek()
        return k == "id" and v.upper() in names

    def eat_kw(self, name) -> None:
        if not self.kw(name):
            raise Untranslatable(f"expected {name}")
        self.i += 1

    def op(self, *ops) -> str | None:
        k, v = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        return None

    def need(self, o):
        if not self.op(o):
            raise Untranslatable(f"expected {o!r}")

    def parse(self) -> tuple[exp.Expression, str]:
        r = self.expr()
        if self.peek()[0] != "eof":
            raise Untranslatable(f"unexpected {self.peek()[1]!r}")
        return r

    def expr(self):
        return self.or_()

    def or_(self):
        l, t = self.and_()
        while self.kw("OR"):
            self.i += 1
            r, _ = self.and_()
            l, t = _bin(exp.Or, l, r), "bool"
        return l, t

    def and_(self):
        l, t = self.not_()
        while self.kw("AND"):
            self.i += 1
            r, _ = self.not_()
            l, t = _bin(exp.And, l, r), "bool"
        return l, t

    def not_(self):
        if self.kw("NOT"):
            self.i += 1
            e, _ = self.not_()
            return exp.Not(this=exp.Paren(this=e) if type(e) in _PREC else e), "bool"
        return self.cmp()

    _CMP = {"=": exp.EQ, "==": exp.EQ, "<>": exp.NEQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}

    def cmp(self):
        l, t = self.add()
        while True:
            o = self.op(*self._CMP)
            if o:
                r, _ = self.add()
                l, t = _bin(self._CMP[o], l, r), "bool"
            elif self.kw("IN"):
                self.i += 1
                self.need("(")
                items = [self.expr()[0]]
                while self.op(","):
                    items.append(self.expr()[0])
                self.need(")")
                l, t = exp.In(this=exp.Paren(this=l) if type(l) in _PREC else l, expressions=items), "bool"
            elif self.kw("IS"):
                self.i += 1
                negated = False
                if self.kw("NOT"):
                    self.i += 1
                    negated = True
                if not self.kw("NULL"):
                    raise Untranslatable("IS other than NULL")
                self.i += 1
                n = exp.Is(this=l, expression=exp.Null())
                l, t = (exp.Not(this=n) if negated else n), "bool"
            else:
                return l, t

    def add(self):
        l, t = self.mul()
        while (o := self.op("+", "-")):
            r, rt = self.mul()
            if o == "+" and "str" in (t, rt):
                l, t = _bin(exp.DPipe, l, r), "str"
            else:
                if o == "+" and t == "any" and rt == "any":
                    note = "'+' operand types unknown; treated as numeric"
                    if note not in self.notes:
                        self.notes.append(note)
                if o == "-" and "date" not in (t, rt):
                    l, r = self._num(l, t), self._num(r, rt)
                l, t = _bin(exp.Add if o == "+" else exp.Sub, l, r), ("date" if "date" in (t, rt) else "num")
        return l, t

    def _num(self, e, t):
        """BigQuery refuses arithmetic on text; Snowflake coerces it, so text operands are converted (non-numeric text -> NULL)."""
        if t != "str" or self.wh.sqlglot_dialect != "bigquery":
            return e
        note = "text operand of an arithmetic expression is converted with SAFE_CAST(... AS FLOAT64), as Snowflake coerces implicitly; non-numeric text becomes NULL"
        if note not in self.notes:
            self.notes.append(note)
        return exp.TryCast(this=e, to=exp.DataType.build("DOUBLE"))

    def mul(self):
        l, t = self.pow_()
        while (o := self.op("*", "/", "%")):
            l = self._num(l, t)
            r, rt = self.pow_()
            r = self._num(r, rt)
            cls = {"*": exp.Mul, "/": exp.Div, "%": exp.Mod}[o]
            if cls is exp.Mod and self.wh.sqlglot_dialect == "bigquery":
                l = self._bq_mod(l, r)
            else:
                l = _bin(cls, l, r)
            t = "num"
        return l, t

    def _bq_mod(self, a, b):
        """BigQuery MOD takes integers only; FLOOR/CEIL/ROUND results are integral by construction so cast them to INT64.
        Other operands keep native MOD (exact for integer columns; a float column is rejected rather than guessed)."""
        inner = a.this if isinstance(a, exp.Paren) else a
        if isinstance(inner, (exp.Floor, exp.Ceil, exp.Round)):
            a = exp.Cast(this=a, to=exp.DataType.build("BIGINT"))
        return _bin(exp.Mod, a, b)

    def pow_(self):
        l, t = self.unary()
        while self.op("^"):
            r, _ = self.unary()
            l, t = exp.Pow(this=l, expression=r), "num"
        return l, t

    def unary(self):
        if self.op("-"):
            e, t = self.unary()
            e = self._num(e, t)
            return exp.Neg(this=exp.Paren(this=e) if type(e) in _PREC else e), "num"
        return self.primary()

    def primary(self):
        k, v = self.peek()
        if k == "num":
            self.i += 1
            return exp.Literal.number(v), "num"
        if k == "str":
            self.i += 1
            return exp.Literal.string(v), "str"
        if k == "date":
            self.i += 1
            body = v.strip("#")
            if self.wh.sqlglot_dialect == "bigquery":
                body = _iso_datetime_text(body, ":" not in body)
            return exp.Cast(this=exp.Literal.string(body), to=exp.DataType.build("TIMESTAMP" if ":" in body else "DATE")), "date"
        if k == "ref":
            return self.reference()
        if k == "op" and v == "(":
            self.i += 1
            e, t = self.expr()
            self.need(")")
            return exp.Paren(this=e), t
        if k == "op" and v == "{":
            return self.lod()
        if k == "id":
            u = v.upper()
            if u == "NULL":
                self.i += 1
                return exp.Null(), "any"
            if u in ("TRUE", "FALSE"):
                self.i += 1
                return exp.Boolean(this=u == "TRUE"), "bool"
            if u == "IF":
                return self.if_()
            if u == "CASE":
                return self.case()
            if u in _KW:
                raise Untranslatable(f"unexpected keyword {u}")
            if self.peek(1) == ("op", "("):
                return self.call()
            hit = self._metric(v)
            if hit is not None:
                self.i += 1
                return hit
            r = self.resolve(None, v)
            if r is not None:
                self.i += 1
                return r
        raise Untranslatable(f"unexpected {v!r}")

    _ADDR = "assumes table-wide addressing"

    def _addr(self) -> None:
        if self._ADDR not in self.notes:
            self.notes.append(self._ADDR)

    def _unwrap_node(self, body: exp.Expression) -> exp.Expression:
        n = body
        while isinstance(n, exp.Paren):
            n = n.this
        return n

    def _unwrap_single_agg(self, body: exp.Expression) -> exp.Expression:
        """SUM/AVG/MIN/MAX(col) → col so RANK/LAG/IF-THEN stay row-level and legal beside other columns."""
        n = self._unwrap_node(body)
        if isinstance(n, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
            inner = self._unwrap_node(n.this)
            if isinstance(inner, exp.Column):
                return inner
        return body

    def _order_clause(self) -> exp.Order | None:
        if not self.order:
            return None
        return exp.Order(expressions=[exp.Ordered(this=c.copy()) for c in self.order])

    def _first_value_window(self, body: exp.Expression, dims=None) -> exp.Expression:
        """FIRST_VALUE needs ORDER BY; BQ rejects NULLS LAST on the default RANGE frame, so use ROWS."""
        w = exp.Window(this=exp.FirstValue(this=body), partition_by=dims or None)
        if (o := self._order_clause()) is not None:
            w.set("order", o)
        if self.wh.sqlglot_dialect == "bigquery":
            spec = sqlglot.parse_one("SUM(x) OVER (ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)").args.get("spec")
            if spec is not None:
                w.set("spec", spec)
        return w

    def _row_number(self) -> exp.Expression:
        w = sqlglot.parse_one("ROW_NUMBER() OVER ()")
        if (o := self._order_clause()) is not None:
            w.set("order", o)
        return w

    def _window(self, body, dims, allow_nonagg=False):
        if any(isinstance(n, exp.Window) for n in body.walk()):
            raise Untranslatable("level-of-detail body must be an aggregate")
        if not any(isinstance(n, _AGG) for n in body.walk()):
            if not allow_nonagg:
                raise Untranslatable("level-of-detail body must be an aggregate")
            w = self._first_value_window(body, dims)
            self.notes.append("FIXED non-agg: FIRST_VALUE over the partition")
            return w
        return exp.Window(this=body, partition_by=dims or None)

    def _signed_int(self) -> int:
        e, _ = self.expr()
        sign, n = 1, e
        if isinstance(n, exp.Neg):
            sign, n = -1, n.this
            if isinstance(n, exp.Paren):
                n = n.this
        if isinstance(n, exp.Literal) and n.is_number:
            return sign * int(float(n.name))
        raise Untranslatable("window offset must be an integer literal")

    def _rows_window(self, body, start: int, end: int, count: bool = False):
        def bound(n: int) -> str:
            if n < 0:
                return f"{-n} PRECEDING"
            if n > 0:
                return f"{n} FOLLOWING"
            return "CURRENT ROW"
        w = sqlglot.parse_one(f"SUM(x) OVER (ROWS BETWEEN {bound(start)} AND {bound(end)})")
        inner = self._unwrap_single_agg(body) if count else self._unwrap_node(body)
        w.set("this", exp.Count(this=inner) if count else inner)
        if (o := self._order_clause()) is not None:
            w.set("order", o)
        return w

    def _rank_window(self, body, fn: str):
        key = self._unwrap_single_agg(body)
        w = sqlglot.parse_one(f"{fn}() OVER ()")
        w.set("order", exp.Order(expressions=[exp.Ordered(this=key, desc=True)]))
        return w

    def _lag_window(self, body: exp.Expression, n: int) -> exp.Expression:
        src = self._unwrap_single_agg(body)
        if n == 0:
            return src
        this = exp.Lag(this=src, offset=exp.Literal.number(-n)) if n < 0 else exp.Lead(this=src, offset=exp.Literal.number(n))
        w = exp.Window(this=this)
        if (o := self._order_clause()) is not None:
            w.set("order", o)
        return w

    def _unwrap_if_aggs(self, node: exp.Expression) -> exp.Expression:
        """IF FIRST()=0 THEN SUM(x) → CASE WHEN … THEN x so the branch is not a nested aggregate."""
        if not any(isinstance(n, exp.Window) for n in node.walk()):
            return node
        for ifn in node.find_all(exp.If):
            if ifn.args.get("true") is not None:
                ifn.set("true", self._unwrap_single_agg(ifn.args["true"]))
        if isinstance(node, exp.Case) and node.args.get("default") is not None:
            node.set("default", self._unwrap_single_agg(node.args["default"]))
        return node

    def lod(self):
        """{FIXED|INCLUDE|EXCLUDE dims : expr} and {AGG} become window SQL (table-wide addressing)."""
        self.need("{")
        dims: list[exp.Expression] = []
        kind = None
        if self.kw("INCLUDE", "EXCLUDE", "FIXED"):
            kind = self.peek()[1].upper()
            self.i += 1
            while self.peek()[0] == "ref":
                _, first = self.peek()
                self.i += 1
                ds, name = None, first
                if self.peek() == ("op", ".") and self.peek(1)[0] == "ref":
                    ds, name = first, self.peek(1)[1]
                    self.i += 2
                r = self.resolve(ds, name)
                dims.append(r[0] if r is not None else _col(ds, name))
                if not self.op(","):
                    break
            if self.peek() == ("op", ":"):
                self.i += 1
        body, t = self.expr()
        self.need("}")
        self._addr()
        if kind == "EXCLUDE":
            return self._window(body, []), t
        return self._window(body, dims, allow_nonagg=True), t

    def reference(self):
        _, first = self.peek()
        self.i += 1
        ds = None
        if self.peek() == ("op", ".") and self.peek(1)[0] == "ref":
            ds, name = first, self.peek(1)[1]
            self.i += 2
        else:
            name = first
        if ds == "Parameters":
            raise Untranslatable("parameter reference")
        r = self.resolve(ds, name)
        if r is None and ds is None:
            r = self._metric(name)
        if r is None:
            raise Untranslatable(f"unresolved reference [{name}]")
        return r

    def if_(self):
        self.eat_kw("IF")
        ifs, rtype = [], "any"
        while True:
            c, _ = self.expr()
            self.eat_kw("THEN")
            v, t = self.expr()
            ifs.append(exp.If(this=c, true=v))
            rtype = t if rtype == "any" else rtype
            if self.kw("ELSEIF"):
                self.i += 1
                continue
            break
        default = None
        if self.kw("ELSE"):
            self.i += 1
            default, t = self.expr()
            rtype = t if rtype == "any" else rtype
        self.eat_kw("END")
        node = exp.Case(ifs=ifs, default=default)
        return self._unwrap_if_aggs(node), rtype

    def case(self):
        self.eat_kw("CASE")
        subject = None if self.kw("WHEN") else self.expr()[0]
        ifs, rtype = [], "any"
        while self.kw("WHEN"):
            self.i += 1
            w, _ = self.expr()
            self.eat_kw("THEN")
            v, t = self.expr()
            ifs.append(exp.If(this=w, true=v))
            rtype = t if rtype == "any" else rtype
        if not ifs:
            raise Untranslatable("CASE without WHEN")
        default = None
        if self.kw("ELSE"):
            self.i += 1
            default, t = self.expr()
            rtype = t if rtype == "any" else rtype
        self.eat_kw("END")
        return exp.Case(this=subject, ifs=ifs, default=default), rtype

    def call(self):
        name = self.peek()[1].upper()
        self.i += 2
        args: list[tuple[exp.Expression, str]] = []
        unit = None
        if name == "COUNT" and self.op("*"):
            self.need(")")
            return exp.Count(this=exp.Star()), "num"
        entry = BY_NAME.get(name)
        if name in ("TOTAL", "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT"):
            if self.op(")"):
                raise Untranslatable(f"{name} needs an argument")
            body, t = self.expr()
            start = end = None
            if self.op(","):
                start = self._signed_int()
                if not self.op(","):
                    raise Untranslatable(f"{name} window offsets need a start and an end")
                end = self._signed_int()
            self.need(")")
            self._addr()
            if start is not None:
                return self._rows_window(body, start, end, count=name == "WINDOW_COUNT"), t
            inner = self._unwrap_node(body)
            outer_cls = {
                "WINDOW_SUM": exp.Sum, "WINDOW_AVG": exp.Avg, "WINDOW_MIN": exp.Min,
                "WINDOW_MAX": exp.Max, "WINDOW_COUNT": exp.Count,
            }.get(name)
            mismatch = {(exp.Sum, exp.Avg), (exp.Avg, exp.Sum), (exp.Min, exp.Max), (exp.Max, exp.Min)}
            if outer_cls is not None and isinstance(inner, _AGG) and (outer_cls, type(inner)) in mismatch:
                # Nested OVER is illegal in SF/BQ SELECT. Row-grain two-stage: unwrap inner agg, apply outer window.
                self.notes.append("mismatched WINDOW: outer window of unwrapped inner (row-grain two-stage)")
                return exp.Window(this=outer_cls(this=self._unwrap_single_agg(inner))), t
            if name == "WINDOW_COUNT":
                return self._window(exp.Count(this=self._unwrap_single_agg(body)), [], allow_nonagg=True), t
            return self._window(body, [], allow_nonagg=True), t
        if name in ("RANK", "RANK_DENSE", "RANK_UNIQUE", "RANK_PERCENTILE"):
            if self.op(")"):
                raise Untranslatable(f"{name} needs an argument")
            body, _ = self.expr()
            self.need(")")
            self._addr()
            fn = {
                "RANK": "RANK", "RANK_DENSE": "DENSE_RANK", "RANK_UNIQUE": "ROW_NUMBER",
                "RANK_PERCENTILE": "PERCENT_RANK",
            }[name]
            return self._rank_window(body, fn), "num"
        if name == "INDEX":
            self.need(")")
            self._addr()
            return self._row_number(), "num"
        if name == "FIRST":
            self.need(")")
            self._addr()
            return exp.Sub(this=exp.Literal.number(1), expression=self._row_number()), "num"
        if name == "LAST":
            self.need(")")
            self._addr()
            return exp.Sub(this=sqlglot.parse_one("COUNT(1) OVER ()"), expression=self._row_number()), "num"
        if name == "SIZE":
            self.need(")")
            self._addr()
            return sqlglot.parse_one("COUNT(1) OVER ()"), "num"
        if name == "LOOKUP":
            if self.op(")"):
                raise Untranslatable("LOOKUP needs an argument")
            body, t = self.expr()
            n = 0
            first = False
            if self.op(","):
                if self.peek()[0] == "id" and self.peek()[1].upper() == "FIRST" and self.peek(1) == ("op", "("):
                    self.i += 2
                    self.need(")")
                    first = True
                else:
                    n = self._signed_int()
            self.need(")")
            self._addr()
            if first:
                self.notes.append("LOOKUP(..., FIRST()): FIRST_VALUE")
                return self._first_value_window(self._unwrap_single_agg(body)), t
            return self._lag_window(body, n), t
        if name == "PREVIOUS_VALUE":
            if self.op(")"):
                raise Untranslatable("PREVIOUS_VALUE needs an argument")
            body, t = self.expr()
            self.need(")")
            self._addr()
            src = self._unwrap_single_agg(body)
            self.notes.append("PREVIOUS_VALUE: FIRST_VALUE of the seed (closed form of the recursive previous)")
            return self._first_value_window(src), t
        if entry is None:
            raise Untranslatable(f"unknown function {name}")
        if not self.op(")"):
            while True:
                if "UNIT_" in entry.sql and not args and self.peek()[0] in ("str", "id") and not unit and self.peek(1) in (("op", ","),):
                    u = self.peek()[1].lower()
                    if u not in UNITS and not (name in ("DATEPART",) and u == "weekday"):
                        raise Untranslatable(f"unsupported date part {u!r}")
                    unit = u
                    self.i += 1
                    args.append((exp.Null(), "str"))
                else:
                    args.append(self.expr())
                if self.op(","):
                    continue
                self.need(")")
                break
        return self.apply(entry, args, unit)

    def apply(self, entry, args, unit):
        entry = for_dialect(entry, self.wh.sqlglot_dialect)
        if entry.name == "DATENAME" and len(args) == 2:
            lit = args[0][0]
            if not (isinstance(lit, exp.Literal) and lit.is_string):
                raise Untranslatable("DATENAME: the date part must be a string literal")
            part = lit.name.lower()
            fmts = _DATENAME_BQ if entry.sql_dialect == "bigquery" else _DATENAME_SF
            if part not in fmts:
                raise Untranslatable(f"DATENAME: no verified Snowflake format for {part!r}")
            fmt = fmts[part]
            sql = (
                f"FORMAT_DATETIME('{fmt}', CAST(__a1 AS DATETIME))"
                if entry.sql_dialect == "bigquery"
                else f"TO_CHAR(__a1, '{fmt}')"
            )
            entry = dataclasses.replace(entry, sql=sql)
        if entry.name == "DATEPARSE" and len(args) == 2:
            lit = args[0][0]
            if not (isinstance(lit, exp.Literal) and lit.is_string):
                raise Untranslatable("DATEPARSE: the format must be a string literal")
            conv = _java_to_strftime if entry.sql_dialect == "bigquery" else _java_to_sf_format
            args = [(exp.Literal.string(conv(lit.name)), "str"), args[1]]
        if entry.status == "unmappable" or not entry.sql:
            raise Untranslatable(f"{entry.name}: {entry.note}")
        if entry.name in ("DATE", "DATETIME") and entry.sql_dialect == "bigquery" and len(args) == 1:
            lit = args[0][0]
            if isinstance(lit, exp.Literal) and lit.is_string:
                args = [(exp.Literal.string(_iso_datetime_text(lit.name, entry.name == "DATE")), args[0][1])]
            elif args[0][1] == "str":
                fmts = ("%m/%d/%Y",) if entry.name == "DATE" else ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y")
                parse = "PARSE_DATE" if entry.name == "DATE" else "PARSE_DATETIME"
                to = entry.name
                alts = ", ".join(f"SAFE.{parse}('{f}', __a0)" for f in fmts)
                entry = dataclasses.replace(entry, sql=f"COALESCE(SAFE_CAST(__a0 AS {to}), {alts})")
        if self.wh.sqlglot_dialect == "bigquery" and entry.name in _NUMERIC_FIRST_ARG and args and args[0][1] == "str":
            args = [(exp.TryCast(this=args[0][0], to=exp.DataType.build("DOUBLE")), "num"), *args[1:]]
            self.notes.append(f"{entry.name}: a text operand is converted with SAFE_CAST(... AS FLOAT64), as Snowflake coerces implicitly; non-numeric text becomes NULL")
        drop = False
        if entry.name == "SPLIT" and entry.sql_dialect == "bigquery" and len(args) == 3:
            n = args[2][0]
            neg = int(n.this.name) if isinstance(n, exp.Neg) and isinstance(n.this, exp.Literal) and n.this.name.isdigit() else None
            if neg:
                entry = dataclasses.replace(entry, sql=f"ARRAY_REVERSE(SPLIT(__a0, __a1))[SAFE_OFFSET({neg - 1})]", status="direct", note="")
                args, drop = args[:2], True
        if len(args) + (1 if drop else 0) not in entry.arity:
            raise Untranslatable(f"{entry.name} expects {entry.arity} argument(s), got {len(args)}")
        if entry.name == "ROUND" and len(args) == 1:
            args = [args[0], (exp.Literal.number(0), "num")]
        if entry.name == "FIND" and len(args) == 2:
            args = [*args, (exp.Literal.number(1), "num")]
        sql = entry.sql
        if "UNIT_" in sql:
            if unit is None:
                raise Untranslatable(f"{entry.name}: the date part must be a literal")
            sql = sql.replace("UNIT_", _UNIT_NAMES.get((entry.sql_dialect, unit), unit).upper())
        tree = _template(sql, entry.sql_dialect).copy()
        for c in list(tree.find_all(exp.Column)):
            m = re.fullmatch(r"__a(\d)", c.name)
            if m:
                c.replace(args[int(m.group(1))][0].copy())
        if entry.status == "approximated":
            self.notes.append(f"{entry.name}: {entry.note}")
        return tree, entry.result if entry.result != "any" else (args[0][1] if args else "any")


_NUMERIC_FIRST_ARG = frozenset({"ROUND", "ABS", "CEILING", "FLOOR", "SQRT", "INT", "SIGN"})
_US_DATE_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def _iso_datetime_text(text: str, date_only: bool) -> str:
    """BigQuery casts only ISO-8601 strings; rewrite a recognised US m/d/yyyy literal (Tableau's default parse) to ISO."""
    from datetime import datetime

    for f in _US_DATE_FORMATS:
        try:
            d = datetime.strptime(text.strip(), f)
        except ValueError:
            continue
        return d.strftime("%Y-%m-%d") if date_only else d.strftime("%Y-%m-%d %H:%M:%S")
    return text


def translate(formula: str, resolve: Resolver | None = None, wh: Warehouse = DEFAULT,
              order: list[exp.Expression] | None = None, metric_names: set[str] | None = None) -> Translation:
    p = _P(formula, resolve or _default_resolve, wh, order=order, metric_names=metric_names)
    tree, t = p.parse()
    return Translation(tree, t, p.notes, any(isinstance(n, _AGG) for n in tree.walk()), wh.sqlglot_dialect)


def to_sql(formula: str, resolve: Resolver | None = None, wh: Warehouse = DEFAULT) -> str | None:
    try:
        return translate(formula, resolve, wh).sql
    except Untranslatable:
        return None


_JAVA_TOKENS = [("yyyy", "YYYY"), ("MMMM", "MMMM"), ("MMM", "MON"), ("yy", "YY"), ("MM", "MM"), ("dd", "DD"), ("HH", "HH24"),
                ("hh", "HH12"), ("mm", "MI"), ("ss", "SS"), ("a", "AM")]
_STRFTIME_TO_JAVA = {"%Y": "yyyy", "%y": "yy", "%B": "MMMM", "%b": "MMM", "%m": "MM", "%d": "dd", "%H": "HH", "%I": "hh", "%M": "mm", "%S": "ss", "%p": "a"}
# DATENAME parts whose TO_CHAR / FORMAT_DATETIME tokens are already used by DATEPARSE. No invented formats.
_DATENAME_SF = {"year": "YYYY", "month": "MMMM", "day": "DD", "hour": "HH24", "minute": "MI", "second": "SS"}
_DATENAME_BQ = {"year": "%Y", "month": "%B", "day": "%d", "hour": "%H", "minute": "%M", "second": "%S"}
_SF_TO_DATENAME = {v: k for k, v in _DATENAME_SF.items()}
_BQ_TO_DATENAME = {v: k for k, v in _DATENAME_BQ.items()}


def _java_to_sf_format(fmt: str) -> str:
    out, i = [], 0
    while i < len(fmt):
        for j, sf in _JAVA_TOKENS:
            if fmt.startswith(j, i):
                out.append(sf)
                i += len(j)
                break
        else:
            ch = fmt[i]
            if ch.isalpha():
                raise Untranslatable(f"DATEPARSE: unsupported format letter {ch!r}")
            out.append(ch)
            i += 1
    return "".join(out)


_UNIT_NAMES = {("bigquery", "weekday"): "dayofweek"}
_JAVA_TO_STRFTIME = {j: s for s, j in _STRFTIME_TO_JAVA.items()}


def _java_to_strftime(fmt: str) -> str:
    out, i = [], 0
    toks = sorted(_JAVA_TO_STRFTIME, key=len, reverse=True)
    while i < len(fmt):
        for j in toks:
            if fmt.startswith(j, i):
                out.append(_JAVA_TO_STRFTIME[j])
                i += len(j)
                break
        else:
            ch = fmt[i]
            if ch.isalpha() or ch == "%":
                raise Untranslatable(f"DATEPARSE: unsupported format letter {ch!r}")
            out.append(ch)
            i += 1
    return "".join(out)


def _strftime_to_java(fmt: str) -> str:
    fmt = fmt.replace("%F", "%Y-%m-%d")  # BigQuery's sqlglot round trip abbreviates this
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == "%" and fmt[i:i + 2] in _STRFTIME_TO_JAVA:
            out.append(_STRFTIME_TO_JAVA[fmt[i:i + 2]])
            i += 2
        elif fmt[i] == "%" or fmt[i].isalpha():
            raise Untranslatable(f"no Tableau DATEPARSE equivalent for format {fmt!r}")
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


# ---- reverse: sqlglot -> Tableau ------------------------------------------------------------------------------------

def _unit(n) -> str:
    u = n.args.get("unit") or n.args.get("this")
    name = (u.name if u is not None else "").lower()
    name = {"dayofweek": "weekday"}.get(name, name)
    if name not in UNITS and name != "weekday":
        raise Untranslatable(f"no Tableau date part for {name!r}")
    return name


def _uncast(n: exp.Expression) -> exp.Expression:
    """Drop the CAST(x AS DATETIME) wrapper the BigQuery templates add around date operands."""
    return n.this if isinstance(n, exp.Cast) and n.args["to"].this in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME) else n


def emit_tableau(tree: exp.Expression, ref: Callable[[exp.Column], str] | None = None, count_star: str | None = None) -> str:  # noqa: ARG001
    """Tableau formula for a sqlglot tree; raises Untranslatable for anything with no catalogued equivalent.

    `count_star` is ignored; SQL `COUNT(*)` is emitted as Tableau `COUNT(*)`.
    """
    ref = ref or (lambda c: f"[{c.name.replace(']', ']]')}]")

    def e(n: exp.Expression | None) -> str:
        if n is None:
            raise Untranslatable("missing operand")
        return go(n)

    def fn(name, *a):
        return f"{name}({', '.join(e(x) for x in a)})"

    def go(n: exp.Expression) -> str:  # noqa: C901
        t = type(n)
        if t is exp.Column:
            return ref(n)
        if t is exp.Literal:
            return "'" + n.name.replace("\\", "\\\\").replace("'", "\\'") + "'" if n.is_string else n.name
        if t is exp.Null:
            return "NULL"
        if t is exp.Boolean:
            return "true" if n.this else "false"
        if t is exp.Paren:
            if isinstance(n.this, exp.Is) and isinstance(n.this.expression, exp.Null):
                return e(n.this)
            return f"({e(n.this)})"
        if t is exp.Window:
            if n.args.get("order") or n.args.get("spec") or not any(isinstance(x, _AGG) for x in n.this.walk()) or n.this.find(exp.Window):
                raise Untranslatable("window other than an aggregate over a partition")
            dims = ", ".join(e(d) for d in n.args.get("partition_by") or [])
            return "{FIXED " + (dims + " " if dims else "") + ": " + e(n.this) + "}"
        if t is exp.Neg:
            return f"-{e(n.this)}"
        bin_ops = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.Mod: "%", exp.EQ: "=", exp.NEQ: "<>", exp.LT: "<",
                   exp.LTE: "<=", exp.GT: ">", exp.GTE: ">=", exp.And: "AND", exp.Or: "OR", exp.DPipe: "+"}
        if t in bin_ops:
            return f"{e(n.this)} {bin_ops[t]} {e(n.expression)}"
        if t is exp.Not:
            return f"NOT {e(n.this)}"
        if t is exp.Is:
            if isinstance(n.expression, exp.Null):
                return fn("ISNULL", n.this)
            raise Untranslatable("IS other than NULL")
        if t is exp.In:
            return f"{e(n.this)} IN ({', '.join(e(x) for x in n.expressions)})"
        if t is exp.Between:
            out = f"{e(n.this)} >= {e(n.args['low'])} AND {e(n.this)} <= {e(n.args['high'])}"
            return out if n.parent is None or isinstance(n.parent, (exp.And, exp.Or, exp.Paren)) else f"({out})"
        if t is exp.Case:
            subj = n.args.get("this")
            f"CASE {e(subj)}" if subj is not None else "IF"
            parts = []
            for i, w in enumerate(n.args["ifs"]):
                kw = "WHEN" if subj is not None else ("IF" if i == 0 else "ELSEIF")
                parts.append(f"{kw} {e(w.this)} THEN {e(w.args['true'])}")
            if subj is not None:
                out = f"CASE {e(subj)} " + " ".join(parts)
            else:
                out = " ".join(parts)
            if n.args.get("default") is not None:
                out += f" ELSE {e(n.args['default'])}"
            return out + " END"
        if t is exp.If:
            if n.args.get("false") is None:
                return fn("IIF", n.this, n.args["true"], exp.Null())
            return fn("IIF", n.this, n.args["true"], n.args["false"])
        if t is exp.Coalesce:
            xs = [n.this, *n.expressions]
            if len(xs) != 2:
                raise Untranslatable("COALESCE with more than two operands")
            if isinstance(xs[1], exp.Literal) and xs[1].name == "0" and not xs[1].is_string:
                return fn("ZN", xs[0])
            return fn("IFNULL", *xs)
        if t is exp.Nullif:
            return f"IF {e(n.this)} = {e(n.expression)} THEN NULL ELSE {e(n.this)} END"
        if t is exp.Count:
            if isinstance(n.this, exp.Distinct):
                if len(n.this.expressions) != 1:
                    raise Untranslatable("COUNT(DISTINCT) over several columns")
                return fn("COUNTD", n.this.expressions[0])
            if isinstance(n.this, exp.Star):
                return "COUNT(*)"
            return fn("COUNT", n.this)
        if t is exp.Quarter:
            return f"DATEPART('quarter', {e(_uncast(n.this))})"
        simple = {exp.Sum: "SUM", exp.Avg: "AVG", exp.Min: "MIN", exp.Max: "MAX", exp.Median: "MEDIAN", exp.Stddev: "STDEV",
                  exp.StddevSamp: "STDEV", exp.Variance: "VAR", exp.Year: "YEAR", exp.Month: "MONTH", exp.Day: "DAY",
                  exp.Length: "LEN", exp.Upper: "UPPER", exp.Lower: "LOWER", exp.Trim: "TRIM", exp.Abs: "ABS", exp.Ceil: "CEILING",
                  exp.Floor: "FLOOR", exp.Sqrt: "SQRT", exp.Ln: "LN", exp.Exp: "EXP", exp.Sign: "SIGN",
                  exp.DayOfWeekIso: "ISOWEEKDAY"}
        if t in simple:
            return fn(simple[t], n.this)
        two = {exp.Left: "LEFT", exp.Right: "RIGHT", exp.Contains: "CONTAINS", exp.StartsWith: "STARTSWITH", exp.EndsWith: "ENDSWITH",
               exp.Pow: "POWER"}
        if t in two:
            return fn(two[t], n.this, n.expression)
        if t is exp.Pi:
            return "PI()"
        if t is exp.StrToTime and n.args.get("safe") and isinstance(n.args.get("format"), exp.Literal):
            return f"DATEPARSE('{_strftime_to_java(n.args['format'].name)}', {e(n.this)})"
        if t is exp.ToChar and isinstance(n.args.get("format"), exp.Literal):
            part = _SF_TO_DATENAME.get(n.args["format"].name)
            if part:
                return f"DATENAME('{part}', {e(n.this)})"
        if t is exp.StPoint:
            return fn("MAKEPOINT", n.expression, n.this)
        if t is exp.StrPosition:
            return fn("FIND", n.this, n.args["substr"], n.args.get("position") or exp.Literal.number(1))
        if t is exp.RegexpReplace:
            return fn("REGEXP_REPLACE", n.this, n.expression, n.args["replacement"])
        if t is exp.RegexpExtract:
            if n.args.get("group") is None or n.args["group"].name != "1":
                raise Untranslatable("REGEXP_SUBSTR other than capture group 1")
            return fn("REGEXP_EXTRACT", n.this, n.expression)
        if t is exp.Replace:
            return fn("REPLACE", n.this, n.expression, n.args["replacement"])
        if t is exp.Substring:
            if n.args.get("length") is None:
                raise Untranslatable("SUBSTRING without length")
            return fn("MID", n.this, n.args["start"], n.args["length"])
        if t is exp.SplitPart:
            return fn("SPLIT", n.this, n.args["delimiter"], n.args["part_index"])
        if t is exp.Round:
            d = n.args.get("decimals")
            return fn("ROUND", n.this) if d is None else fn("ROUND", n.this, d)
        if t is exp.Log:
            if isinstance(n.this, exp.Literal) and n.this.name == "10":
                return fn("LOG", n.expression)
            raise Untranslatable("LOG with a base other than 10")
        if t in (exp.TimestampTrunc, exp.DateTrunc, exp.DatetimeTrunc):
            return f"DATETRUNC('{_unit(n)}', {e(_uncast(n.this))})"
        if t is exp.Extract:
            return f"DATEPART('{_unit(n)}', {e(_uncast(n.expression))})"
        if t in (exp.DateAdd, exp.DatetimeAdd):
            amount = n.expression
            if isinstance(amount, exp.Literal) and amount.is_string and re.fullmatch(r"-?\d+", amount.name):
                amount = exp.Literal.number(amount.name)
            return f"DATEADD('{_unit(n)}', {e(amount)}, {e(_uncast(n.this))})"
        if t in (exp.DateDiff, exp.DatetimeDiff):
            return f"DATEDIFF('{_unit(n)}', {e(_uncast(n.expression))}, {e(_uncast(n.this))})"
        if t is exp.Anonymous and n.name.upper() == "ST_GEOGPOINT" and len(n.expressions) == 2:
            return fn("MAKEPOINT", n.expressions[1], n.expressions[0])
        if t is exp.SafeFunc and isinstance(n.this, exp.ParseDatetime) and isinstance(n.this.args.get("format"), exp.Literal):
            return f"DATEPARSE('{_strftime_to_java(n.this.args['format'].name)}', {e(n.this.this)})"
        if t is exp.TimeToStr and isinstance(n.args.get("format"), exp.Literal):
            part = _BQ_TO_DATENAME.get(n.args["format"].name)
            if part:
                src = n.this.this if isinstance(n.this, exp.TsOrDsToDatetime) else n.this
                return f"DATENAME('{part}', {e(_uncast(src))})"
        if t is exp.CurrentDatetime:
            return "NOW()"
        if t is exp.Bracket and isinstance(n.this, exp.Split) and n.args.get("safe") and n.args.get("offset") == 0 and len(n.expressions) == 1:
            idx = n.expressions[0]
            if isinstance(idx, exp.Sub) and isinstance(idx.expression, exp.Literal) and idx.expression.name == "1":
                return fn("SPLIT", n.this.this, n.this.expression, idx.this)
            raise Untranslatable("SPLIT index other than token - 1")
        if t is exp.Acos and isinstance(n.this, exp.Neg) and isinstance(n.this.this, exp.Literal) and n.this.this.name == "1":
            return "PI()"
        if t is exp.CurrentDate:
            return "TODAY()"
        if t is exp.CurrentTimestamp:
            return "NOW()"
        if t is exp.Cast:
            to = n.args["to"].this
            inner = n.this
            ints = (exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT)
            if to in ints:
                if isinstance(inner, exp.Trunc):
                    return fn("INT", inner.this)
                return f"INT(ROUND({e(inner)}))"
            if to in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL):
                return fn("FLOAT", inner)
            if to in (exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT, exp.DataType.Type.CHAR):
                return fn("STR", inner)
            if to is exp.DataType.Type.DATE:
                return fn("DATE", inner)
            if to in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME):
                return fn("DATETIME", inner)
        raise Untranslatable(f"no Tableau equivalent for {t.__name__}")

    return go(tree)


def from_sql(sql: str, ref: Callable[[exp.Column], str] | None = None, wh: Warehouse = DEFAULT, read: str | None | bool = True, count_star: str | None = None) -> str:
    """`read` is the sqlglot dialect of `sql`; the default (True) means the warehouse's own, None means generic ANSI."""
    try:
        tree = sqlglot.parse_one(sql, read=wh.sqlglot_dialect if read is True else read)
    except Exception as ex:  # noqa: BLE001
        raise Untranslatable(f"unparseable SQL: {ex}") from ex
    return emit_tableau(tree, ref, count_star=count_star)


def tableau_to_sql(formula: str, field_sql: dict[tuple[str, str], str], home: str | None, _depth: int = 0, wh: Warehouse = DEFAULT) -> str | None:
    """Translate a Tableau expression to Snowflake SQL with the calc parser, else None (see calc_parser/calc_catalog).

    `[DS].[f]` becomes `DS.f` (metric style); a bare `[f]` is replaced by the home dataset field's own SQL (row-level style).
    """
    if _depth > 8:
        return None

    def resolve(ds, name):
        if ds is not None:
            return exp.column(name, table=ds), "any"
        sql = field_sql.get((home or "", name))
        if sql is None:
            return None
        return exp.Paren(this=sqlglot.parse_one(sql, read=wh.sqlglot_dialect)), "any"

    try:
        t = translate(formula, resolve, wh)
    except (Untranslatable, sqlglot.errors.SqlglotError):
        return None
    return t.sql
