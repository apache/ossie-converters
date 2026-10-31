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

"""Conversion between an Atlan metric registry record and Ossie, both ways.

A registry record (docs/registry-format.md) is a SQL formula over `source` + `joins`, with
filters, dimensions, parameters and prose an agent needs. Forward: datasets, relationships,
fields, filters moved into every aggregate, parameters bound, and an ATLAN extension with the
registry id plus what a talk-to-data agent uses. A formula that is a whole SELECT is split
first (`split_query`): one Ossie metric per aggregate output.

    to_ossie(record)            -> (ossie metric, datasets, relationships)
    to_registry(doc, metric)    -> registry definition rebuilt from Ossie (new-metric case)
    write_back(doc, metric, record) -> fields an Ossie edit changed (id-present case)

Naming rules (deterministic, so forward(reverse(x)) compares cleanly):
  dataset       lower-cased table name; `<schema>_<table>` if the bare name is an SQL keyword
                or already taken in the model
  field         a dimension keeps its name; a plain column is its lower-cased name; a computed
                key is its SQL slugged (LEFT(ID, 15) -> left_id_15)
  relationship  `<from>_to_<to>`
  metric        the registry name (one output; whole-query formulas with several outputs are
                split upstream and named `<registry name>_<output alias>`; single underscores because
                MetricFlow rejects `__` in names)
"""

from __future__ import annotations

import contextlib
import copy
import json
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

DIALECT = "snowflake"
OSSIE_DIALECT = "OSSIE_SQL_2026"
DTYPE = {"string": "String", "integer": "Integer", "decimal": "Decimal", "boolean": "Boolean",
         "date": "Date", "timestamp": "DateTime"}
DTYPE_BACK = {v: k for k, v in DTYPE.items()}
# A dataset named like one of these would not re-parse as `name.column` (or would read as a
# keyword to another tool); it gets its schema prefix. An explicit list, so dataset names do not
# move when a dependency changes its keyword table.
KEYWORDS = set("""
all alter and any array as asc begin between by case cast check column commit constraint create
cross current database date decimal declare default delete desc describe distinct drop else end
escape except exists false fetch filter first for foreign from full function grant group having if
ignore in index inner insert intersect interval into is join key last left like limit loop merge
natural not null nulls offset on or order outer over partition primary procedure qualify range
references replace respect return revoke right rollback rows schema select set show table then time
timestamp top true truncate union unique update use user using values view when where while window
with current_date current_time current_timestamp current_user localtime localtimestamp session_user
ilike rlike regexp lateral sample""".split())
TIME_KINDS = {"time", "date", "timestamp"}


def _expr(text: str) -> dict:
    # The same text under both labels: OSSIE_SQL_2026 is the spec's portable dialect; Sigma, Omni
    # and ThoughtSpot read only ANSI_SQL and drop or fail on a metric without it.
    return {"dialects": [{"dialect": OSSIE_DIALECT, "expression": text},
                         {"dialect": "ANSI_SQL", "expression": text}]}


# Labels for ai_context.instructions, in order. Generated from agent_context (the structured
# copy), because Cube, ThoughtSpot, Omni and Power BI carry a metric's instructions into their
# own AI field and read nothing in the ATLAN extension.
INSTRUCTION_LABELS = [("grain", "Grain"), ("population", "Counts"), ("exclusions", "Excludes"),
                      ("unit", "Unit"), ("reaggregation", "Re-aggregation"),
                      ("null_handling", "Nulls"), ("replaced_by", "Replaced by"),
                      ("window", "Date range fixed in the source query, not applied by this metric")]


def instructions(ctx: dict) -> str:
    lines = []
    for key, label in INSTRUCTION_LABELS:
        v = ctx.get(key)
        if v in (None, "", [], {}):
            continue
        sep = " AND " if key == "window" else "; "
        lines.append(f"{label}: {sep.join(v) if isinstance(v, list) else v}.")
    return " ".join(lines)


def field_note(ext: dict) -> str:
    """A dimension's values, value meanings and filter form as one string for the field's
    ai_context: as a string it reaches Snowflake (description), Databricks (comment), Cube,
    ThoughtSpot, Omni, Power BI and Honeydew. The ATLAN copy on the field stays the structured,
    editable one."""
    parts = []
    if ext.get("values"):
        more = ", more exist" if ext.get("values_truncated") else ""
        parts.append(f"Values: {', '.join(map(str, ext['values']))} (sample{more}).")
    if ext.get("value_map"):
        parts.append("Meaning: " + "; ".join(f"{k} = {v}" for k, v in ext["value_map"].items()) + ".")
    if ext.get("filter_form"):
        parts.append(f"Filter as: {ext['filter_form']}.")
    return " ".join(parts)


def _table_idents(table: str, folded: bool = True) -> list[exp.Identifier]:
    """The one place a table name is read: parsed in the record's dialect (so BigQuery's
    `proj.ds.tbl` in one pair of backticks is three parts). With `folded`, every part is
    case-folded the way the dialect resolves it (for matching); without, parts are as written."""
    try:
        t = exp.to_table(table, dialect=DIALECT)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError, ValueError):
        raise NotConvertible(f"table name {table!r} does not parse in this dialect") from None
    raw = t.parts
    if not raw or not all(isinstance(i, exp.Identifier) for i in raw):
        raise NotConvertible(f"table name {table!r} is not a plain table reference")
    if any(q in i.name for i in raw for q in "`\"[]"):
        raise NotConvertible(f"table name {table!r} is quoted in a way this dialect does not use")
    return normalize_identifiers(t.copy(), dialect=DIALECT).parts if folded else raw


def _parts(table: str) -> list[str]:
    """catalog.schema.table as bare names, quotes removed."""
    return [i.name for i in _table_idents(table)]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def norm_table(table: str) -> str:
    """A table name as the Ossie dataset source: quoted parts exactly as written (case and
    quotes), unquoted parts folded the way the dialect folds them (Snowflake upper, Postgres
    lower). Quotes are never dropped: whether a quoted name is safe unquoted depends on each
    warehouse's reserved words."""
    folded, raw = _table_idents(table), _table_idents(table, folded=False)
    return ".".join((r if r.quoted else f).sql(dialect=DIALECT) for f, r in zip(folded, raw))


def table_key(table: str) -> tuple[str, ...]:
    """What two spellings of one table have in common, for matching only (never written out): the
    folded names without quotes. So `proj`.`ds`.`cust` and proj.ds.cust, or "CRM"."PUBLIC"."ACCOUNT"
    and crm.public.account in Snowflake, are one table."""
    return tuple(i.name for i in _table_idents(table))


def _ident(name: str) -> str:
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        return name
    out = _slug(name) or "metric"
    return out if out[0].isalpha() or out[0] == "_" else f"m_{out}"


def dataset_name(table: str, taken: set[str]) -> str:
    """An Ossie identifier for the table: its last part, slugged (`my tbl` -> my_tbl), with the
    schema in front when that is a keyword or already taken."""
    parts = [_slug(p) or "dataset" for p in _parts(table)]
    name = parts[-1]
    if name in KEYWORDS or name in taken:
        name = "_".join(parts[-2:])
    return name


class _Model:
    """Datasets and fields for one registry record, keyed by the record's SQL aliases."""

    def __init__(self, d: dict):
        if not (d["source"].get("table") or d["source"].get("entity")):
            raise NotConvertible("source names no table")
        self.alias_table = {d["source"].get("alias") or "m": norm_table(d["source"].get("table") or d["source"]["entity"])}
        for j in d.get("joins") or []:
            if not j.get("to") or not j.get("name"):
                raise NotConvertible("a join has no target table or no alias")
            self.alias_table[j["name"]] = norm_table(j["to"])
        taken: set[str] = set()
        self.alias_ds: dict[str, str] = {}
        for alias, table in self.alias_table.items():
            name = dataset_name(table, taken)
            if name in taken:                          # same table under a second alias
                name = f"{name}_{_slug(alias)}"
            taken.add(name)
            self.alias_ds[alias] = name
        self.fields: dict[str, list[dict]] = {n: [] for n in self.alias_ds.values()}
        self.by_sql: dict[tuple[str, str], str] = {}     # (dataset, column sql) -> field name
        self.base = d["source"].get("alias") or "m"
        # a column may be qualified by the table's name instead of its alias: the first alias
        # registered for that table wins
        self.table_alias: dict[str, str] = {}
        self.short_alias: dict[str, str | None] = {}
        for alias, table in self.alias_table.items():
            self.table_alias.setdefault(table_key(table), alias)
            short = _parts(table)[-1].upper()
            self.short_alias[short] = alias if short not in self.short_alias else None   # None = ambiguous
        for alias, of in (d["source"].get("_aliases") or {}).items():     # extra aliases from source.sql / join sql
            self.add_alias(alias, of)

    def add_alias(self, alias: str, of: str) -> None:
        """`alias` names the same table as the existing alias `of`."""
        if alias and alias not in self.alias_ds and of in self.alias_ds:
            self.alias_table[alias] = self.alias_table[of]
            self.alias_ds[alias] = self.alias_ds[of]

    def alias_of(self, c: exp.Column) -> str | None:
        """Which registered alias a column refers to: its own qualifier, the table it names, or
        the base table when it has none. None when it names something unknown."""
        q = c.table
        if not q:
            return self.base
        if q in self.alias_ds:
            return q
        full = table_key(exp.Table(this=c.args["table"].copy(), db=c.args["db"].copy() if c.args.get("db") else None,
                                   catalog=c.args["catalog"].copy() if c.args.get("catalog") else None).sql(dialect=DIALECT))
        return self.table_alias.get(full) or self.short_alias.get(q.upper())

    def field(self, alias: str, node: exp.Expression, name: str | None = None, **extra) -> str:
        ds = self.alias_ds[alias]
        bare = node.copy()
        for c in bare.find_all(exp.Column):
            c.set("table", None)
            c.set("db", None)
            c.set("catalog", None)
        # unquoted identifiers to upper case (Snowflake's own folding), so the same column read
        # as IS_DELETED by one metric and is_deleted by another is one field
        bare = normalize_identifiers(bare, dialect=DIALECT)
        sql = bare.sql(dialect=DIALECT)
        key = (ds, sql)
        if key in self.by_sql:
            fname = self.by_sql[key]
            f = next(x for x in self.fields[ds] if x["name"] == fname)
            for k, v in extra.items():
                f.setdefault(k, v)
            return fname
        if name is None:
            name = bare.name.lower() if isinstance(bare, exp.Column) else _slug(sql)   # no quotes in a name
        used = {x["name"] for x in self.fields[ds]}
        base, i = name, 2
        while name in used:                            # same name, different SQL
            name, i = f"{base}_{i}", i + 1
        f = {"name": name, "expression": _expr(sql), **extra}
        self.fields[ds].append(f)
        self.by_sql[key] = name
        return name

    def rewrite(self, sql: str, params: dict, bind: bool = True) -> str:
        """alias.COLUMN -> dataset.field; :param -> its typed default literal (bind=True) or kept
        as the placeholder (bind=False, for text that is parked rather than computed)."""
        if re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", sql):
            raise NotConvertible("formula has a {template} placeholder, not a declared parameter")
        # parse first: `V:amount` is a Snowflake variant path, only `:name` placeholders are parameters
        tree = sqlglot.parse_one(sql, read=DIALECT)
        for ph in list(tree.find_all(exp.Placeholder)):
            if ph.name in ("", "?"):
                raise NotConvertible("formula has an unbound positional parameter (?) with no name or default")
            if ph.name not in params:
                raise NotConvertible(f"formula uses undeclared parameter {ph.name}")
            if bind:
                ph.replace(sqlglot.parse_one(_literal(params[ph.name]), read=DIALECT))
        for c in list(tree.find_all(exp.Column)):
            alias = self.alias_of(c)
            if alias is None:
                raise NotConvertible(f"column {c.sql(dialect=DIALECT)} reads a table the metric does not declare")
            if isinstance(c.this, exp.Star):              # COUNT(m.*): the dataset, no field
                c.set("table", exp.to_identifier(self.alias_ds[alias]))
                continue
            fname = self.field(alias, exp.column(c.name, quoted=c.this.quoted))   # "MyCol" stays "MyCol"
            new = exp.column(fname, table=self.alias_ds[alias])
            if c is tree:                                 # the whole text is one column
                tree = new
            else:
                c.replace(new)
        # COUNT(*) names no dataset; COUNT(<base>.*) does, which is what Ossie's dbt converter
        # needs to place a row count (converters/dbt README) and what the reverse reads back
        for cnt in list(tree.find_all(exp.Count)):
            if isinstance(cnt.this, exp.Star):
                cnt.set("this", exp.Column(this=exp.Star(), table=exp.to_identifier(self.alias_ds[self.base])))
        return tree.sql(dialect=DIALECT)


def _table_name(t: exp.Table) -> str:
    """A parsed table reference in its canonical spelling (see norm_table)."""
    return norm_table(exp.Table(this=t.this.copy(), db=t.args["db"].copy() if t.args.get("db") else None,
                                catalog=t.args["catalog"].copy() if t.args.get("catalog") else None).sql(dialect=DIALECT))


def _literal(p: dict) -> str:
    v = p.get("default")
    if v is None:
        raise NotConvertible(f"parameter {p['name']!r} has no default; Ossie has no parameters, so "
                         "the metric cannot be expressed: flag it for a person")
    if p["datatype"] in ("date", "timestamp"):
        return f"{p['datatype'].upper()} '{v}'"
    return exp.Literal.string(v).sql() if isinstance(v, str) else str(v)   # 'O''Brien'


DATE_LIT = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?")
RELATIVE = (exp.CurrentDate, exp.CurrentTimestamp)


RANGE = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)


def _date_token(node: exp.Expression, date_params: set[str]) -> bool:
    return any((isinstance(x, exp.Literal) and x.is_string and DATE_LIT.fullmatch(x.this))
               or (isinstance(x, exp.Placeholder) and x.name in date_params)
               for x in node.find_all(exp.Literal, exp.Placeholder))


def split_window(pred: str, params: list[dict]) -> tuple[list[str], list[str]]:
    """A filter's conjuncts split into (window, kept). A window conjunct is a range comparison
    (>=, <, BETWEEN) of a column against a fixed date ('2026-03-18', or a date parameter): an
    analyst's one-off range, parked in agent_context.window and named in the instructions, not
    applied by the metric. Everything else, including an equality to one date and a range
    relative to the current date, is part of the metric and stays in it."""
    tree = sqlglot.parse_one(pred, read=DIALECT)
    while isinstance(tree, exp.Paren):
        tree = tree.this
    conjuncts = [c.this if isinstance(c, exp.Paren) and isinstance(c.this, (exp.Condition,)) and not isinstance(c.this, exp.And) else c
                 for c in (tree.flatten() if isinstance(tree, exp.And) else [tree])]
    date_params = {p["name"] for p in params if p.get("datatype") in ("date", "timestamp")}
    window, kept = [], []
    for c in conjuncts:
        is_window = False
        if isinstance(c, RANGE) and not c.find(*RELATIVE) and not re.search(r"\b(GETDATE|SYSDATE|NOW)\s*\(", c.sql(), re.I):
            if isinstance(c, exp.Between):
                sides = [c.this, c.args["low"], c.args["high"]]
                is_window = (c.this.find(exp.Column) is not None and not _date_token(c.this, date_params)
                             and all(_date_token(x, date_params) and not x.find(exp.Column) for x in sides[1:]))
            else:
                a, b = c.left, c.right
                col_side = [x for x in (a, b) if x.find(exp.Column) is not None and not _date_token(x, date_params)]
                date_side = [x for x in (a, b) if _date_token(x, date_params) and x.find(exp.Column) is None]
                is_window = len(col_side) == 1 and len(date_side) == 1
        (window if is_window else kept).append(c.sql(dialect=DIALECT))
    return window, kept


def _join_predicate(j: dict, m: "_Model") -> exp.Expression:
    """The ON condition of a declared join. `sql` is either the condition itself or a full
    `[LEFT] JOIN table [alias] ON condition`; an alias used there is registered for the join."""
    text = j["sql"]
    if re.match(r"\s*(LEFT|RIGHT|INNER|FULL|CROSS)?\s*(OUTER)?\s*JOIN\b", text, re.I):
        jn = sqlglot.parse_one("SELECT * FROM _b " + text, read=DIALECT).args["joins"][0]
        if jn.args.get("on") is None:
            raise NotConvertible(f"join {j['name']!r} has no ON clause")
        if isinstance(jn.this, exp.Table) and jn.this.alias:
            m.add_alias(jn.this.alias, j["name"])
        return jn.args["on"]
    return sqlglot.parse_one(text, read=DIALECT)


def _wrap_aggregates(sql: str, predicate: str) -> str:
    """Every aggregate f(x) becomes f(CASE WHEN predicate THEN x END); COUNT(*) counts 1."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    cond = sqlglot.parse_one(predicate, read=DIALECT)
    if tree.find(exp.WithinGroup):
        raise NotConvertible("a filter cannot be folded into an ordered-set aggregate (WITHIN GROUP)")
    for agg in list(tree.find_all(exp.AggFunc)):
        arg = agg.this
        others = [v for k, v in agg.args.items() if k != "this" and isinstance(v, exp.Expression)]
        if any(o.find(exp.Column) for o in others):      # MIN_BY(value, order): the order side would stay unfiltered
            raise NotConvertible(f"a filter cannot be folded into {agg.sql_name()}: it reads more than one column")
        distinct = isinstance(arg, exp.Distinct)
        if distinct and len(arg.expressions) > 1:          # COUNT(DISTINCT a, b): wrap every column
            arg.set("expressions", [exp.Case(ifs=[exp.If(this=cond.copy(), true=e.copy())]) for e in arg.expressions])
            continue
        inner = arg.expressions[0] if distinct else arg
        star = isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star))
        value = exp.Literal.number(1) if star else inner
        case = exp.Case(ifs=[exp.If(this=cond.copy(), true=value.copy())])
        agg.set("this", exp.Distinct(expressions=[case]) if distinct else case)
    return tree.sql(dialect=DIALECT)


# Metric meaning with no Ossie element: kept as named keys in ext.agent_context, so an edit can be
# written back; ai_context.instructions is generated from them (instructions()) for the exporters
# that read only instructions.
#   agent_context key -> registry definition path
SEMANTICS = {"grain": "grain", "population": "population", "exclusions": "exclusions",
             "unit": "output.unit", "reaggregation": "aggregation.reaggregation",
             "null_handling": "aggregation.null_handling", "replaced_by": "replaced_by"}


def _get(d: dict, path: str):
    for k in path.split("."):
        d = (d or {}).get(k)
    return d


def _semantics(d: dict) -> dict:
    out = {k: _get(d, p) for k, p in SEMANTICS.items()}
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


class NotConvertible(ValueError):
    """The metric has no Ossie form by these rules; flag it for a person."""


def _normalize_source(d: dict) -> None:
    """A row-set source (`SELECT * FROM t [alias] WHERE ...`) becomes table + filters."""
    sql = d["source"].get("sql")
    if not sql:
        return
    if not re.match(r"\s*(WITH|SELECT)\b", sql, re.I):      # a FROM fragment: `t LEFT JOIN u ON ...`
        sql = "SELECT * FROM " + sql
    tree = sqlglot.parse_one(sql, read=DIALECT)
    if not (isinstance(tree, exp.Select) and len(tree.expressions) == 1
            and isinstance(tree.expressions[0], exp.Star)
            and not any(tree.args.get(k) for k in ("group", "having", "limit", "with", "with_"))):
        raise NotConvertible("row-set source is more than SELECT * FROM table WHERE ...")
    frm = tree.args.get("from") or tree.args.get("from_")
    t = frm.this if frm else None
    if not isinstance(t, exp.Table):
        raise NotConvertible("row-set source reads a subquery, not a table")
    alias = d["source"].get("alias") or "m"
    d["source"]["alias"] = alias
    d["source"]["table"] = _table_name(t)
    d["source"]["sql"] = None
    extra = d["source"].setdefault("_aliases", {})
    frag_join_aliases = {(jn.this.alias or jn.this.name) for jn in tree.args.get("joins") or []
                         if isinstance(jn.this, exp.Table)}
    if alias in frag_join_aliases:
        raise NotConvertible(f"source.alias {alias!r} names a joined table inside source.sql, "
                             "so the formula's columns cannot be placed")
    if t.alias and t.alias != alias:
        extra[t.alias] = alias
    declared = {table_key(j["to"]) for j in d.get("joins") or []}
    for jn in tree.args.get("joins") or []:              # joins in the row set become declared joins
        jt = jn.this
        if not isinstance(jt, exp.Table) or jn.args.get("on") is None:
            raise NotConvertible("row-set source joins without an ON clause, or joins a subquery")
        to = _table_name(jt)
        jalias = jt.alias or jt.name
        if table_key(to) in declared:                    # the declared join carries the cardinality
            existing = next(j for j in d["joins"] if table_key(j["to"]) == table_key(to))
            if jalias != existing["name"]:
                extra[jalias] = existing["name"]
            continue
        d.setdefault("joins", []).append({"name": jalias, "to": to, "sql": jn.args["on"].sql(dialect=DIALECT),
                                          "cardinality": "many_to_one",
                                          "origin": "join in source.sql (cardinality assumed)"})
    if tree.args.get("joins"):
        # the formula, filters, dimensions and time column run over the same joined row set as the
        # WHERE, so a bare column there is just as ambiguous
        _refuse_bare_structured(d, include_formula=True)
    where = tree.args.get("where")
    if where:
        cond = where.this
        _qualify_or_refuse([cond], alias, joined=bool(tree.args.get("joins")))
        parts = list(cond.flatten()) if isinstance(cond, exp.And) else [cond]
        d["filters"] = [p.sql(dialect=DIALECT) for p in parts] + list(d.get("filters") or [])


def _qualify_or_refuse(nodes: list[exp.Expression], base_alias: str, joined: bool) -> None:
    """Give every unqualified column the base table's alias, or refuse when the SQL joins other
    tables: there the column may live on any of them, and guessing the base table would write a
    field that does not exist without any signal."""
    bare = sorted({c.name for n in nodes for c in n.find_all(exp.Column)
                   if not c.table and not isinstance(c.this, exp.Star)})
    if bare and joined:
        raise NotConvertible(f"column{'s' if len(bare) > 1 else ''} {', '.join(bare)} not qualified by a "
                             "table in a query that joins tables, so the owning table is unknown")
    for n in nodes:
        for c in n.find_all(exp.Column):
            if not c.table:
                c.set("table", exp.to_identifier(base_alias))


def _refuse_bare_structured(d: dict, include_formula: bool) -> None:
    """A record whose SQL joins tables (a row-set source.sql, or a whole-query formula): its
    structured fields run over that join, so a column with no table there is refused. A
    dimension may name its table in required_join."""
    texts = [*([d.get("formula")] if include_formula else []), *(d.get("filters") or []),
             *(d.get("mandatory_filters") or []),
             *(x.get("sql") for x in d.get("dimensions") or [] if not x.get("required_join")),
             (d.get("source") or {}).get("timeframe_column")]
    _qualify_or_refuse([sqlglot.parse_one(t, read=DIALECT) for t in texts if t], "_", joined=True)


def split_query(art: dict) -> list[dict]:
    """Step 0: a whole-query formula `SELECT <group cols>, <aggregates> FROM t [JOIN ...] [WHERE]
    [GROUP BY] [ORDER BY]` becomes one structured record per aggregate output, named
    <name>_<output alias> when there are several. Anything else (CTEs, subqueries, windows,
    HAVING, DISTINCT, LIMIT) is NotConvertible; ORDER BY changes no value and is dropped."""
    d = art["metadata"]["definition"]
    try:
        tree = sqlglot.parse_one(d["formula"], read=DIALECT)
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
        raise NotConvertible(f"formula does not parse: {str(e).splitlines()[0][:80]}") from None
    if not isinstance(tree, exp.Select):
        raise NotConvertible("formula is a set operation, not one SELECT")
    if tree.args.get("with") or tree.args.get("with_"):
        raise NotConvertible("formula uses CTEs")
    if tree.find(exp.Subquery) or tree.find(exp.Window):
        raise NotConvertible("formula uses a subquery or window function")
    if tree.find(exp.Exists) or any(n is not tree for n in tree.find_all(exp.Select)):
        raise NotConvertible("formula uses a subquery or window function")
    if tree.args.get("distinct") or tree.args.get("qualify"):
        raise NotConvertible("formula de-duplicates or ranks rows (SELECT DISTINCT / QUALIFY)")
    if tree.args.get("having"):
        # a filter on the aggregated groups; a CASE WHEN inside the aggregate cannot express it
        raise NotConvertible("formula filters groups after aggregating (HAVING); Ossie has no form for it")
    if tree.args.get("limit") or tree.args.get("offset") or (tree.args.get("limit_") if "limit_" in tree.args else None):
        raise NotConvertible("formula keeps only some of the groups (LIMIT / OFFSET); Ossie has no form for it")
    frm = tree.args.get("from") or tree.args.get("from_")
    src = frm.this if frm else None
    if not isinstance(src, exp.Table):
        raise NotConvertible("FROM is not a table")
    base_alias = src.alias_or_name
    table = _table_name
    declared = {table_key(j["to"]): j for j in d.get("joins") or [] if j.get("to")}
    joins = []
    for j in tree.args.get("joins") or []:
        on = j.args.get("on")
        if on is None or j.args.get("kind") in ("CROSS",):
            raise NotConvertible("join without an ON equality")
        known = declared.get(table_key(table(j.this)))      # a declared join carries the cardinality
        joins.append({"name": j.this.alias_or_name, "to": table(j.this), "sql": on.sql(dialect=DIALECT),
                      "cardinality": known["cardinality"] if known else "many_to_one",
                      "origin": known.get("origin") or "declared" if known else
                      f"split from the formula ({(j.args.get('side') or 'INNER')} JOIN; cardinality assumed)"})
    group = [g for g in (tree.args.get("group").expressions if tree.args.get("group") else [])]
    outs, dims = [], []
    for e in tree.expressions:
        inner = e.this if isinstance(e, exp.Alias) else e
        name = e.alias_or_name or _slug(inner.sql(dialect=DIALECT))   # unaliased expression
        if inner.find(exp.AggFunc):
            outs.append((name, inner))
        else:
            dims.append((name, inner))
    if not outs:
        raise NotConvertible("no aggregate output (a row listing, not a measure)")
    for g in group:                                 # GROUP BY 1, 2 -> the select items
        if isinstance(g, exp.Literal) and g.is_int:
            continue
        if not any(inner.sql() == g.sql() or (isinstance(g, exp.Column) and not g.table and g.name == n)
                   for n, inner in dims):                # GROUP BY <select alias> is that select item
            dims.append((g.alias_or_name if isinstance(g, exp.Column) else _slug(g.sql()), g))
    # an unqualified column belongs to the base table only when the query reads one table; with
    # a join, SQL resolves it against every joined table, which a registry record cannot do here
    where = tree.args.get("where")
    _qualify_or_refuse([inner for _, inner in dims + outs] + ([where.this] if where else [])
                       + [j.args["on"] for j in tree.args.get("joins") or [] if j.args.get("on") is not None],
                       base_alias, joined=bool(joins))
    filters = []
    if where:
        cond = where.this
        filters = [p.sql(dialect=DIALECT) for p in (cond.flatten() if isinstance(cond, exp.And) else [cond])]
    records, taken = [], set()
    for alias, inner in outs:
        r = copy.deepcopy(art)
        rd = r["metadata"]["definition"]
        if len(outs) > 1:
            part = _slug(alias) or _slug(inner.sql(dialect=DIALECT))[:40]
            name, i = f"{art['name']}_{part}", 2
            while name in taken:
                name, i = f"{art['name']}_{part}_{i}", i + 1
            taken.add(name)
            r["name"] = name
        rd["formula"] = inner.sql(dialect=DIALECT)
        rd["source"] = {**rd["source"], "alias": base_alias, "table": table(src), "sql": None,
                        "entity": table(src)}
        orig = d.get("source", {}).get("alias")      # structured filters may still use the record's alias
        if orig and orig != base_alias and orig not in {j["name"] for j in joins}:
            rd["source"]["_aliases"] = {**(rd["source"].get("_aliases") or {}), orig: base_alias}
        rd["joins"] = joins
        if joins:                                   # the record's own fields run over the joined query too
            _refuse_bare_structured(d, include_formula=False)
        rd["filters"] = filters + list(d.get("filters") or [])
        rd["dimensions"] = [{"name": _slug(n), "sql": x.sql(dialect=DIALECT), "datatype": "unknown",
                             "kind": "time" if (x.find(exp.DateTrunc, exp.TimestampTrunc)
                                                or (isinstance(x, exp.Cast) and x.to.is_type("date")))
                             else "categorical"} for n, x in dims] or d.get("dimensions") or []
        rd["output"] = {**rd["output"], "alias": r["name"]}
        if len(outs) > 1:
            r["output_column"] = alias           # which output column of the query this metric is
            r["_sibling"] = bool(records)        # not the first part: heavy text is carried once
            r["description"] = (f"{art['description'].rstrip('.')}. This part: "
                                f"{alias} = {inner.sql(dialect=DIALECT)}.")
        records.append(r)
    return records


TEMPLATE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


# Registry `dialect` values whose formula is SQL. Anything else (promql, mixpanel, slack ...) has
# no Ossie expression.
SQL_DIALECTS = {"snowflake", "ansi", "ansi_sql", "sql", "postgres", "postgresql", "bigquery", "duckdb",
                "databricks", "spark", "redshift", "mysql", "tsql", "mssql", "trino", "presto", "athena",
                "clickhouse", "oracle", "sqlite"}
JINJA = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


# registry `dialect` -> the sqlglot dialect the formula is parsed with (and the Ossie text is
# written in). The Ossie labels OSSIE_SQL_2026 and ANSI_SQL both carry that text: sqlglot's
# portable form leaves source-specific functions (COUNT_IF, DATEADD, GET_PATH) as they are, and
# a real translation would only swap one dialect's idioms for another's.
PARSE_DIALECT = {"snowflake": "snowflake", "athena": "athena", "trino": "trino", "presto": "presto",
                 "postgres": "postgres", "postgresql": "postgres", "bigquery": "bigquery",
                 "databricks": "databricks", "spark": "spark", "redshift": "redshift", "duckdb": "duckdb",
                 "mysql": "mysql", "tsql": "tsql", "mssql": "tsql", "clickhouse": "clickhouse",
                 "oracle": "oracle", "sqlite": "sqlite", "ansi": "snowflake", "ansi_sql": "snowflake", "sql": "snowflake"}


@contextlib.contextmanager
def _parsing(dialect: str | None):
    """Parse and write SQL in this registry dialect for the duration of one conversion."""
    global DIALECT
    saved, DIALECT = DIALECT, PARSE_DIALECT.get((dialect or "").lower(), "snowflake")
    try:
        yield
    finally:
        DIALECT = saved


def convert(art: dict) -> list[tuple[dict, list[dict], list[dict]]]:
    """Any registry metric -> list of (metric, datasets, relationships). Steps 0 to 6."""
    d = (art.get("metadata") or {}).get("definition") if isinstance(art, dict) else None
    if not isinstance(d, dict) or not art.get("name") or not art.get("id"):
        raise NotConvertible("record has no name, id or metadata.definition")
    if not isinstance(d.get("formula"), str) or not d["formula"].strip():
        raise NotConvertible("formula is empty")
    if not isinstance(d.get("source"), dict):
        raise NotConvertible("definition has no source")
    dialect = (d.get("dialect") or "").lower()
    if dialect and dialect not in SQL_DIALECTS:
        raise NotConvertible(f"dialect {dialect!r} is not SQL, so the formula has no Ossie expression")
    with _parsing(dialect):
        return _convert(art)


def _bind_jinja(art: dict) -> dict:
    """`{{name}}` -> `:name` wherever the parameter is declared (a copy of the record)."""
    d = art["metadata"]["definition"]
    declared = {p["name"] for p in (d.get("source") or {}).get("parameters") or [] if isinstance(p, dict)}
    texts = [d.get("formula"), (d.get("source") or {}).get("sql"), *(d.get("filters") or []),
             *(d.get("mandatory_filters") or [])]
    if not any(JINJA.search(t or "") for t in texts):
        return art
    if any(re.search(r"'[^']*\{\{[^}]*\}\}[^']*'", t or "") for t in texts):
        raise NotConvertible("a {{parameter}} inside a string literal has no bound form")
    art = copy.deepcopy(art)
    d = art["metadata"]["definition"]

    def sub(t):
        return JINJA.sub(lambda mt: f":{mt.group(1)}" if mt.group(1) in declared else mt.group(0), t)

    d["formula"] = sub(d["formula"])
    if d["source"].get("sql"):
        d["source"]["sql"] = sub(d["source"]["sql"])
    d["filters"] = [sub(f) for f in d.get("filters") or []]
    d["mandatory_filters"] = [sub(f) for f in d.get("mandatory_filters") or []]
    return art


def _convert(art: dict) -> list[tuple[dict, list[dict], list[dict]]]:
    art = _bind_jinja(art)
    d = art["metadata"]["definition"]
    raw = [d.get("formula") or "", (d.get("source") or {}).get("sql") or "",
           *(d.get("filters") or []), *(d.get("mandatory_filters") or [])]
    if any(TEMPLATE.search(t) for t in raw):     # before parsing: {x} parses as a Snowflake object
        raise NotConvertible("formula has a {template} placeholder, not a declared parameter")
    try:
        if re.match(r"\s*(WITH|SELECT)\b", d["formula"], re.I):
            return [to_ossie(r) for r in split_query(art)]
        return [to_ossie(art)]
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
        raise NotConvertible(f"formula does not parse: {str(e).splitlines()[0][:80]}") from None


def _params_to_dimensions(d: dict) -> list[str]:
    """A parameter with no default that only picks one value of a column (`col = :p`, the
    per-account metrics) becomes a dimension on that column; the filter that used it is removed
    and returned. A no-default parameter used any other way is NotConvertible."""
    removed = []
    for p in list(d["source"].get("parameters") or []):
        if p.get("default") is not None:
            continue
        hit = None
        for key in ("filters", "mandatory_filters"):
            for f in d.get(key) or []:
                t = sqlglot.parse_one(f, read=DIALECT)
                if isinstance(t, exp.EQ) and any(isinstance(x, exp.Placeholder) and x.name == p["name"] for x in (t.left, t.right)):
                    col = t.right if isinstance(t.left, exp.Placeholder) else t.left
                    if isinstance(col, exp.Column):
                        hit = (key, f, col)
        if hit:
            key, f, col = hit
            d[key] = [x for x in d[key] if x != f]
            removed.append(f)
            if not any(sqlglot.parse_one(x["sql"], read=DIALECT) == col for x in d.get("dimensions") or []):
                d.setdefault("dimensions", []).append({"name": col.name.lower(), "kind": "categorical",
                                                       "sql": col.sql(dialect=DIALECT),
                                                       "datatype": p.get("datatype") or "string"})
            d["source"]["parameters"] = [x for x in d["source"]["parameters"] if x is not p]
            still = [t for t in (d.get("formula"), *(d.get("filters") or []), *(d.get("mandatory_filters") or []))
                     if t and re.search(rf"(?<![A-Za-z0-9_]):{re.escape(p['name'])}(?![A-Za-z0-9_])", t)]
            if still:
                raise NotConvertible(f"parameter {p['name']!r} has no default and is used beyond "
                                     f"`{col.sql(dialect=DIALECT)} = :{p['name']}`")
    return removed


def _prepared(art: dict) -> dict:
    """The record's definition as to_ossie sees it: `{{name}}` bound, a whole query split (first
    part), the row-set source folded into filters, the parameter picks turned into dimensions."""
    art = copy.deepcopy(_bind_jinja(art))
    d = art["metadata"]["definition"]
    if re.match(r"\s*(WITH|SELECT)\b", d["formula"], re.I):
        d = split_query(art)[0]["metadata"]["definition"]
    _normalize_source(d)
    d["_param_filters"] = _params_to_dimensions(d)
    return d


def unfolded_filters(art: dict) -> list[str]:
    """The record's filters that are NOT folded into the Ossie expression: the per-account
    parameter picks and the fixed date windows, in the record's own aliases. A write-back that
    rewrites the formula keeps these."""
    with _parsing(art["metadata"]["definition"].get("dialect")):
        d = _prepared(art)
        plist = d["source"].get("parameters") or []
        window = [w for f in d.get("filters") or [] for w in split_window(f, plist)[0]]
    return d["_param_filters"] + window


def record_aliases(art: dict) -> dict[str, str]:
    """Ossie dataset name -> the alias the record itself uses for that table (the source alias,
    or the join name), so a write-back speaks the record's language."""
    with _parsing(art["metadata"]["definition"].get("dialect")):
        m = _Model(_prepared(art))
    out = {}
    for alias, ds in m.alias_ds.items():
        out.setdefault(ds, alias)
    return out


def to_ossie(art: dict) -> tuple[dict, list[dict], list[dict]]:
    art = copy.deepcopy(art)
    d = art["metadata"]["definition"]
    if re.match(r"\s*(WITH|SELECT)\b", d["formula"], re.I):
        raise NotConvertible("whole-query formula: run split_query() first (convert() does)")
    _normalize_source(d)
    top = sqlglot.parse_one(d["formula"], read=DIALECT)
    if isinstance(top, (exp.Predicate, exp.Not, exp.And, exp.Or)):
        raise NotConvertible("formula is a yes/no test over an aggregate, not a measure")
    refs = {_slug(c.name) for c in top.find_all(exp.Column)} & {_slug(x) for x in d.get("depends_on") or []}
    if refs:
        raise NotConvertible("formula is built from other metrics (" + ", ".join(sorted(refs)) + "); "
                             "Ossie does not define metric references")
    if not top.find(exp.AggFunc):
        raise NotConvertible("no aggregate output (a row listing, not a measure)")
    m = _Model(d)
    _params_to_dimensions(d)
    if any("cardinality assumed" in (j.get("origin") or "") for j in d.get("joins") or []):
        raise NotConvertible("join read from SQL with no declared cardinality; only declared "
                             "many_to_one joins are written")
    params = {p["name"]: p for p in d["source"].get("parameters") or []}   # bound on use, see rewrite()

    # relationships: ON clause of each join -> key fields on both sides
    rels = []
    for j in d.get("joins") or []:
        on = _join_predicate(j, m)
        eqs = list(on.flatten()) if isinstance(on, exp.And) else [on]
        if not all(isinstance(e, exp.EQ) for e in eqs) or j["cardinality"] != "many_to_one":
            raise NotConvertible(f"join {j['name']!r}: only many_to_one equality joins have an Ossie "
                             "form; keep it in the extension and flag it")
        from_cols, to_cols, from_alias = [], [], None
        for e in eqs:
            sides = [e.left, e.right]
            owners = [{m.alias_of(c) for c in s.find_all(exp.Column)} for s in sides]
            if not owners[0] or not owners[1]:
                raise NotConvertible(f"join {j['name']!r}: `{e.sql(dialect=DIALECT)}` is not a key equality; "
                                     "only key equalities have an Ossie form")
            if None in owners[0] | owners[1]:
                raise NotConvertible(f"join {j['name']!r}: its ON clause names a table the metric does not declare")
            owner_ds = [{m.alias_ds[a] for a in o} for o in owners]
            if {m.alias_ds[j["name"]]} not in owner_ds:
                raise NotConvertible(f"join {j['name']!r}: its ON clause does not name the joined table "
                                     "on one side and a known table on the other")
            tgt = sides[owner_ds.index({m.alias_ds[j["name"]]})]
            src = sides[1] if tgt is sides[0] else sides[0]
            from_alias = next(iter({m.alias_of(c) for c in src.find_all(exp.Column)}))
            from_cols.append(m.field(from_alias, src))
            to_cols.append(m.field(j["name"], tgt))
        rels.append({"name": f"{m.alias_ds[from_alias]}_to_{m.alias_ds[j['name']]}",
                     "from": m.alias_ds[from_alias], "to": m.alias_ds[j["name"]],
                     "from_columns": from_cols, "to_columns": to_cols})

    # dimensions -> named fields on the dataset their SQL reads. What describes the column
    # (values, value map, filter form) sits on the field; which metric groups by it sits on the
    # metric (agent_context.dimensions), because a field is shared by every metric of the dataset.
    dim_refs = []
    for dim in d.get("dimensions") or []:
        node = sqlglot.parse_one(dim["sql"], read=DIALECT)
        if dim.get("required_join") and dim["required_join"] not in m.alias_ds:
            raise NotConvertible(f"dimension {dim['name']!r}: required_join {dim['required_join']!r} "
                                 "names no join of this record")
        if dim.get("required_join") in m.alias_ds:          # a bare column of the joined table
            for c in node.find_all(exp.Column):
                if not c.table:
                    c.set("table", exp.to_identifier(dim["required_join"]))
        aliases = {m.alias_of(c) for c in node.find_all(exp.Column)}
        if None in aliases:
            raise NotConvertible(f"dimension {dim['name']!r} reads a table the metric does not declare")
        if len(aliases) != 1:
            raise NotConvertible(f"dimension {dim['name']!r} reads {len(aliases)} tables")
        extra = {"dimension": {"is_time": dim["kind"] in TIME_KINDS}}
        if dim.get("datatype") in DTYPE:
            extra["datatype"] = DTYPE[dim["datatype"]]
        if dim.get("description"):
            extra["description"] = dim["description"]
        ext = {k: dim[k] for k in ("values", "value_map", "values_truncated") if dim.get(k) not in (None, [], {})}
        alias = aliases.pop()
        if ext or dim.get("filter_form"):
            extra["custom_extensions"] = [{"vendor_name": "ATLAN", "data": ext}]
        fname = m.field(alias, node, name=_slug(dim["name"]) or dim["name"], **extra)
        dim_refs.append(f"{m.alias_ds[alias]}.{fname}")
        if dim.get("filter_form"):                     # after the field exists, so it is reused
            ff = sqlglot.parse_one(dim["filter_form"].replace(":value", "'__v__'"), read=DIALECT)
            for c in ff.find_all(exp.Column):          # a bare column is the dimension's own table's
                if not c.table:
                    c.set("table", exp.to_identifier(alias))
            ext["filter_form"] = m.rewrite(ff.sql(dialect=DIALECT), {}).replace("'__v__'", ":value")
        if field_note(ext):
            f = next(x for x in m.fields[m.alias_ds[alias]] if x["name"] == fname)
            f["ai_context"] = field_note(ext)
    tf = d["source"].get("timeframe_column")
    tf_ref = None
    if tf:
        node = sqlglot.parse_one(tf, read=DIALECT)
        if not isinstance(node, exp.Column) or m.alias_of(node) is None:
            raise NotConvertible(f"timeframe_column {tf!r} is not a column of a declared table")
        a = m.alias_of(node)
        tf_ref = f"{m.alias_ds[a]}.{m.field(a, exp.column(node.name, quoted=node.this.quoted), dimension={'is_time': True})}"

    # the metric: filters rewritten and moved inside every aggregate
    mand = [m.rewrite(f, params) for f in d.get("mandatory_filters") or []]
    plist = d["source"].get("parameters") or []
    window, kept = [], []
    for f in d.get("filters") or []:
        w, k = split_window(f, plist)
        window += w
        kept += k
    filt = [m.rewrite(f, params) for f in kept]
    pred = " AND ".join(f"({p})" for p in mand + filt)
    expr = m.rewrite(d["formula"], params)
    if pred:
        expr = _wrap_aggregates(expr, pred)
    if any(ph.name in ("", "?") for ph in sqlglot.parse_one(expr, read=DIALECT).find_all(exp.Placeholder)):
        raise NotConvertible("formula has an unbound positional parameter (?) with no name or default")
    if sqlglot.parse_one(expr, read=DIALECT).find(exp.ArrayAgg, exp.ArrayUniqueAgg, exp.ObjectAgg, exp.GroupConcat):
        raise NotConvertible("aggregate returns a list, not a number (ARRAY_AGG, LISTAGG, OBJECT_AGG)")
    if sqlglot.parse_one(expr, read=DIALECT).find(exp.Select, exp.Exists, exp.Window):
        raise NotConvertible("formula uses a subquery or window function")
    if not sqlglot.parse_one(expr, read=DIALECT).find(exp.AggFunc):
        raise NotConvertible("aggregate function not in Ossie's portable SQL")
    if (d.get("aggregation") or {}).get("zero_denominator") == "zero" and not _zero_wrapped(expr):
        expr = f"COALESCE({expr}, 0)"
    # a registry name may be a display string ("MAU change vs 60-90 days ago"); Ossie and every
    # tool want an identifier. The registry id in the extension still maps it back.
    metric = {"name": _ident(art["name"]), "expression": _expr(expr)}
    if (d.get("output") or {}).get("datatype") in DTYPE:
        metric["datatype"] = DTYPE[d["output"]["datatype"]]
    metric["description"] = art.get("description") or ""
    qs = [e["question"] for e in d.get("examples") or [] if e.get("question")]
    ai = {}
    try:
        shown = [m.rewrite(w, params) for w in window]
    except NotConvertible:                            # a date parameter with no default: show the placeholder
        shown = [m.rewrite(w, params, bind=False) for w in window]
    text = instructions({**_semantics(d), **({"window": shown} if window else {})})
    if text:
        ai["instructions"] = text
    if qs:
        ai["examples"] = qs                           # Ossie's own slot for sample questions
    if ai:
        metric["ai_context"] = ai
    prov = d.get("provenance") or {}
    ctx = {**_semantics(d), "sql_dialect": d.get("dialect") or "snowflake"}
    if window:                                        # parked, not applied (split_window); Ossie names, :params kept
        ctx["window"] = [m.rewrite(w, params, bind=False) for w in window]
    if dim_refs:
        ctx["dimensions"] = dim_refs                  # this metric's group-by fields
    if rels:
        ctx["relationships"] = [r["name"] for r in rels]
    if tf_ref:
        ctx["timeframe_field"] = tf_ref
    if d["source"].get("parameters"):
        ctx["parameters"] = d["source"]["parameters"]
    if d.get("depends_on"):
        ctx["depends_on"] = [_ident(x) for x in d["depends_on"]]   # the Ossie names of those metrics
    # example and verified SQL are the heaviest text; a query split into several metrics carries
    # them on its first part only (the parts share the registry id)
    # an example's question (also in ai_context.examples) stays with its SQL; results are not carried
    exs = [{**({"question": e["question"]} if e.get("question") else {}), "sql": e["sql"]}
           for e in d.get("examples") or [] if e.get("sql")]
    if exs and not art.get("_sibling"):
        ctx["examples"] = exs
    if prov.get("observed_sql") and not art.get("_sibling") and (not exs or prov["observed_sql"] != exs[0].get("sql")):
        ctx["verified_sql"] = prov["observed_sql"]
    if prov.get("trust_score") is not None:
        ctx["trust"] = {"score": prov["trust_score"]}
    metric["custom_extensions"] = [{"vendor_name": "ATLAN", "data": {
        "registry": {"id": art["id"], "definition_version": d.get("version"),
                     **({"output_column": art["output_column"]} if art.get("output_column") else {})},
        "agent_context": ctx}}]
    datasets = []
    for alias, ds in m.alias_ds.items():
        entry = {"name": ds, "source": m.alias_table[alias]}
        keys = [r["to_columns"] for r in rels if r["to"] == ds]
        # A declared many_to_one join says its target columns identify one row. Most exporters
        # read primary_key (dbt primary entity, Snowflake, Cube, Omni, Power BI ...); only four
        # read unique_keys. A second, different key on the same dataset stays a unique key.
        if keys:
            entry["primary_key"] = copy.deepcopy(keys[0])
            rest = [k for k in keys[1:] if k != keys[0]]
            if rest:
                entry["unique_keys"] = copy.deepcopy(rest)
        if m.fields[ds]:
            entry["fields"] = m.fields[ds]
        datasets.append(entry)
    return metric, datasets, rels


def _conflict(ds: dict, rels: dict, owner: dict, m: dict, dsl: list[dict], rl: list[dict]) -> str | None:
    """Why this metric's datasets/relationships cannot share the document with what is there."""
    if m["name"] in owner.get("_metrics", set()):
        return f"metric name {m['name']!r} is already used by another record (two display names slug to it)"
    for d in dsl:
        have = ds.get(d["name"])
        if have is None:
            continue
        if have.get("source") != d.get("source"):
            return f"dataset {d['name']}: {have.get('source')} and {d.get('source')} share a name"
        for f in d.get("fields", []):
            g = next((x for x in have.get("fields", []) if x["name"] == f["name"]), None)
            if g is None:
                continue
            for k, v in f.items():
                if k in ("custom_extensions", "description", "ai_context"):
                    continue
                if k in g and g[k] != v:
                    return (f"field {d['name']}.{f['name']}: metrics {owner[(d['name'], f['name'])]} and "
                            f"{m['name']} disagree on {k!r}")
            for x in f.get("custom_extensions") or []:
                gx = next((y for y in g.get("custom_extensions") or [] if y["vendor_name"] == x["vendor_name"]), None)
                if gx and any(k in gx["data"] and gx["data"][k] != v for k, v in x["data"].items()):
                    return f"field {d['name']}.{f['name']}: metrics disagree on its values or filter form"
        if d.get("primary_key") and have.get("primary_key") not in (None, d["primary_key"]):
            return f"dataset {d['name']}: metrics disagree on primary_key"
    for r in rl:
        if r["name"] in rels and rels[r["name"]] != r:
            return f"relationship {r['name']}: metrics disagree on its keys"
    return None


def merge(parts: list[tuple[dict, list[dict], list[dict]]], name: str = "model",
          description: str = "", workspace_id: str | None = None, on_conflict: str = "raise") -> dict:
    """Several converted metrics into one Ossie document. Datasets, fields and relationships with
    the same name are shared (they are dataset-level); what is per metric stays on the metric.
    Two metrics may describe a shared field in different words (the first wording is kept and
    noted); if they define it differently (expression, type, time role) the document cannot hold
    both: on_conflict="raise" raises ValueError, "skip" leaves the later metric out. What was
    left out or reworded is returned under the private key `_merge_notes` as (kind, metric,
    detail) tuples, kind "left_out" or "prose"; export() drops it, so the document never carries
    diagnostics. The registry workspace is model-level: one ATLAN extension."""
    ds: dict[str, dict] = {}
    rels: dict[str, dict] = {}
    metrics = []
    notes: list[tuple[str, str, str]] = []
    owner: dict[tuple[str, str], str] = {}        # (dataset, field) -> first metric that defined it
    for m, dsl, rl in parts:
        why = _conflict(ds, rels, owner, m, dsl, rl)
        if why:
            if on_conflict != "skip":
                raise ValueError(why)
            notes.append(("left_out", m["name"], why))
            continue
        metrics.append(m)
        owner.setdefault("_metrics", set()).add(m["name"])
        for d in dsl:
            if d["name"] not in ds:
                ds[d["name"]] = copy.deepcopy(d)
                for f in d.get("fields", []):
                    owner[(d["name"], f["name"])] = m["name"]
                continue
            have = ds[d["name"]]
            for f in d.get("fields", []):
                g = next((x for x in have.setdefault("fields", []) if x["name"] == f["name"]), None)
                if g is None:
                    have["fields"].append(copy.deepcopy(f))
                    owner[(d["name"], f["name"])] = m["name"]
                    continue
                for k, v in f.items():
                    if k == "custom_extensions":
                        continue
                    if k in g and g[k] != v:               # prose only (checked above): first wording wins
                        notes.append(("prose", m["name"], f"{d['name']}.{f['name']}: {k} differs from "
                                      f"{owner[(d['name'], f['name'])]}; kept the first"))
                        continue
                    g.setdefault(k, v)
                for x in f.get("custom_extensions") or []:
                    gx = next((y for y in g.setdefault("custom_extensions", []) if y["vendor_name"] == x["vendor_name"]), None)
                    if gx is None:
                        g["custom_extensions"].append(copy.deepcopy(x))
                    else:
                        for k, v in x["data"].items():
                            gx["data"].setdefault(k, v)
            if d.get("primary_key"):
                have["primary_key"] = d["primary_key"]
            for key in d.get("unique_keys", []):
                if key not in have.setdefault("unique_keys", []):
                    have["unique_keys"].append(key)
        for r in rl:
            rels.setdefault(r["name"], copy.deepcopy(r))
    doc = {"version": "0.2.0.dev0", "name": name}
    if description:
        doc["description"] = description
    doc["datasets"] = list(ds.values())
    if rels:
        doc["relationships"] = list(rels.values())
    doc["metrics"] = metrics
    if workspace_id:
        doc["custom_extensions"] = [{"vendor_name": "ATLAN", "data": {"registry": {"workspace_id": workspace_id}}}]
    if notes:
        doc["_merge_notes"] = notes
    return doc


def export(doc: dict) -> dict:
    """The strict Ossie 0.2.0.dev0 form: every ATLAN extension's data as a JSON string; private
    keys (`_merge_notes`) dropped."""
    out = json.loads(json.dumps({k: v for k, v in doc.items() if not k.startswith("_")}))
    holders = [out] + out["datasets"] + [f for d in out["datasets"] for f in d.get("fields", [])] \
        + out.get("relationships", []) + out["metrics"]
    for h in holders:
        for x in h.get("custom_extensions") or []:
            if not isinstance(x["data"], str):
                x["data"] = json.dumps(x["data"])
    return out


def _registry_of(m: dict) -> dict:
    return next((x["data"].get("registry") or {} for x in m.get("custom_extensions") or []
                 if x["vendor_name"] == "ATLAN"), {})


def agent_context(m: dict) -> dict:
    """The ATLAN agent_context block of an Ossie metric ({} when there is none)."""
    return next((x["data"].get("agent_context") or {} for x in m.get("custom_extensions") or []
                 if x["vendor_name"] == "ATLAN"), {})


def expression_text(holder: dict) -> str:
    """The SQL of a field or metric: OSSIE_SQL_2026, else ANSI_SQL, else the first dialect
    (files from other converters may list a vendor dialect first)."""
    dl = holder["expression"]["dialects"]
    for want in (OSSIE_DIALECT, "ANSI_SQL"):
        for x in dl:
            if x.get("dialect") == want:
                return x["expression"]
    return dl[0]["expression"]


def _metric(doc: dict, name: str) -> dict:
    m = next((x for x in doc.get("metrics") or [] if x.get("name") == name), None)
    if m is None:
        raise LookupError(f"no metric named {name!r} in the document")
    return m


# ---------- Ossie -> Atlan

class Conflict(RuntimeError):
    """The registry changed since the Ossie file was made; re-convert before writing back."""


class NotWritable(RuntimeError):
    """The edit cannot be expressed in the registry record; change the registry instead."""


def _zero_wrapped(sql: str) -> bool:
    """The whole expression is COALESCE(<expr>, 0) with a numeric zero."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    return (isinstance(tree, exp.Coalesce) and len(tree.expressions) == 1
            and isinstance(tree.expressions[0], exp.Literal) and not tree.expressions[0].is_string
            and tree.expressions[0].this == "0")


def _reverse_parts(doc: dict, m: dict, aliases: dict[str, str] | None = None,
                   zero_setting: bool = False) -> dict:
    """formula, source, joins, dimensions, timeframe rebuilt from the Ossie model. Which
    relationships, group-by fields and time field belong to this metric comes from the metric's
    own agent_context (relationships / dimensions / timeframe_field); a metric without it (new,
    authored in Ossie) uses the relationships among the datasets its expression reads."""
    ctx = agent_context(m)
    ds = {d["name"]: d for d in doc.get("datasets") or []}
    from_registry = any(x["vendor_name"] == "ATLAN" and "registry" in x["data"]
                        for x in m.get("custom_extensions") or [])
    names = ctx.get("relationships", [] if from_registry else None)   # converted: absent = none
    rels = [r for r in doc.get("relationships") or [] if names is None or r["name"] in names]
    expr_text = expression_text(m)
    # Files from other tools' converters write bare column names (SUM(o_totalprice)) or metric
    # names (revenue / order_count). A bare column that exists on exactly one dataset is read as
    # that dataset's; a metric name or an ambiguous column has no registry form.
    tree0 = sqlglot.parse_one(expr_text, read=DIALECT)
    metric_names = {x["name"] for x in doc["metrics"]}
    for c in list(tree0.find_all(exp.Column)):
        if c.table:
            continue
        if c.name in metric_names:
            raise NotConvertible(f"metric is built from other metrics ({c.name}); the registry formula "
                                 "cannot reference another metric")
        homes = [n for n, d in ds.items() if any(f["name"] == c.name for f in d.get("fields", []))]
        if len(homes) != 1:
            raise NotConvertible(f"column {c.name!r} is not qualified by a dataset and "
                                 f"{'no dataset' if not homes else 'several datasets'} define it")
        c.set("table", exp.to_identifier(homes[0]))
    expr_text = tree0.sql(dialect=DIALECT)
    used = {c.table for c in tree0.find_all(exp.Column) if c.table in ds}
    targets = {r["to"] for r in rels}
    base = next((n for n in ds if n in used and n not in targets), None)
    if base is None and names is not None:            # this metric's own joins lead to what it reads: start at their root
        roots = sorted({r["from"] for r in rels} - targets)
        base = roots[0] if len(roots) == 1 else None
    if base is None:                                  # another tool's file: the dataset read is the source
        base = next(iter(sorted(used)), None)
    if base is None:
        raise NotConvertible("the expression reads no dataset column, so there is no source table to rebuild")
    # the joins: relationships reachable from the base, many side to one side
    reached, chain = {base}, []
    while True:
        step = [r for r in rels if r["from"] in reached and r["to"] not in reached]
        if not step:
            break
        chain += step
        reached |= {r["to"] for r in step}
    stray = used - reached
    if stray:
        raise NotConvertible(f"the expression reads {', '.join(sorted(stray))} with no relationship from {base}")
    rels = chain
    for n in reached | used:
        if not ds[n].get("source"):
            raise NotConvertible(f"dataset {n!r} has no source table")
    aliases = aliases or {}
    alias = {base: aliases.get(base, "m")}
    for i, r in enumerate(rels):
        alias.setdefault(r["to"], aliases.get(r["to"], f"t{i + 1}"))

    def field_sql(dname, fname):
        if dname not in alias:
            raise NotConvertible(f"dataset {dname!r} is not reachable from {base!r} by this metric's relationships")
        if fname == "*":                                  # COUNT(dataset.*)
            return exp.Column(this=exp.Star(), table=exp.to_identifier(alias[dname]))
        f = next((f for f in ds[dname].get("fields", []) if f["name"] == fname), None)
        # a column no field declares (files from dbt's converter write SUM(orders.amount) over
        # physical columns) is read as that column of the dataset's source table, as written
        node = sqlglot.parse_one(expression_text(f), read=DIALECT) if f else exp.column(fname)
        for c in ([node] if isinstance(node, exp.Column) else list(node.find_all(exp.Column))):
            c.set("table", exp.to_identifier(alias[dname]))
        return node

    tree = sqlglot.parse_one(expr_text, read=DIALECT)
    # For a record whose aggregation.zero_denominator is zero, forward writes the setting as
    # COALESCE(<expr>, 0) around the whole expression: that wrapper is the setting, not part of
    # the formula. Anywhere else a COALESCE is the author's own SQL and stays in the formula.
    zero = zero_setting and _zero_wrapped(tree.sql(dialect=DIALECT))
    if zero:
        tree = tree.this
    for c in list(tree.find_all(exp.Column)):
        if c.table in ds:
            c.replace(field_sql(c.table, c.name))
    joins = [{"name": alias[r["to"]], "to": ds[r["to"]]["source"],
              "sql": " AND ".join(f"{field_sql(r['from'], a).sql(dialect=DIALECT)} = "
                                  f"{field_sql(r['to'], b).sql(dialect=DIALECT)}"
                                  for a, b in zip(r["from_columns"], r["to_columns"])),
              "cardinality": "many_to_one", "origin": "ossie relationship"} for r in rels]
    dims = []
    for ref in ctx.get("dimensions") or []:
        dname, fname = ref.split(".", 1)
        f = next(x for x in ds[dname].get("fields", []) if x["name"] == fname)
        dims.append({"name": f["name"], "sql": field_sql(dname, f["name"]).sql(dialect=DIALECT),
                     "kind": "time" if (f.get("dimension") or {}).get("is_time") else "categorical",
                     "datatype": DTYPE_BACK.get(f.get("datatype"), "unknown"),
                     **({"description": f["description"]} if f.get("description") else {})})
    tf = None
    if ctx.get("timeframe_field"):
        dname, fname = ctx["timeframe_field"].split(".", 1)
        tf = field_sql(dname, fname).sql(dialect=DIALECT)
    window = []
    for w in ctx.get("window") or []:                 # parked predicates, back in the record's aliases
        wt = sqlglot.parse_one(w, read=DIALECT)
        for c in list(wt.find_all(exp.Column)):
            if c.table in ds:
                c.replace(field_sql(c.table, c.name))
        window.append(wt.sql(dialect=DIALECT))
    return {"formula": tree.sql(dialect=DIALECT), "base": ds[base]["source"], "base_alias": alias[base],
            "joins": joins, "dimensions": dims, "timeframe_column": tf, "window": window,
            "is_ratio": isinstance(tree, exp.Div), "zero_denominator": "zero" if zero else None,
            "parameters": ctx.get("parameters") or []}


def write_back(doc: dict, metric_name: str, record: dict, registry_version: int | None = None) -> dict:
    """Id-present case. Compares the Ossie metric with the registry record converted forward and
    returns a patch for the registry: {} when nothing an editor can change differs, else
    {"expected_version", "new_version", "set": {registry field: new value}}. Raises Conflict when
    the registry's current definition version is not the one the Ossie file was built from."""
    m = _metric(doc, metric_name)
    reg = _registry_of(m)
    if reg.get("id") != record.get("id"):
        raise NotWritable(f"{metric_name!r} was made from registry metric {reg.get('id')!r}, not {record.get('id')!r}")
    current = registry_version if registry_version is not None else record["metadata"]["definition"]["version"]
    if current != reg["definition_version"]:
        raise Conflict(f"registry is at definition version {current}, Ossie file was built from "
                       f"{reg['definition_version']}")
    converted = convert(record)
    # a split metric is matched by the output column it came from, a single metric by the
    # registry id already checked above: a renamed metric is still found
    key = reg.get("output_column")
    if key:
        part = next((x for x in converted if _registry_of(x[0]).get("output_column") == key), None)
        if part is None:
            raise NotWritable(f"{metric_name!r} is not produced by registry metric {record['name']!r}")
    else:
        part = converted[0]
    fm, fds, frels = part
    fdoc = {"datasets": fds, "relationships": frels, "metrics": [fm]}
    split = len(converted) > 1 or bool(key)
    rd0 = record["metadata"]["definition"]
    with _parsing(rd0.get("dialect")):
        try:
            names = record_aliases(record)
            zero = (rd0.get("aggregation") or {}).get("zero_denominator") == "zero"
            mine, theirs = _reverse_parts(doc, m, names, zero), _reverse_parts(fdoc, fm, names, zero)
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
            raise NotConvertible(f"an expression in the Ossie file does not parse: {str(e).splitlines()[0][:80]}") from None
    patch = {}
    if m["name"] != fm["name"]:
        patch["name"] = m["name"]
    if m.get("description") != fm.get("description"):
        desc = m.get("description") or ""
        if split:                                     # the generated "This part: ..." sentence is not registry text
            base_desc = (record.get("description") or "").rstrip(".")
            suffix = fm["description"][len(base_desc):]      # ". This part: <col> = <aggregate>."
            desc = desc.replace(suffix, "").strip()
        if desc != (record.get("description") or ""):
            patch["description"] = desc
    said = (m.get("ai_context") or {}).get("instructions")
    if "instructions" in (m.get("ai_context") or {}) and said != (fm.get("ai_context") or {}).get("instructions"):
        raise NotWritable("ai_context.instructions is generated from agent_context; edit the "
                          "agent_context keys (grain, population, ...) instead")
    if m.get("datatype") and m.get("datatype") != fm.get("datatype"):
        patch["output.datatype"] = DTYPE_BACK.get(m["datatype"], "unknown")
    rd = record["metadata"]["definition"]
    if mine["zero_denominator"] != theirs["zero_denominator"]:   # the editor removed the COALESCE wrapper
        patch["aggregation.zero_denominator"] = "null"
    if mine["formula"] != theirs["formula"]:
        patch["formula"] = mine["formula"]          # the folded filters are now inside it
        patch["filters"] = unfolded_filters(record)   # the windows and parameter picks were never folded
        patch["mandatory_filters"] = []
        if re.match(r"\s*(WITH|SELECT)\b", rd["formula"], re.I) or (rd.get("source") or {}).get("sql"):
            # the record's filters, joins and group-by lived in its query or row-set source: the
            # new formula is an expression over a plain table, so the source and structure move too
            patch["source.table"] = mine["base"]
            patch["source.alias"] = mine["base_alias"]
            patch["source.sql"] = None
            patch["dimensions"] = mine["dimensions"]
            patch["joins"] = mine["joins"]
    q_now = (m.get("ai_context") or {}).get("examples") or []
    if q_now != (fm.get("ai_context") or {}).get("examples", []):
        patch["examples[].question"] = q_now
    ctx_now = agent_context(m)
    ctx_reg = agent_context(fm)
    for k, path in SEMANTICS.items():
        if ctx_now.get(k) != ctx_reg.get(k):
            patch[path] = ctx_now.get(k)
    if mine["dimensions"] != theirs["dimensions"]:
        patch["dimensions"] = mine["dimensions"]
    if mine["joins"] != theirs["joins"]:
        patch["joins"] = mine["joins"]
    if patch.get("joins") and "formula" not in patch and (rd.get("source") or {}).get("sql"):
        patch["source.table"] = mine["base"]          # the joins lived in the row-set source
        patch["source.sql"] = None
    if split:
        # One registry query holds all N parts: structural edits to one part cannot be folded
        # back without rewriting the others, so only metric-level text may change.
        structural = sorted(set(patch) - {"description", "examples[].question", *SEMANTICS.values()})
        if structural:
            raise NotWritable(f"{metric_name!r} was split from a whole-query formula; "
                              f"{', '.join(structural)} cannot be written back from one part. "
                              "Edit the registry query instead.")
    if not patch:
        return {}
    return {"expected_version": current, "new_version": current + 1, "set": patch}


def _examples_back(questions: list[str], exs: list[dict]) -> list[dict]:
    """Registry examples from Ossie's questions and the Atlan block's SQL entries: an SQL entry
    that names its question is paired with it; the other questions stand alone."""
    out = [{**({"question": e["question"]} if e.get("question") else {}), "sql": e.get("sql", ""), "source": "ossie"}
           for e in exs]
    paired = {e.get("question") for e in exs}
    out += [{"question": q, "sql": "", "source": "ossie"} for q in questions if q not in paired]
    return out


def to_registry(doc: dict, metric_name: str) -> dict:
    """New-metric case (no registry id): rebuild a registry definition from Ossie alone.
    Filters stay inside the formula (Ossie cannot say which CASE WHEN was a filter)."""
    m = _metric(doc, metric_name)
    ctx0 = agent_context(m)
    dialect = ctx0.get("sql_dialect", "snowflake")
    with _parsing(dialect):
        try:
            parts = _reverse_parts(doc, m)
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
            raise NotConvertible(f"an expression in the Ossie file does not parse: {str(e).splitlines()[0][:80]}") from None
    qs = (m.get("ai_context") or {}).get("examples") or []
    ctx = agent_context(m)
    sem = {}
    for k, path in SEMANTICS.items():
        if ctx.get(k) not in (None, "", [], {}):
            node = sem
            *head, last = path.split(".")
            for h in head:
                node = node.setdefault(h, {})
            node[last] = ctx[k]
    defn = {
        "spec_version": "1.0-draft.4", "version": 1, "dialect": dialect,
        "formula": parts["formula"],
        "aggregation": {"type": "ratio" if parts["is_ratio"] else "custom", **sem.pop("aggregation", {})},
        "output": {"alias": m["name"], "datatype": DTYPE_BACK.get(m.get("datatype"), "unknown"),
                   **sem.pop("output", {})},
        "source": {"entity": parts["base"], "alias": "m", "table": parts["base"], "sql": None,
                   "parameters": parts["parameters"],
                   **({"timeframe_column": parts["timeframe_column"]} if parts["timeframe_column"] else {})},
        "joins": parts["joins"], "dimensions": parts["dimensions"],
        **({"filters": parts["window"]} if parts["window"] else {}),   # parked, see split_window
        "examples": _examples_back(qs, ctx.get("examples") or []),
        **sem,
    }
    return {"name": m["name"], "description": m.get("description", ""), "definition": defn}
