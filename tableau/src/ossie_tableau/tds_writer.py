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

"""Ossie -> Tableau logical-layer TDS writer (object-graph form, modelled on a Snowflake semantic-view export).

Expression translation order: TABLEAU dialect verbatim (with [Dataset].[field] rewritten to local column names), then a
sqlglot translation of simple aggregates over dataset.column, otherwise a classified `untranslatable_expression` loss.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ossie.models import OssieDataType, OssieDataset, OssieDocument, OssieField

from . import stash
from .convert import Loss
from .calc_parser import Untranslatable, from_sql as calc_from_sql
from .formula import tokenize
from .warehouse import BIGQUERY, SNOWFLAKE, Connection

_REPLAYED_RAW = {
    "aliases", "semantic-values", "parameter", "filter", "drill-paths",
    "default-sorts", "field-sort-info", "group", "column-instance", "calc",
    "one-time-sql", "connection-customization", "extract",
}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?!\s*\()")

USER_NS = "http://www.tableausoftware.com/xml/user"
ET.register_namespace("user", USER_NS)
PFX_FOLDERS = "folders-common"

TDS_TYPE = {
    OssieDataType.STRING: ("string", 129), OssieDataType.INTEGER: ("integer", 131), OssieDataType.DECIMAL: ("real", 131),
    OssieDataType.FLOAT: ("real", 5), OssieDataType.BOOLEAN: ("boolean", 11), OssieDataType.DATE: ("date", 7),
    OssieDataType.TIME: ("string", 129), OssieDataType.DATE_TIME: ("datetime", 135),
    OssieDataType.DATE_TIME_TZ: ("datetime", 135), OssieDataType.OPAQUE: ("string", 129),
}
_PAIR = re.compile(r"\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]")
_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def SnowflakeConn(server: str, warehouse: str = "", database: str = "", schema: str = "", role: str = "", username: str = "") -> Connection:
    return Connection(SNOWFLAKE, server, database, schema, role, username, {"warehouse": warehouse})


def BigQueryConn(project: str, dataset: str = "", billing_project: str = "", username: str = "", server: str = BIGQUERY.default_server) -> Connection:
    """`project` is the data project (CATALOG); `billing_project` is the project that runs queries (EXECCATALOG, defaults to `project`)."""
    return Connection(BIGQUERY, server, project, dataset, "", username, {"project": project, "EXECCATALOG": billing_project or project})


@dataclass
class TdsResult:
    xml: str
    losses: list[Loss] = field(default_factory=list)


def _h(*parts: str, n: int = 32) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:n]


def _el(tag: str, attrs: dict | None = None, *kids: ET.Element, text: str | None = None) -> ET.Element:
    e = ET.Element(tag, {k: str(v) for k, v in sorted((attrs or {}).items())})
    e.text = text
    e.extend(kids)
    return e


def _snippet(xml: str) -> ET.Element:
    """Parse a stashed element; `user:` attributes are re-spelled literally because the datasource root already declares the prefix."""
    e = ET.fromstring(xml)
    for x in e.iter():
        for k in [k for k in x.attrib if k.startswith("{" + USER_NS + "}")]:
            x.attrib["user:" + k.split("}", 1)[1]] = x.attrib.pop(k)
    return e


def _ai_context_el(ctx) -> ET.Element | None:
    if ctx is None:
        return None
    if isinstance(ctx, str):
        body = {"instructions": ctx} if ctx else None
    else:
        body = ctx.model_dump(mode="json", exclude_none=True)
    if not body:
        return None
    return _el("user:ossie-ai-context", text=json.dumps(body, sort_keys=True))


def _collect_foreign(doc: OssieDocument) -> list[dict]:
    out: list[dict] = []

    def add(path: str, obj) -> None:
        for x in getattr(obj, "custom_extensions", None) or []:
            if x.vendor_name != "TABLEAU":
                out.append({"path": path, "vendor_name": x.vendor_name, "data": x.data})

    add("model", doc)
    for ds in doc.datasets:
        add(f"dataset:{ds.name}", ds)
        for f in ds.fields or []:
            add(f"field:{ds.name}.{f.name}", f)
    for r in doc.relationships or []:
        add(f"relationship:{r.name}", r)
    for m in doc.metrics or []:
        add(f"metric:{m.name}", m)
    return out


def _tableau_ext(obj) -> dict:
    for x in getattr(obj, "custom_extensions", None) or []:
        if x.vendor_name == "TABLEAU":
            try:
                return json.loads(x.data)
            except ValueError:
                return {}
    return {}


def _dialect(expr, *names: str) -> str | None:
    for n in names:
        for d in expr.dialects:
            if d.dialect.value == n:
                return d.expression
    return None


def _pick(expr, wh) -> tuple[str | None, str | None]:
    """The preferred SQL text of `expr` and the sqlglot dialect to read it with (None = generic ANSI)."""
    for n in wh.dialect_preference:
        sql = _dialect(expr, n)
        if sql is not None:
            return sql, (wh.sqlglot_dialect if n == wh.ossie_dialect else None)
    return None, None


def _bare_column(sql: str, dialect: str | None) -> str | None:
    try:
        n = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    return n.name if isinstance(n, exp.Column) and not n.table else None


def _dataset_aliases(ds: OssieDataset) -> set[str]:
    """Names SQL may use for this dataset: Ossie name and the last segment of a warehouse source."""
    out = {ds.name.lower()}
    src = (ds.source or "").strip()
    if src and not re.match(r"(?is)^\(?\s*(select|with)\b", src):
        parts = [p.strip('"`[]') for p in src.split(".") if p.strip('"`[]')]
        if parts:
            out.add(parts[-1].lower())
    return out


_SQL_IDENT = r"(?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*"


def _excluding_window_to_tableau(sql: str, resolve) -> str | None:
    """Narrow Superstore `PARTITION BY EXCLUDING` windows → Tableau table calcs. None for any other shape."""
    if not re.search(r"PARTITION\s+BY\s+EXCLUDING", sql, re.I):
        return None
    s = re.sub(r"\s+", " ", sql.strip())
    m = re.fullmatch(
        rf"(SUM|AVG|MIN|MAX)\s*\(\s*({_SQL_IDENT})\s*\)\s+OVER\s*\(\s*"
        rf"PARTITION\s+BY\s+EXCLUDING\s+{_SQL_IDENT}\s+ORDER\s+BY\s+{_SQL_IDENT}\s+"
        rf"ROWS\s+BETWEEN\s+UNBOUNDED\s+PRECEDING\s+AND\s+CURRENT\s+ROW\s*\)",
        s, re.I)
    if m:
        tab = resolve(m.group(2))
        if not tab:
            return None
        return f"RUNNING_{m.group(1).upper()}({tab})"
    m = re.fullmatch(
        rf"AVG\s*\(\s*({_SQL_IDENT})\s*\)\s+OVER\s*\(\s*"
        rf"PARTITION\s+BY\s+EXCLUDING\s+{_SQL_IDENT}\s+ORDER\s+BY\s+{_SQL_IDENT}\s+"
        rf"ROWS\s+BETWEEN\s+(\d+)\s+PRECEDING\s+AND\s+CURRENT\s+ROW\s*\)",
        s, re.I)
    if m:
        tab = resolve(m.group(1))
        if not tab:
            return None
        return f"WINDOW_AVG({tab}, -{m.group(2)}, 0)"
    m = re.fullmatch(
        rf"({_SQL_IDENT})\s*-\s*LAG\s*\(\s*({_SQL_IDENT})\s*(?:,\s*1)?\s*\)\s+OVER\s*\(\s*"
        rf"PARTITION\s+BY\s+EXCLUDING\s+{_SQL_IDENT}\s+ORDER\s+BY\s+{_SQL_IDENT}\s*\)",
        s, re.I)
    if m:
        left, right = resolve(m.group(1)), resolve(m.group(2))
        if not left or not right:
            return None
        return f"{left} - LOOKUP({right}, -1)"
    return None


def _physical_column(sql: str, dialect: str | None, ds: OssieDataset) -> str | None:
    """sourceColumn for a field: a bare identifier, or `table.column` when `table` is this dataset."""
    try:
        n = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return None
    if not isinstance(n, exp.Column):
        return None
    if not n.table or n.table.lower() in _dataset_aliases(ds):
        return n.name
    return None


def _unwrap_parens(s: str) -> str:
    """Remove one wrapping `( ... )` pair, only when the opening paren closes at the very end."""
    if not s.startswith("("):
        return s
    depth, i, n = 0, 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "'\"":
            j = i + 1
            while j < n and not (s[j] == ch and s[j + 1:j + 2] != ch):
                j += 2 if s[j] == ch else 1
            i = j
        elif s.startswith("--", i):
            i = s.find("\n", i)
            if i < 0:
                return s
        elif s.startswith("/*", i):
            i = s.find("*/", i)
            if i < 0:
                return s
            i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[1:-1] if i == n - 1 else s
        i += 1
    return s


def _source_relation(ds: OssieDataset, nc: str, db: str = "") -> ET.Element:
    s = ds.source.strip()
    if re.match(r"(?is)^\(?\s*(select|with)\b", s):
        return _el("relation", {"connection": nc, "name": ds.name, "type": "text"}, text=_unwrap_parens(s))
    parts = [p.strip('"`') for p in s.split(".")]
    if db and len(parts) == 2:
        parts = [db, *parts]  # Tableau Cloud fails multi-hop relationships on 2-part paths; keep the fully-qualified form
    return _el("relation", {"connection": nc, "name": ds.name, "table": ".".join(f"[{p}]" for p in parts), "type": "table"})


@dataclass
class _Col:
    ds: str
    name: str  # ossie field name
    local: str  # tableau local name, no brackets
    remote: str | None  # physical column; None for calculated
    dtype: str
    f: OssieField | None = None
    caption: str | None = None  # restored Tableau caption; None means "derive"
    has_caption: bool = False
    tds_type: str | None = None  # original Tableau datatype restored from a stash (e.g. spatial)


_SCHEMA_ORDER = {t: n for n, t in enumerate(
    "document-format-change-manifest repository-location connection utility-dimensions dimension overridable-settings aliases "
    "column column-instance group mapped-images drill-paths unlinked-server-hierarchies folders-common folders-parameters actions "
    "calculated-members extract layout style semantic-values date-options default-date-format default-sorts field-sort-info "
    "datasource-dependencies explainability filter object-graph".split())}


def write_tds(doc: OssieDocument, conn: Connection) -> TdsResult:
    losses: list[Loss] = []

    def restore(ext: dict, key: str, kind: str, witness, label: str):
        """Stashed data when it is still valid for the live `witness`; a stale stash is reported and ignored."""
        if key not in ext:
            return None
        try:
            data, ok = stash.restore(ext[key], witness, kind)
        except stash.StashError as e:
            losses.append(Loss("stash_stale", label, f"{key}: {e}"))
            return None
        if not ok:
            losses.append(Loss("stash_stale", label, f"{key}: the model changed since it was stashed; using the live value"))
            return None
        return data
    wh = conn.warehouse
    nc = wh.tableau_class + "." + _h(conn.server, conn.database, conn.schema, n=26)
    oid = {d.name: f"{d.name}_{_h(d.name).upper()}" for d in doc.datasets}
    table_to_ds: dict[str, str] = {}
    for ds in doc.datasets:
        for alias in _dataset_aliases(ds):
            table_to_ds.setdefault(alias, ds.name)

    declared: dict[str, dict[str, OssieField]] = {}
    for ds in doc.datasets:
        m = declared.setdefault(ds.name, {})
        for f in ds.fields or []:
            m[f.name.lower()] = f
            sql, read = _pick(f.expression, wh)
            if sql is not None and (b := _physical_column(sql, read, ds)) is not None:
                m.setdefault(b.lower(), f)
    extra: dict[str, list[OssieField]] = {}
    for r in doc.relationships or []:
        for dsn, names, other, onames in ((r.from_dataset, r.from_columns, r.to, r.to_columns), (r.to, r.to_columns, r.from_dataset, r.from_columns)):
            for col, ocol in zip(names, onames):
                if dsn not in declared or col.lower() in declared[dsn]:
                    continue
                peer = declared.get(other, {}).get(ocol.lower())
                dt = peer.datatype if peer is not None and peer.datatype is not None else OssieDataType.STRING
                nf = OssieField.model_validate({
                    "name": col, "datatype": dt.value, "dimension": {},
                    "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": col}]},
                    "custom_extensions": [{"vendor_name": "TABLEAU", "data": json.dumps({"hidden": True})}]})
                extra.setdefault(dsn, []).append(nf)
                declared[dsn][col.lower()] = nf
                losses.append(Loss("relationship_key_synthesized", f"{dsn}.{col}",
                                   f"join column is not a declared field; added as a hidden {dt.value} column"))

    cols: list[_Col] = []
    used: set[str] = set()
    for ds in doc.datasets:
        for f in list(ds.fields or []) + extra.get(ds.name, []):
            sql, read = _pick(f.expression, wh)
            tab = _dialect(f.expression, "TABLEAU")
            remote = _physical_column(sql, read, ds) if sql is not None else None
            if f.datatype is None:
                losses.append(Loss("datatype_defaulted", f"{ds.name}.{f.name}", "no datatype; defaulted from dimension flags"))
            if f.datatype is not None:
                dt = TDS_TYPE[f.datatype][0]
            elif f.dimension is not None and f.dimension.is_time:
                dt = "datetime"
            elif f.dimension is not None:
                dt = "string"
            else:
                dt = "real"
            fext = _tableau_ext(f)
            names = restore(fext, "names", "field_names", {"name": f.name, "label": f.label}, f"{ds.name}.{f.name}")
            tds_type = restore(fext, "datatype", "datatype", f.datatype.value if f.datatype else None, f"{ds.name}.{f.name}")
            if tds_type:
                dt = tds_type
            if remote is not None and tab is None:
                local = remote if remote.lower() not in used else f"{remote} ({ds.name})"
                if names and names["local"] and names["local"].lower() not in used:
                    local = names["local"]
                used.add(local.lower())
                cols.append(_Col(ds.name, f.name, local, remote, dt, f, names["caption"] if names else None, bool(names), tds_type))
            elif tab is not None or sql is not None:
                local = f"Calculation_{_h(ds.name, f.name, n=20)}"
                if names and names["local"] and names["local"].lower() not in used:
                    local = names["local"]
                used.add(local.lower())
                cols.append(_Col(ds.name, f.name, local, None, dt, f, names["caption"] if names else None, bool(names), tds_type))
            else:
                losses.append(Loss("untranslatable_expression", f"{ds.name}.{f.name}", f"not a column and no TABLEAU expression: {sql}"))

    by_key: dict[tuple[str, str], _Col] = {}
    for c in cols:
        by_key[(c.ds.lower(), c.name.lower())] = c
        if c.remote:
            by_key.setdefault((c.ds.lower(), c.remote.lower()), c)

    def lookup(ds_name: str | None, col: str) -> _Col | None:
        if ds_name:
            resolved = table_to_ds.get(ds_name.lower(), ds_name)
            return by_key.get((resolved.lower(), col.lower()))
        hits = [c for (d, n), c in by_key.items() if n == col.lower()]
        return hits[0] if len(hits) == 1 else None

    metric_locals: dict[str, str] = {}

    def _metric_bracket(name: str) -> str | None:
        local = metric_locals.get(name.lower())
        if not local:
            return None
        return "[" + local.replace("]", "]]") + "]"

    def rewrite_tableau(label: str, formula: str, home: str | None = None) -> str | None:
        """Rewrite [Dataset].[field] pairs and bare [field] refs (resolved within `home`) to local column names.

        After field lookup misses, an unqualified `[MetricName]` or a bare metric identifier
        becomes that metric's calculation local.
        """
        toks = tokenize(formula)
        out: list[str] = []
        bad: list[str] = []
        i = 0

        def q(c: _Col) -> str:
            return "[" + c.local.replace("]", "]]") + "]"
        while i < len(toks):
            t = toks[i]
            text = formula[t.start:t.end]
            if t.kind == "ref":
                if i + 2 < len(toks) and formula[toks[i + 1].start:toks[i + 1].end] == "." and toks[i + 2].kind == "ref":
                    c = lookup(t.value, toks[i + 2].value)
                    if c is None:
                        bad.append(f"[{t.value}].[{toks[i + 2].value}]")
                    out.append(q(c) if c else formula[t.start:toks[i + 2].end])
                    i += 3
                    continue
                c = lookup(home, t.value) if home else None
                if c is None:
                    c = lookup(None, t.value)
                if c is None:
                    qloc = _metric_bracket(t.value)
                    if qloc:
                        out.append(qloc)
                        i += 1
                        continue
                    bad.append(f"[{t.value}]")
                out.append(q(c) if c else text)
            else:
                if t.kind == "code" and metric_locals:
                    text = _IDENT.sub(lambda m: _metric_bracket(m.group(0)) or m.group(0), text)
                out.append(text)
            i += 1
        if bad:
            losses.append(Loss("untranslatable_expression", label, f"unresolved reference(s) {bad[:3]}"))
            return None
        return "".join(out)

    def from_sql(label: str, sql: str, home: str | None = None, read: str | None | bool = True) -> str | None:
        def ref(n: exp.Column) -> str:
            c = lookup(n.table or home, n.name) or (lookup(None, n.name) if not n.table else None)
            if c is None:
                local = metric_locals.get(n.name.lower())
                if local:
                    return "[" + local.replace("]", "]]") + "]"
                raise Untranslatable(f"unresolved column {n.sql()}")
            return "[" + c.local.replace("]", "]]") + "]"
        def resolve_ident(ident: str) -> str | None:
            table, name = (ident.rsplit(".", 1) if "." in ident else (None, ident))
            c = lookup(table, name) or (lookup(None, name) if not table else None)
            if c:
                return "[" + c.local.replace("]", "]]") + "]"
            local = metric_locals.get(name.lower())
            if local:
                return "[" + local.replace("]", "]]") + "]"
            return None

        try:
            return calc_from_sql(sql, ref, wh, read)
        except Untranslatable as e:
            tab_win = _excluding_window_to_tableau(sql, resolve_ident)
            if tab_win:
                losses.append(Loss(
                    "table_calc_translated", label,
                    "PARTITION BY EXCLUDING window emitted as a Tableau table calculation; addressing follows the viz, not EXCLUDING"))
                return tab_win
            losses.append(Loss("untranslatable_expression", label, str(e)[:200]))
            return None

    raw_items: list[dict] = []
    doc_ext = next((json.loads(x.data) for x in doc.custom_extensions or [] if x.vendor_name == "TABLEAU" and x.data.startswith("{")), {})
    if "raw" in doc_ext:
        raw_items = restore(doc_ext, "raw", "raw_xml", sorted(d.name for d in doc.datasets), "document") or []
    sql_items = list(dict.fromkeys(i["sql"] for i in raw_items if i.get("tag") == "one-time-sql"))
    one_time_sql = "\n".join(sql_items)
    if len(sql_items) > 1:
        losses.append(Loss("initial_sql_merged", "", f"{len(sql_items)} distinct initial-SQL statements from separate connections were joined onto the single {wh.name} connection"))

    # --- connection layer
    cattrs = {"authentication": "Username Password", "class": wh.tableau_class, "dbname": conn.database, "odbc-connect-string-extras": "",
              "one-time-sql": one_time_sql, "schema": conn.schema, "server": conn.server, "service": conn.role, "username": conn.username}
    if wh.database_attribute != "dbname":
        cattrs[wh.database_attribute] = conn.database
    for k in wh.omit_attributes:
        cattrs.pop(k, None)
    cattrs.update(wh.fixed_attributes)
    if conn.extra.get("authentication") and any(k == "authentication" for k, _ in wh.fixed_attributes):
        cattrs["authentication"] = conn.extra["authentication"]
    cattrs.update({k: conn.extra.get(k, "") for k in wh.extra_attributes})
    custom = [_snippet(i["xml"]) for i in raw_items if i.get("tag") == "connection-customization"][:1]
    named = _el("named-connections", None, _el("named-connection", {"caption": conn.server, "name": nc}, _el("connection", cattrs, *custom)))

    targets = {r.to for r in doc.relationships or []}
    roots = [d for d in doc.datasets if d.name not in targets]
    # Tableau pairs logical tables with collection relations by position: depth-first from the root through the relationships
    by_name = {d.name: d for d in doc.datasets}
    collection_order: list[OssieDataset] = []

    def _visit(d: OssieDataset) -> None:
        if d in collection_order:
            return
        collection_order.append(d)
        for r in doc.relationships or []:
            if r.from_dataset == d.name and r.to in by_name:
                _visit(by_name[r.to])

    for d in roots:
        _visit(d)
    for d in doc.datasets:
        _visit(d)
    collection = _el("relation", {"type": "collection"}, *[_source_relation(d, nc, conn.database) for d in collection_order])
    maps: list[ET.Element] = []
    records = []
    for ds in doc.datasets:
        for i, c in enumerate([c for c in cols if c.ds == ds.name and c.remote], 1):
            t, rt = TDS_TYPE.get(c.f.datatype, (c.dtype, 129)) if c.f and c.f.datatype else (c.dtype, 129 if c.dtype == "string" else 131)
            if c.tds_type:
                t = c.tds_type
            kids = [("remote-name", c.remote), ("remote-type", rt), ("local-name", f"[{c.local}]"), ("parent-name", f"[{ds.name}]"),
                    ("remote-alias", c.remote), ("ordinal", i), ("local-type", t)]
            rec = _el("metadata-record", {"class": "column"}, *[_el(k, text=str(v)) for k, v in kids])
            rec.append(_el("object-id", text=f"[{oid[ds.name]}]"))
            records.append(rec)
            maps.append(_el("map", {"key": f"[{c.local}]", "value": f"[{ds.name}].[{c.remote}]"}))

    connection = _el("connection", {"class": "federated"}, named, collection,
                     _el("cols", None, *maps), _el("metadata-records", None, *records))

    # --- logical layer columns
    columns: list[ET.Element] = []
    table_columns = [_el("column", {"caption": d.name, "datatype": "table", "name": f"[__tableau_internal_object_id__].[{oid[d.name]}]",
                                    "role": "measure", "type": "quantitative"}) for d in doc.datasets]
    folders: dict[str, list[str]] = {}
    for c in cols:
        ext = _tableau_ext(c.f)
        isdim = c.f.dimension is not None
        attrs = {"datatype": c.dtype, "name": f"[{c.local}]", "role": "dimension" if isdim else "measure",
                 "type": ("ordinal" if c.dtype in ("date", "datetime") else "nominal") if isdim else "quantitative"}
        cap = c.caption if c.has_caption else (c.f.label or (c.name if c.name != (c.remote or "") else None))
        if cap:
            attrs["caption"] = cap
        if ext.get("hidden"):
            attrs["hidden"] = "true"
        if ext.get("semantic_role"):
            attrs["semantic-role"] = ext["semantic_role"]
        if ext.get("default_format"):
            attrs["default-format"] = ext["default_format"]
        if not isdim:
            attrs["aggregation"] = ext.get("default_aggregation", "Sum")
        elif ext.get("default_aggregation"):
            attrs["aggregation"] = ext["default_aggregation"]
        kids: list[ET.Element] = []
        if c.f.description:
            kids.append(_el("desc", None, _el("formatted-text", None, _el("run", text=c.f.description))))
        if (aiel := _ai_context_el(c.f.ai_context)) is not None:
            kids.append(aiel)
        if c.remote is None:
            tab = _dialect(c.f.expression, "TABLEAU")
            if tab is not None:
                formula = rewrite_tableau(f"{c.ds}.{c.name}", tab, c.ds)
            else:
                sql, read = _pick(c.f.expression, wh)
                formula = from_sql(f"{c.ds}.{c.name}", sql or "", c.ds, read)
            if formula is None:
                continue
            saved = restore(ext, "tableau_calc", "tableau_calc", tab, f"{c.ds}.{c.name}") if tab is not None else None
            if saved is not None:
                kids.insert(0, ET.fromstring(saved["xml"]))
            else:
                kids.insert(0, _el("calculation", {"class": "tableau", "formula": formula}))
            if c.has_caption:
                if c.caption:
                    attrs["caption"] = c.caption
                else:
                    attrs.pop("caption", None)
            else:
                attrs["caption"] = c.name
        if ext.get("folder"):
            folders.setdefault(ext["folder"], []).append(f"[{c.local}]")
        columns.append(_el("column", attrs, *kids))

    def _metric_sql_refs(sql: str, read: str | None) -> list[tuple[str | None, str]]:
        try:
            tree = sqlglot.parse_one(sql, read=wh.sqlglot_dialect if read else read)
        except sqlglot.errors.SqlglotError:
            return [(a, b) for a, b in re.findall(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b", sql)]
        return [(c.table or None, c.name) for c in tree.find_all(exp.Column)]

    metrics = list(doc.metrics or [])
    metric_index = {m.name.lower(): m for m in metrics}

    def _metric_deps(m) -> list[str]:
        sql, read = _pick(m.expression, wh)
        if not sql:
            return []
        deps: list[str] = []
        for table, name in _metric_sql_refs(sql, read):
            if lookup(table, name) or (lookup(None, name) if not table else None):
                continue
            hit = metric_index.get(name.lower())
            if hit is not None and hit is not m:
                deps.append(hit.name.lower())
        return deps

    pending = dict.fromkeys(m.name.lower() for m in metrics)
    ordered: list = []
    while pending:
        ready = [metric_index[n] for n in list(pending) if all(d not in pending for d in _metric_deps(metric_index[n]))]
        if not ready:
            ready = [metric_index[n] for n in list(pending)]
        for m in ready:
            pending.pop(m.name.lower(), None)
            ordered.append(m)

    for m in ordered:
        tab = _dialect(m.expression, "TABLEAU")
        sql, read = _pick(m.expression, wh)
        formula = rewrite_tableau(m.name, tab) if tab is not None else (from_sql(m.name, sql, None, read) if sql is not None else None)
        if formula is None:
            if tab is None and sql is None:
                losses.append(Loss("untranslatable_expression", m.name, f"no TABLEAU/{wh.ossie_dialect}/ANSI_SQL expression"))
            continue
        dt = TDS_TYPE[m.datatype][0] if m.datatype else "real"
        kids = []
        if m.description:
            kids.append(_el("desc", None, _el("formatted-text", None, _el("run", text=m.description))))
        if (aiel := _ai_context_el(m.ai_context)) is not None:
            kids.append(aiel)
        mext = _tableau_ext(m)
        names = restore(mext, "names", "field_names", {"name": m.name, "label": None}, m.name)
        local = f"Calculation_{_h('metric', m.name, n=20)}"
        local_restored = False
        if names and names["local"] and names["local"].lower() not in used:
            local = names["local"]
            local_restored = True
        used.add(local.lower())
        metric_locals[m.name.lower()] = local
        mattrs = {"datatype": dt, "name": f"[{local}]", "role": "measure", "type": "quantitative"}
        mcap = (names["caption"] or (None if local_restored else m.name)) if names else m.name
        if mcap:
            mattrs["caption"] = mcap
        if mext.get("hidden"):
            mattrs["hidden"] = "true"
        for key, attr in (("semantic_role", "semantic-role"), ("default_format", "default-format"), ("default_aggregation", "aggregation")):
            if mext.get(key):
                mattrs[attr] = mext[key]
        if mext.get("folder"):
            folders.setdefault(mext["folder"], []).append(f"[{local}]")
        columns.append(_el("column", mattrs, _el("calculation", {"class": "tableau", "formula": formula}), *kids))

    # --- object graph
    objects = []
    for d in doc.datasets:
        okids = [_el("properties", {"context": ""}, _source_relation(d, nc, conn.database))]
        if (aiel := _ai_context_el(d.ai_context)) is not None:
            okids.append(aiel)
        objects.append(_el("object", {"caption": d.name, "id": oid[d.name]}, *okids))
    rels = []
    by_ds = {d.name: d for d in doc.datasets}
    for r in doc.relationships or []:
        pairs = []
        for fc, tc in zip(r.from_columns, r.to_columns):
            a, b = lookup(r.from_dataset, fc), lookup(r.to, tc)
            if a is None or b is None:
                losses.append(Loss("relationship", r.name, f"column {fc if a is None else tc} not found"))
                pairs = []
                break
            # object-graph operands are physical column names; restored local names (e.g. Excel's 'sheet$'_col) are not
            pairs.append(tuple(x.local if (x.remote and x.local.upper().startswith(x.remote.upper() + " (")) or not (x.has_caption and x.remote) else x.remote for x in (a, b)))
        if not pairs:
            continue
        to_ds = by_ds.get(r.to)
        keysets = [set(map(str.lower, k)) for k in ([to_ds.primary_key] if to_ds and to_ds.primary_key else []) + list(to_ds.unique_keys or [])] if to_ds else []
        uniq = any(k and k <= {x.lower() for x in r.to_columns} for k in keysets)
        a, b = pairs[0]
        e2 = {"object-id": oid[r.to]}
        if uniq:
            e2["unique-key"] = "true"
        eqs = [_el("expression", {"op": "="}, _el("expression", {"op": f"[{x}]"}), _el("expression", {"op": f"[{y}]"})) for x, y in pairs]
        expr = eqs[0] if len(eqs) == 1 else _el("expression", {"op": "AND"}, *eqs)
        rels.append(_el("relationship", None, expr,
                        _el("first-end-point", {"object-id": oid[r.from_dataset]}), _el("second-end-point", e2)))

    og_kids = [_el("objects", None, *objects)] + ([_el("relationships", None, *rels)] if rels else [])

    by_col = {c.get("name"): c for c in columns}
    for i in raw_items:
        if i["tag"] in ("aliases", "semantic-values") and "column" in i and f"[{i['column']}]" in by_col:
            by_col[f"[{i['column']}]"].append(_snippet(i["xml"]))
    param_cols = [_snippet(i["xml"]) for i in raw_items if i["tag"] == "parameter"]
    filters = [_snippet(i["xml"]) for i in raw_items if i["tag"] == "filter"]
    tail = [_snippet(i["xml"]) for i in raw_items if i["tag"] in ("drill-paths", "default-sorts", "field-sort-info")]
    groups = [_snippet(i["xml"]) for i in raw_items if i["tag"] in ("group", "column-instance")]
    semantic_values = [_snippet(i["xml"]) for i in raw_items if i["tag"] == "semantic-values" and "column" not in i]
    calc_cols = []
    for i in raw_items:
        if i["tag"] == "calc":
            el = _snippet(i["xml"])
            if el.get("name") not in by_col:
                calc_cols.append(el)
                by_col[el.get("name")] = el
    if filters:
        losses.append(Loss("datasource_filter_restored", doc.name, f"{len(filters)} data-source filter(s) written back; query results are filtered as in Tableau"))

    # Restore the original root attributes while the model name is unchanged; a datasource without a caption stays without one
    # (Tableau shows the published name), and a generated `federated_*` name is never written as a caption.
    sroot = doc_ext.get("root") if isinstance(doc_ext.get("root"), dict) and doc_ext["root"].get("name") == doc.name else None
    root_attrs = {"formatted-name": (sroot or {}).get("formatted_name") or "federated." + _h(doc.name, conn.server, n=26), "inline": "true",
                  "version": "18.1", "xmlns:user": "http://www.tableausoftware.com/xml/user"}
    if sroot and sroot.get("source_platform"):
        root_attrs["source-platform"] = sroot["source_platform"]
    caption = sroot.get("caption") if sroot is not None else (None if re.match(r"federated[._]", doc.name) else doc.name)
    if caption:
        root_attrs["caption"] = caption
    root = _el("datasource", root_attrs,
               _el("document-format-change-manifest", None,
                   _el("ObjectModelEncapsulateLegacy"), _el("ObjectModelRelationshipPerfOptions"), _el("ObjectModelTableType"),
                   _el("SchemaViewerObjectModel")),
               connection, *filters, _el("aliases", {"enabled": "yes"}), *columns, *calc_cols, *table_columns, *groups, *param_cols)
    if folders:
        root.append(_el(PFX_FOLDERS, None, *[_el("folder", {"name": n}, *[_el("folder-item", {"name": i, "type": "field"}) for i in items])
                                              for n, items in sorted(folders.items())]))
    root.append(_el("layout", {"dim-ordering": "alphabetic", "measure-ordering": "alphabetic", "show-structure": "false"}))
    root.extend(semantic_values)
    root.append(_el("object-graph", None, *og_kids))
    root.extend(tail)
    if (aiel := _ai_context_el(doc.ai_context)) is not None:
        root.append(aiel)
    foreign = _collect_foreign(doc)
    if foreign:
        root.append(_el("user:ossie-foreign-extensions", text=json.dumps(foreign)))
    for i in raw_items:
        if i.get("tag") not in _REPLAYED_RAW and i.get("xml"):
            root.append(_snippet(i["xml"]))
    root[:] = sorted(root, key=lambda e: _SCHEMA_ORDER.get(e.tag.rsplit("}", 1)[-1].rsplit("...", 1)[-1], len(_SCHEMA_ORDER)))
    ET.indent(root, space="  ")
    return TdsResult('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n", losses)
