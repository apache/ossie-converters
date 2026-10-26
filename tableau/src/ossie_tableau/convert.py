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

"""Tableau classic datasource (.tds/.twb/.twbx/.tdsx) to Ossie flat document."""
from __future__ import annotations

import copy
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot

from . import stash
from .formula import masked, refs as formula_refs, sub_refs, tokenize
from .losses import spec as loss_spec
from .warehouse import DEFAULT, GENERIC, WAREHOUSES, Warehouse, by_tableau_class, needs_quote

from ossie.models import (
    OssieAIContextObject,
    OssieCustomExtension,
    OssieDataset,
    OssieDataType,
    OssieDialect,
    OssieDialectExpression,
    OssieDimension,
    OssieDocument,
    OssieExpression,
    OssieField,
    OssieMetric,
    OssieRelationship,
)


AGG_RE = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(", re.I)
REF_RE = re.compile(r"\[([^\[\]]+)\]")
NUMERIC_AGG_RE = re.compile(r"\b(SUM|AVG|MIN|MAX|MEDIAN)\s*\(\s*\[[^\[\]]+\]\s*\)", re.I)
BARE_AGG_RE = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(\s*\[[^\[\]]+\]\s*\)", re.I)
RLS_RE = re.compile(r"\b(ISMEMBEROF|USERNAME|FULLNAME|ISFULLNAME|ISUSERNAME|USERDOMAIN|ISCURRENTUSER)\s*\(", re.I)
# applied to formula.masked(): strings/comments blanked and reference names flattened
UNSUPPORTED = [
    ("lod", re.compile(r"\{")),
    ("table_calc", re.compile(r"\b(WINDOW_\w+|RUNNING_\w+|LOOKUP|INDEX|FIRST|LAST|SIZE|RANK\w*|PREVIOUS_VALUE|TOTAL)\s*\(", re.I)),
    ("rawsql", re.compile(r"\bRAWSQL\w*\s*\(", re.I)),
]
_AGG1 = r"(?:SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(\s*\[[^\[\]]+\]\s*\)"
# whole-formula shapes proven equivalent to `AGG(x) OVER ()` in VDS and Snowflake (see FINDINGS.md, live gate)
_PAIR = lambda w, aggs: rf"{w}\s*\(\s*(?:{aggs})\s*\(\s*\[[^\[\]]+\]\s*\)\s*\)"
LOD_WINDOW_SHAPE = re.compile(
    rf"^\s*(?:\{{\s*(?:FIXED\s*)?:?\s*{_AGG1}\s*\}}|TOTAL\s*\(\s*{_AGG1}\s*\)"
    rf"|{_PAIR('WINDOW_SUM', 'SUM|COUNT')}|{_PAIR('WINDOW_MIN', 'MIN')}|{_PAIR('WINDOW_MAX', 'MAX')})\s*$", re.I)
_TC_REF = r"\[[^\[\]]+\]"
_TC_AGG = rf"(?:SUM|AVG|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(\s*{_TC_REF}\s*\)"
_TC_BODY = rf"(?:{_TC_AGG}|{_TC_REF})"
_NUM = r"-?\d+(?:\.\d+)?"
_TC_ATOM = (
    rf"(?:RUNNING_SUM\s*\(\s*{_TC_BODY}\s*\)"
    rf"|RANK(?:_DENSE|_UNIQUE|_PERCENTILE)?\s*\(\s*{_TC_BODY}\s*\)"
    rf"|WINDOW_COUNT\s*\(\s*{_TC_BODY}(?:\s*,\s*-?\d+\s*,\s*-?\d+)?\s*\)"
    rf"|INDEX\s*\(\s*\)"
    rf"|FIRST\s*\(\s*\)"
    rf"|LAST\s*\(\s*\)"
    rf"|SIZE\s*\(\s*\)"
    rf"|PREVIOUS_VALUE\s*\(\s*(?:{_TC_BODY}|{_NUM})\s*\)"
    rf"|WINDOW_(?:SUM|AVG|MIN|MAX)\s*\(\s*{_TC_BODY}\s*,\s*-?\d+\s*,\s*-?\d+\s*\)"
    rf"|WINDOW_(?:SUM|AVG|MIN|MAX)\s*\(\s*{_TC_BODY}\s*\)"
    rf"|{_TC_BODY}\s*-\s*LOOKUP\s*\(\s*{_TC_BODY}\s*,\s*-1\s*\)"
    rf"|LOOKUP\s*\(\s*{_TC_BODY}\s*,\s*(?:{_NUM}|FIRST\s*\(\s*\))\s*\))"
)
TABLE_CALC_METRIC_SHAPE = re.compile(
    rf"^\s*(?:{_TC_ATOM}(?:\s*[+\-*/]\s*{_NUM})?|{_NUM}\s*[+\-*/]\s*{_TC_ATOM})\s*$",
    re.I)
# viz-dependent LOD kept as metrics with SQL dialects (quiet addressing note): INCLUDE/EXCLUDE, brace/{FIXED : AGG}, {FIXED [d]: [x]}
# plus one binary + - * / with a numeric literal on either side. Brace/{FIXED : AGG} is reconciled; others get sql_unreconciled.
_LOD_DIMS = r"\[[^\[\]]+\](?:\s*,\s*\[[^\[\]]+\])*"
_LOD_ATOM = (
    rf"(?:\{{\s*(?:INCLUDE|EXCLUDE)\s+{_LOD_DIMS}\s*:\s*{_AGG1}\s*\}}"
    rf"|\{{\s*(?:FIXED\s*)?:?\s*{_AGG1}\s*\}}"
    rf"|\{{\s*FIXED\s+{_LOD_DIMS}\s*:\s*{_AGG1}\s*\}}"
    rf"|\{{\s*FIXED\s+{_LOD_DIMS}\s*:\s*{_TC_REF}\s*\}})"
)
LOD_VIZ_SHAPE = re.compile(
    rf"^\s*(?:{_LOD_ATOM}(?:\s*[+\-*/]\s*{_NUM})?|{_NUM}\s*[+\-*/]\s*{_LOD_ATOM})\s*$",
    re.I)
# Whole-formula shapes whose window SQL matches table-across on a fixed sort (BQ + SF live gates).
# INCLUDE/EXCLUDE, PREVIOUS_VALUE, and offset/mismatched WINDOW_* stay sql_unreconciled.
# {FIXED [d]: [field]} is reconciled after FIRST_VALUE (not MAX) + first-row-per-partition truth.
# 22–24 (INDEX()>1, LOOKUP IS NULL, WINDOW_COUNT, RANK_PERCENTILE, LOOKUP FIRST()) after BQ 49/49 + SF 37/37.
_RECONCILED_VIZ = re.compile(
    rf"^\s*(?:"
    rf"SIZE\s*\(\s*\)(?:\s*\*\s*{_NUM})?"
    rf"|INDEX\s*\(\s*\)(?:\s*\+\s*{_NUM})?"
    rf"|INDEX\s*\(\s*\)\s*>\s*{_NUM}"
    rf"|{_NUM}\s*\+\s*INDEX\s*\(\s*\)"
    rf"|FIRST\s*\(\s*\)"
    rf"|LAST\s*\(\s*\)"
    rf"|FIRST\s*\(\s*\)\s*-\s*LAST\s*\(\s*\)"
    rf"|\(\s*INDEX\s*\(\s*\)\s*\+\s*{_NUM}\s*\)\s*\*\s*SIZE\s*\(\s*\)"
    rf"|\(\s*FIRST\s*\(\s*\)\s*-\s*{_NUM}\s*\)\s*\+\s*SIZE\s*\(\s*\)"
    rf"|INDEX\s*\(\s*\)\s*\+\s*RANK(?:_DENSE|_UNIQUE)?\s*\(\s*{_TC_BODY}\s*\)"
    rf"|RANK(?:_DENSE|_UNIQUE)?\s*\(\s*{_TC_BODY}\s*\)"
    rf"|RANK_PERCENTILE\s*\(\s*{_TC_BODY}\s*\)"
    rf"|WINDOW_COUNT\s*\(\s*{_TC_BODY}(?:\s*,\s*-?\d+\s*,\s*-?\d+)?\s*\)"
    rf"|LOOKUP\s*\(\s*{_TC_BODY}\s*,\s*{_NUM}\s*\)(?:\s+IS\s+NULL)?"
    rf"|LOOKUP\s*\(\s*{_TC_BODY}\s*,\s*FIRST\s*\(\s*\)\s*\)"
    rf"|IIF\s*\(\s*INDEX\s*\(\s*\)\s*>\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*\)"
    rf"|IF\s+FIRST\s*\(\s*\)\s*=\s*0\s+THEN\s+{_TC_AGG}\s+END"
    rf"|\{{\s*FIXED\s+{_LOD_DIMS}\s*:\s*{_AGG1}\s*\}}\s*[+\-*/]\s*{_NUM}"
    rf"|\{{\s*FIXED\s+{_LOD_DIMS}\s*:\s*{_TC_REF}\s*\}}"
    rf"|\{{\s*FIXED\s+{_LOD_DIMS}\s*:\s*{_TC_REF}\s*\}}\s*[+\-*/]\s*{_NUM}"
    rf")\s*$",
    re.I)
_TC_ATOM_RE = re.compile(_TC_ATOM, re.I)
_LOD_ATOM_RE = re.compile(_LOD_ATOM, re.I)
_FOLD_BIN = re.compile(rf"(?:@|{_NUM})\s*[+\-*/]\s*(?:@|{_NUM})")
_FOLD_PAREN = re.compile(r"\(\s*@\s*\)")
_FOLD_CMP = re.compile(rf"(?:@|{_NUM})\s*(?:<=|>=|<>|=|<|>)\s*(?:@|{_NUM})")
_FOLD_IIF = re.compile(rf"IIF\s*\(\s*@\s*,\s*(?:@|{_NUM}|{_AGG1})\s*,\s*(?:@|{_NUM}|{_AGG1})\s*\)", re.I)
_FOLD_IF = re.compile(
    rf"IF\s+@\s+THEN\s+(?:@|{_NUM}|{_AGG1})(?:\s+ELSE\s+(?:@|{_NUM}|{_AGG1}))?\s+END", re.I)
_FOLD_ISNULL = re.compile(r"@\s+IS\s+NULL", re.I)


def _viz_arith_formula(masked: str) -> bool:
    """True when `masked` is recognized table-calc / viz-LOD atoms combined with + - * /, comparisons, IF/IIF, and parentheses."""
    s = masked.strip()
    spans: list[tuple[int, int]] = []
    for rx in (_TC_ATOM_RE, _LOD_ATOM_RE):
        spans.extend(m.span() for m in rx.finditer(s))
    spans.sort(key=lambda p: (p[0], p[0] - p[1]))
    kept: list[tuple[int, int]] = []
    for a, b in spans:
        if any(a < kb and b > ka for ka, kb in kept):
            continue
        kept.append((a, b))
    kept.sort()
    parts: list[str] = []
    i = 0
    for a, b in kept:
        parts.append(s[i:a])
        parts.append("@")
        i = b
    parts.append(s[i:])
    s = "".join(parts)
    prev = None
    while s != prev:
        prev = s
        s = _FOLD_PAREN.sub("@", s)
        s = _FOLD_CMP.sub("@", s)
        s = _FOLD_ISNULL.sub("@", s)
        s = _FOLD_IIF.sub("@", s)
        s = _FOLD_IF.sub("@", s)
        s = _FOLD_BIN.sub("@", s)
    return s.strip() == "@"
# `{FIXED [d], ... : AGG([x])}`: a row-level field when dims and measure sit in one dataset
LOD_FIXED_FIELD = re.compile(rf"^\s*\{{\s*FIXED\s+\[[^\[\]]+\](?:\s*,\s*\[[^\[\]]+\])*\s*:\s*{_AGG1}\s*\}}\s*$", re.I)
CALC_CLASS_LOSS = {"bin": "bin", "categorical-bin": "categorical_bin", "passthrough": "passthrough"}
RAWSQL_RE = re.compile(
    r"^\s*RAWSQL_(?:BOOL|DATE|DATETIME|INT|REAL|STR)\s*\(\s*(?P<q>['\"])(?P<sql>.*?)(?P=q)\s*(?P<args>(?:,\s*\[[^\]]+\]\s*)*)\)\s*$", re.S | re.I)
OBJECT_ID_REF = "__tableau_internal_object_id__"
TYPE_MAP = {
    "string": OssieDataType.STRING,
    "integer": OssieDataType.INTEGER,
    "real": OssieDataType.FLOAT,
    "boolean": OssieDataType.BOOLEAN,
    "date": OssieDataType.DATE,
    "datetime": OssieDataType.DATE_TIME,
}
UNSUPPORTED_TYPES = {"spatial", "tuple", "table"}
TEMPORAL = {"date", "datetime"}


def _tag(e: ET.Element) -> str:
    return e.tag.rsplit("...", 1)[-1]


def _kids(e: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in e if _tag(c) == name]


def _kid(e: ET.Element, name: str) -> ET.Element | None:
    k = _kids(e, name)
    return k[0] if k else None


def _strip(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _ident(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_") or "x"


USER_NS = "http://www.tableausoftware.com/xml/user"
ET.register_namespace("user", USER_NS)

_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# Reserved words a bare identifier cannot stand as. A metric named one of these is
# inlined instead (same rule the Cube converter uses for the model-level namespace).
_SQL_KEYWORDS = frozenset("""
    ALL AND ANY AS ASC BETWEEN BY CASE CAST COLLATE CROSS CURRENT DESC DISTINCT
    ELSE END ESCAPE EXCEPT EXISTS FALSE FILTER FOLLOWING FROM FULL GROUP HAVING
    ILIKE IN INNER INTERSECT INTERVAL IS JOIN LEFT LIKE LIMIT NATURAL NOT NULL
    ON OR ORDER OUTER OVER PARTITION PRECEDING RANGE RIGHT ROW ROWS SELECT SOME
    THEN TRUE UNBOUNDED UNION USING WHEN WHERE WINDOW WITH WITHIN
""".split())


def _is_referenceable_name(name: str) -> bool:
    """True when a metric name can stand as a bare identifier in an expression."""
    return bool(_IDENT_RE.match(name)) and name.upper() not in _SQL_KEYWORDS


def _local_tag(e: ET.Element) -> str:
    return _tag(e).rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _user_kid(e: ET.Element | None, name: str) -> ET.Element | None:
    if e is None:
        return None
    want = {f"{{{USER_NS}}}{name}", f"user:{name}", name}
    return next((c for c in e if c.tag in want), None)


def _read_ai_context(el: ET.Element | None):
    kid = _user_kid(el, "ossie-ai-context")
    if kid is None or not (kid.text or "").strip():
        return None
    try:
        data = json.loads(kid.text)
    except ValueError:
        return None
    if isinstance(data, str):
        return data or None
    if isinstance(data, dict) and data:
        return OssieAIContextObject.model_validate(data)
    return None


def _with_caption_synonym(ctx, name: str, *candidates: str | None):
    """Add display names (caption, remote column, table) that differ from `name` and are not already present."""
    seen: set[str] = set()
    if isinstance(ctx, OssieAIContextObject) and ctx.synonyms:
        seen.update(ctx.synonyms)
    extra: list[str] = []
    for c in candidates:
        if c and c != name and c not in seen:
            extra.append(c)
            seen.add(c)
    if not extra:
        return ctx
    if ctx is None:
        return OssieAIContextObject(synonyms=tuple(extra))
    if isinstance(ctx, str):
        return OssieAIContextObject(instructions=ctx, synonyms=tuple(extra))
    return ctx.model_copy(update={"synonyms": tuple(ctx.synonyms or ()) + tuple(extra)})


def _source_table(src: str) -> str | None:
    """Last segment of a warehouse table path; None for custom SQL."""
    if not src or re.match(r"(?is)^\(?\s*(select|with)\b", src.strip()):
        return None
    parts = [p.strip('"`[]') for p in src.split(".") if p.strip('"`[]')]
    return parts[-1] if parts else None


def _combine_exts(*parts) -> list[OssieCustomExtension] | None:
    out: list[OssieCustomExtension] = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, list):
            out.extend(p)
        else:
            out.append(p)
    return out or None


CONSUMED_DS_TAGS = frozenset({
    "connection", "column", "aliases", "folders-common", "object-graph",
    "group", "drill-paths", "filter", "default-sorts", "field-sort-info",
    "column-instance", "semantic-values", "extract", "layout",
    "document-format-change-manifest", "ossie-ai-context", "ossie-foreign-extensions",
})


def _unique(base: str, used: set[str]) -> str:
    n, i = base, 2
    while n.lower() in used:
        n, i = f"{base}_{i}", i + 1
    used.add(n.lower())
    return n


def _sql_col(name: str) -> str:
    return name if not needs_quote(name) else '"' + name.replace('"', '""') + '"'


@dataclass
class Loss:
    kind: str
    name: str = ""
    detail: str = ""
    # filled from the catalogue (losses.py) when left empty
    category: str = ""
    severity: str = ""
    remedy: str = ""
    stashed: bool = False

    def __post_init__(self) -> None:
        sp = loss_spec(self.kind)
        self.category = self.category or sp.category
        self.severity = self.severity or sp.severity
        self.remedy = self.remedy or sp.remedy


@dataclass
class Result:
    document: OssieDocument | None
    losses: list[Loss] = field(default_factory=list)
    source_file: str = ""
    datasource: str = ""
    stats: dict = field(default_factory=dict)


@dataclass
class _Table:
    caption: str
    name: str
    source: str
    object_id: str = ""
    columns: list[tuple[str, str]] = field(default_factory=list)  # (remote name, tableau type)
    field_names: dict[str, str] = field(default_factory=dict)  # remote name -> ossie field name
    connection: str = ""  # federated named-connection id
    unique: bool = False  # an object-graph endpoint declared unique-key
    used_fields: set[str] = field(default_factory=set)
    ai_context: OssieAIContextObject | str | None = None


MAX_XML_BYTES = 256 * 1024 * 1024


def _parse_xml(data: bytes, name: str) -> ET.Element:
    if len(data) > MAX_XML_BYTES:
        raise ValueError(f"{name}: XML larger than {MAX_XML_BYTES} bytes")
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data[:65536].lower():
        raise ValueError(f"{name}: DOCTYPE/ENTITY declarations are not allowed")
    return ET.fromstring(data)


def _read_all_xml(path: Path) -> list[tuple[str, ET.Element]]:
    if path.suffix.lower() in (".twbx", ".tdsx"):
        with zipfile.ZipFile(path) as z:
            inner = [i for i in z.infolist() if i.filename.lower().endswith((".twb", ".tds"))]
            if not inner:
                raise ValueError(f"no .twb/.tds inside {path.name}")
            out = []
            for member in inner:
                if member.file_size > MAX_XML_BYTES:
                    raise ValueError(f"{path.name}: {member.filename} expands to {member.file_size} bytes")
                with z.open(member) as fh:
                    data = fh.read(MAX_XML_BYTES + 1)
                out.append((member.filename, _parse_xml(data, f"{path.name}:{member.filename}")))
            return out
    return [("", _parse_xml(path.read_bytes(), path.name))]


def _read_xml(path: Path) -> ET.Element:
    return _read_all_xml(path)[0][1]


def table_calc_meta(root: ET.Element) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse `<table-calc>` only. Do not invent Compute Using from Columns/Rows.

    Returns (order_keys, ordering_types). `ordering-field` becomes an ORDER BY local.
    `ordering-type` is recorded as Columns/Rows/Field. The 21 TABLEAU_PUBLIC TWBs
    have no worksheets and no `<table-calc>`; INCLUDE/EXCLUDE stay `sql_unreconciled`.
    """
    keys: dict[str, list[str]] = {}
    types: dict[str, str] = {}
    for tc in root.iter():
        if _tag(tc) != "table-calc":
            continue
        field = tc.get("field") or ""
        fp = re.findall(r"\[([^\]]+)\]", field)
        name = fp[-1] if fp else (field or "*")
        otype = (tc.get("ordering-type") or "").strip()
        if otype:
            types[name] = otype
        ordering = tc.get("ordering-field") or ""
        parts = re.findall(r"\[([^\]]+)\]", ordering)
        if parts:
            keys[name] = [parts[-1]]
    return keys, types


def table_calc_addressing(root: ET.Element) -> dict[str, list[str]]:
    """ORDER BY locals from `<table-calc ordering-field>` (do not invent)."""
    keys, _ = table_calc_meta(root)
    return keys


def _union_source(rel: ET.Element, losses: list[Loss], conns: dict[str, dict] | None = None) -> str:
    members = [m for m in rel if _tag(m) == "relation"]
    sources = [_qualify(_source_of(m), m, conns or {}, losses, m.get("name", "")) for m in members]
    colsets = []
    for m in members:
        ce = next((e for e in m if _tag(e) == "columns"), None)
        colsets.append(sorted(c.get("name", "") for c in (ce if ce is not None else [])))
    same = bool(members) and all(c == colsets[0] and c for c in colsets)
    name = rel.get("name", "")
    if same:
        op = " UNION ALL " if rel.get("all") == "true" else " UNION "
        losses.append(Loss("union", name, f"{len(members)} member(s) combined into a UNION source; Tableau matches columns by name"))
        return op.join(f"SELECT * FROM {s}" for s in sources)
    if members and all(colsets):
        allcols = sorted({c for cs in colsets for c in cs})
        op = " UNION ALL " if rel.get("all") == "true" else " UNION "
        q = lambda c: '"' + c.replace('"', '""') + '"'  # noqa: E731
        sel = [f"SELECT {', '.join(q(c) if c in cs else 'NULL AS ' + q(c) for c in allcols)} FROM {src}" for cs, src in zip(colsets, sources)]
        losses.append(Loss("union", name, f"{len(members)} member(s) with differing columns ({', '.join(sources)}) combined into an explicit "
                           f"NULL-padded {op.strip()} over {len(allcols)} column(s); Tableau's 'Table Name' and 'Sheet' columns are not produced"))
        return op.join(sel)
    losses.append(Loss("union", name, f"{len(members)} member(s) with differing columns ({', '.join(sources)}); only the first member is used as the source"))
    return sources[0] if sources else name


NON_WAREHOUSE_CLASSES = frozenset({"federated", "sqlproxy", "hyper", "dataengine", "genericodbc-dsn"})
FILE_CLASSES = frozenset({"excel-direct", "textscan", "csv", "google-sheets"})


def _source_of(rel: ET.Element) -> str:
    if rel.get("type") == "text":
        return (rel.text or "").strip() or rel.get("name", "")
    t = rel.get("table") or rel.get("name") or ""
    parts = REF_RE.findall(t)
    return ".".join(parts) if parts else t


CONN_KEYS = ("class", "server", "dbname", "schema", "warehouse", "service", "username", "project", "CATALOG", "EXECCATALOG", "connection-dialect")
# `authentication` is kept only for BigQuery, where the TDS writer reuses it (service-account vs plain OAuth)
_BQ_ONLY_KEYS = ("authentication",)


def _pick_conn(c: ET.Element) -> dict:
    keys = CONN_KEYS + (_BQ_ONLY_KEYS if c.get("class") == "bigquery" else ())
    return {k: c.get(k) for k in keys if c.get(k)}


def _conn_attrs(ds: ET.Element) -> dict[str, dict]:
    """Physical connection attributes by named-connection id; key "" is the datasource's own connection."""
    out: dict[str, dict] = {}
    for nc in ds.iter():
        if _tag(nc) == "named-connection":
            inner = _kid(nc, "connection")
            if inner is not None:
                out[nc.get("name", "")] = _pick_conn(inner)
    top = _kid(ds, "connection")
    if top is not None and top.get("class") != "federated":
        out[""] = _pick_conn(top)
    return out


def _qualify(src: str, rel: ET.Element, conns: dict[str, dict], losses: list["Loss"], name: str) -> str:
    """db.schema.table when the connection names a database (Snowflake dbname, BigQuery CATALOG) and the source is a plain two-part table."""
    if rel.get("type") != "table":
        return src
    c = conns.get(rel.get("connection", "")) or conns.get("") or {}
    db = c.get("dbname") or c.get("CATALOG")
    if db and src.count(".") == 1 and not src.startswith(db + "."):
        losses.append(Loss("source_qualified", name, f"source qualified with the connection database {db!r}"))
        return f"{db}.{src}"
    return src


def _leaf_relations(rel: ET.Element) -> list[ET.Element]:
    kind = rel.get("type")
    if kind in ("table", "text", "union"):
        return [rel]
    out: list[ET.Element] = []
    for c in rel:
        if _tag(c) == "relation":
            out.extend(_leaf_relations(c))
    return out


def _join_clauses(rel: ET.Element) -> list[tuple[ET.Element, str]]:
    out: list[tuple[ET.Element, str]] = []
    for c in rel:
        if _tag(c) == "clause" and c.get("type") == "join":
            out.append((c, rel.get("join", "")))
        elif _tag(c) == "relation":
            out.extend(_join_clauses(c))
    return out


def _mapped_columns(ds: ET.Element, conn: ET.Element | None, relation: str) -> list[tuple[str, str]]:
    """Columns that `<cols><map key="[local]" value="[relation].[remote]"/>` attributes to `relation`.

    Used when a relation carries no metadata-records. The datatype comes from the
    datasource-level `<column>` for the local name, defaulting to string.
    """
    cols_el = _kid(conn, "cols") if conn is not None else None
    if cols_el is None:
        return []
    types = {_strip(c.get("name", "")): c.get("datatype", "string") for c in _kids(ds, "column")}
    out: list[tuple[str, str]] = []
    for m in _kids(cols_el, "map"):
        rel_name, _, remote = (m.get("value") or "").partition("].[")
        if rel_name.lstrip("[") != relation or not remote:
            continue
        out.append((remote.rstrip("]"), types.get(_strip(m.get("key", "")), "string")))
    return out


def _tables(ds: ET.Element, losses: list[Loss], joins: list[tuple[ET.Element, str]]) -> tuple[list[_Table], ET.Element | None]:
    conn = _kid(ds, "connection")
    og = _kid(ds, "object-graph")
    conns = _conn_attrs(ds)
    tables: list[_Table] = []
    if og is not None:
        objs = _kid(og, "objects")
        for obj in (_kids(objs, "object") if objs is not None else []):
            rel = next((e for e in obj.iter() if _tag(e) == "relation" and e.get("type") == "union"), None) or next(
                (e for e in obj.iter() if _tag(e) == "relation" and e.get("type") in ("table", "text")), None)
            if rel is None:
                losses.append(Loss("unsupported_object", obj.get("caption", ""), "object without table relation"))
                continue
            cols = []
            ce = next((e for e in rel if _tag(e) == "columns"), None)
            for c in (ce if ce is not None else []):
                cols.append((c.get("name", ""), c.get("datatype", "string")))
            if not cols and conn is not None:
                for m in conn.iter():
                    if _tag(m) == "metadata-record" and m.get("class") == "column":
                        oid = m.findtext("{*}object-id") or next((x.text for x in m if _tag(x) == "object-id"), None)
                        if oid and _strip(oid) == obj.get("id"):
                            cols.append((m.findtext("remote-name") or "", m.findtext("local-type") or "string"))
            src = _union_source(rel, losses, conns) if rel.get("type") == "union" else _qualify(_source_of(rel), rel, conns, losses, rel.get("name", ""))
            tables.append(_Table(obj.get("caption") or rel.get("name", ""), rel.get("name", ""), src, obj.get("id", ""), cols, connection=rel.get("connection", ""),
                                ai_context=_read_ai_context(obj)))
        return tables, og
    if conn is None:
        return tables, None
    rels = _kids(conn, "relation")
    legacy_true = [r for r in rels if "true" in r.tag]
    chosen = (legacy_true or rels)
    leaves: list[ET.Element] = []
    for r in chosen:
        leaves.extend(_leaf_relations(r))
    for r in chosen:
        joins.extend(_join_clauses(r))
    mrecs = [m for m in conn.iter() if _tag(m) == "metadata-record" and m.get("class") == "column"]
    for rel in leaves:
        parent = f"[{rel.get('name', '')}]"
        cols = []
        for m in mrecs:
            pn = m.findtext("parent-name")
            if pn == parent:
                cols.append((m.findtext("remote-name") or _strip(m.findtext("local-name") or ""), m.findtext("local-type") or "string"))
        if not cols:
            ce = next((e for e in rel if _tag(e) == "columns"), None)
            for c in (ce if ce is not None else []):
                cols.append((c.get("name", ""), c.get("datatype", "string")))
        if not cols:
            cols = _mapped_columns(ds, conn, rel.get("name", ""))
        src = _union_source(rel, losses, conns) if rel.get("type") == "union" else _qualify(_source_of(rel), rel, conns, losses, rel.get("name", ""))
        tables.append(_Table(rel.get("name", ""), rel.get("name", ""), src, "", cols, connection=rel.get("connection", "")))
    return tables, None


def _calc_aggregated(masked_formula: str) -> bool:
    return bool(AGG_RE.search(masked_formula))


def _attr(e: ET.Element, name: str) -> str | None:
    for k, v in e.attrib.items():
        if k.rsplit("}", 1)[-1].rsplit(":", 1)[-1].rsplit("...", 1)[-1] == name:
            return v
    return None


def _description(col: ET.Element) -> str | None:
    d = _kid(col, "desc")
    if d is None:
        return None
    text = "".join(r.text or "" for r in d.iter() if _tag(r) == "run").strip()
    return text or None


def _field_extension(col: ET.Element | None, folder: str | None, extra: dict | None = None) -> OssieCustomExtension | None:
    d: dict = dict(extra or {})
    if col is not None:
        if _attr(col, "hidden") == "true":
            d["hidden"] = True
        for key, attr in (("semantic_role", "semantic-role"), ("default_aggregation", "aggregation"), ("default_format", "default-format")):
            v = _attr(col, attr)
            if v:
                d[key] = v
    if folder:
        d["folder"] = folder
    if not d:
        return None
    return OssieCustomExtension(vendor_name="TABLEAU", data=json.dumps(d, sort_keys=True))


def _ext(vendor_data: dict) -> list[OssieCustomExtension]:
    return [OssieCustomExtension(vendor_name="TABLEAU", data=json.dumps(vendor_data, sort_keys=True))]


def _is_numrec(col: ET.Element) -> bool:
    return _attr(col, "auto-column") == "numrec"


PARAM_REF_RE = re.compile(r"\[Parameters\]\.\[([^\]]+)\]")
# raw tags the writer puts back into the TDS; the rest are kept so nothing is lost but are not re-emitted
RESTORED_TAGS = ("filter", "drill-paths", "default-sorts", "field-sort-info", "one-time-sql", "connection-customization", "parameter", "aliases", "group", "column-instance", "semantic-values", "calc")


def _xml(el: ET.Element) -> str:
    return ET.tostring(el, encoding="unicode")


def _paren_events(formula: str):
    """Yield (kind, start, end, value) for `ref`, `open` (value = preceding function name) and `close`, skipping strings and comments."""
    for t in tokenize(formula):
        if t.kind == "ref":
            yield "ref", t.start, t.end, t.value
        elif t.kind == "code":
            for m in re.finditer(r"(\b[A-Za-z_]+)?\s*\(|\)", formula[t.start:t.end]):
                if m.group(0) == ")":
                    yield "close", t.start + m.start(), t.start + m.end(), ""
                else:
                    yield "open", t.start + m.start(), t.start + m.end(), m.group(1) or ""


def _inside_aggregate(formula: str, ref: str) -> bool:
    """True when any `[ref]` sits inside the parentheses of an aggregate call."""
    stack: list[bool] = []
    for kind, _s, _e, val in _paren_events(formula):
        if kind == "close":
            if stack:
                stack.pop()
        elif kind == "open":
            stack.append(bool(val) and AGG_RE.match(val + "(") is not None)
        elif val == ref and any(stack):
            return True
    return False


def _helper_aggregate(out: str, tables: list):
    """Split `AGG(<row-level expression over one table>)` into (AGG, expression, table); None for any other shape."""
    m = re.match(r"\s*(SUM|AVG|MIN|MAX|MEDIAN|COUNTD|COUNT)\s*\(", out, re.I)
    if not m:
        return None
    depth, close_at, end = 0, -1, -1
    for kind, s, e, _v in _paren_events(out):
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
            if depth == 0:
                close_at, end = s, e
                break
    if close_at < 0 or out[end:].strip():
        return None
    inner = out[m.end():close_at]
    if AGG_RE.search(inner):
        return None
    names = set(re.findall(r"\[([^\[\]]+)\]\.\[", inner))
    if not names and tables and re.fullmatch(r"\s*[-+]?\s*\(?\s*[-+]?\d+(?:\.\d+)?\s*\)?\s*", inner):
        return m.group(1).upper(), inner, tables[0]
    hit = [t for t in tables if t.name in names]
    if len(names) != 1 or len(hit) != 1:
        return None
    return m.group(1).upper(), inner, hit[0]


_NUM_TYPES = {"INTEGER", "DECIMAL", "FLOAT", "NUMBER", "NUMERIC", "DOUBLE", "REAL"}


def _make_sql_resolver(ds_fields: dict, wh: Warehouse = DEFAULT, addressing: dict[str, list[str]] | None = None,
                       metric_names: set[str] | None = None):
    """Return `sql_dialect(tab, home, label, losses)` over an index of ds_fields that is built once and extended incrementally as fields are appended."""
    from .calc_parser import Untranslatable, translate
    preference = wh.dialect_preference
    import sqlglot
    from sqlglot import exp
    sql_of: dict[tuple[str, str], tuple[str, str | None]] = {}
    type_of: dict[tuple[str, str], str] = {}
    by_name: dict[str, set[tuple[str, str]]] = {}
    seen: dict[str, int] = {}

    def _type(f) -> str:
        v = getattr(f.datatype, "value", f.datatype)
        v = str(v or "").upper()
        return "str" if v in ("STRING", "TEXT", "VARCHAR") else "num" if v in _NUM_TYPES else "any"

    def refresh():
        for tn, fs in ds_fields.items():
            for f in fs[seen.get(tn, 0):]:
                key = (tn, f.name)
                type_of[key] = _type(f)
                by_name.setdefault(f.name, set()).add(key)
                for pref in preference:
                    d = next((x for x in f.expression.dialects if x.dialect.value == pref), None)
                    if d:
                        sql_of[key] = (d.expression, None if pref == "ANSI_SQL" else wh.sqlglot_dialect)
                        break
            seen[tn] = len(fs)

    def _home_name(home: str | None) -> str | None:
        if home:
            return home
        return next(iter(ds_fields), None)

    def _resolved(ds, name, home):
        if ds is not None:
            hits = by_name.get(name, set())
            key = (ds, name) if (ds, name) in type_of else (next(iter(hits)) if len(hits) == 1 else None)
            hit = sql_of.get(key) if key else None
            typ = type_of.get(key, "any") if key else "any"
            table = key[0] if key else ds
            if hit is not None:
                node = sqlglot.parse_one(hit[0], read=hit[1])
                while isinstance(node, exp.Paren):
                    node = node.this
            else:
                node = exp.column(name, table=ds)
            # home == "" means "no home table": the metric spans tables, so every column keeps its qualifier.
            home_eff = _home_name(home)
            if isinstance(node, exp.Column) and table and (home == "" or (home_eff and table.lower() != home_eff.lower())):
                node = exp.column(node.name, table=table)
            return exp.Paren(this=node), typ
        key = (home or "", name)
        hit = sql_of.get(key)
        return None if hit is None else (exp.Paren(this=sqlglot.parse_one(hit[0], read=hit[1])), type_of.get(key, "any"))

    def _order_exprs(resolve, home, extra_names: list[str] | None = None):
        h = _home_name(home)
        names = list(extra_names or [])
        if not names and h and ds_fields.get(h):
            names = [ds_fields[h][0].name]
        out = []
        for fname in names:
            hit = resolve(None, fname)
            if hit is None and h:
                hit = resolve(h, fname)
            if not hit:
                continue
            node = hit[0]
            while isinstance(node, exp.Paren):
                node = node.this
            out.append(node)
        return out

    def sql_dialect(tab: str, home: str | None, label: str, losses: list,
                    order_names: list[str] | None = None) -> "OssieDialectExpression | None":
        """Target-warehouse rendering of a TABLEAU-dialect expression, or None when it cannot be translated exactly enough."""
        refresh()

        def resolve(ds, name):
            return _resolved(ds, name, home)
        try:
            t = translate(tab, resolve, wh, order=_order_exprs(resolve, home, order_names),
                          metric_names=metric_names)
            sql = t.sql
        except (Untranslatable, sqlglot.errors.SqlglotError):
            return None
        if t.notes:
            losses.append(Loss("expression_approximated", label, "; ".join(t.notes)[:300]))
        return OssieDialectExpression(dialect=OssieDialect[wh.ossie_dialect], expression=sql)

    def sql_dialects(tab: str, home: str | None, label: str, losses: list, quiet: bool = False,
                     order_names: list[str] | None = None) -> list:
        """SNOWFLAKE + ANSI_SQL + BIGQUERY."""
        refresh()

        def resolve(ds, name):
            return _resolved(ds, name, home)

        from .warehouse import BIGQUERY as BQ_WH, SNOWFLAKE as SF_WH
        order = _order_exprs(resolve, home, order_names)
        out: list = []
        try:
            t = translate(tab, resolve, SF_WH, order=order, metric_names=metric_names)
        except (Untranslatable, sqlglot.errors.SqlglotError):
            return []
        if t.notes and not quiet:
            losses.append(Loss("expression_approximated", label, "; ".join(t.notes)[:300]))
        out.append(OssieDialectExpression(dialect=OssieDialect.SNOWFLAKE, expression=t.tree.sql("snowflake")))
        out.append(OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=t.tree.sql()))
        try:
            tb = translate(tab, resolve, BQ_WH, order=order, metric_names=metric_names)
            bq_sql = tb.sql
            if "FIRST_VALUE" in bq_sql.upper():
                bq_sql = bq_sql.replace(" NULLS LAST", "")
            out.append(OssieDialectExpression(dialect=OssieDialect.BIGQUERY, expression=bq_sql))
        except (Untranslatable, sqlglot.errors.SqlglotError, AttributeError):
            pass
        return out

    return sql_dialect, sql_dialects


def _warehouse_of(ds: ET.Element) -> Warehouse:
    for c in _conn_attrs(ds).values():
        if (w := by_tableau_class(c.get("class", ""))) is not None:
            return w
    return DEFAULT


def convert_datasource(ds: ET.Element, source_file: str = "", params: list[ET.Element] | None = None, inline_parameters: bool = False,
                       warehouse: Warehouse | None = None, addressing: dict[str, list[str]] | None = None,
                       ordering_types: dict[str, str] | None = None) -> Result:
    wh = warehouse or _warehouse_of(ds)
    losses: list[Loss] = []
    ds_name = ds.get("caption") or ds.get("name") or ds.get("formatted-name") or "datasource"
    joins: list[tuple[ET.Element, str]] = []
    tables, og = _tables(ds, losses, joins)
    res = Result(None, losses, source_file, ds_name)
    classes = {e.get("class") for e in ds.iter() if _tag(e) == "connection" and e.get("class")}
    if not tables:
        if "sqlproxy" in classes:
            losses.append(Loss("published_datasource", ds_name, "reference to a published datasource; its tables are defined on the server"))
        else:
            losses.append(Loss("no_tables", ds_name, "no table relations found"))
        return res

    unknown_cls = sorted(classes - FILE_CLASSES - NON_WAREHOUSE_CLASSES - {w.tableau_class for w in WAREHOUSES.values()})
    if unknown_cls and warehouse is None:
        wh = GENERIC
        losses.append(Loss("unknown_warehouse", ds_name, f"connection class(es) {', '.join(unknown_cls)} are not registered warehouses; translated as generic ANSI SQL"))
    target_dialect = OssieDialect[wh.ossie_dialect]
    for rel in ds.iter():
        if _tag(rel) != "relation":
            continue
        rname = rel.get("name") or rel.get("table") or ""
        if "*" in (rel.get("table") or "") or "*" in rname:
            losses.append(Loss("wildcard_table", rname, "wildcard (sharded) table; kept as written in the dataset source, which most consumers cannot resolve"))
        if rel.get("type") == "text" and re.search(r"(?<![\w@])@[A-Za-z_]\w*", rel.text or ""):
            losses.append(Loss("query_parameter", rname, "custom SQL uses @named query parameters; the SQL is kept verbatim and the parameters are not bound"))

    file_cls = sorted(classes & FILE_CLASSES)
    if file_cls:
        losses.append(Loss("file_source", ds_name, f"file-based source ({', '.join(file_cls)}); the sheet or file is not a warehouse table, so a Snowflake semantic view cannot be created over it"))

    named: dict[str, tuple[str, str]] = {}  # named-connection id -> (caption, class)
    for nc in ds.iter():
        if _tag(nc) == "named-connection":
            inner = _kid(nc, "connection")
            named[nc.get("name", "")] = (nc.get("caption") or nc.get("name", ""), inner.get("class", "") if inner is not None else "")

    used_ds: set[str] = set()
    for t in tables:
        base = _ident(t.caption or t.name)
        if base.lower() in used_ds and t.connection in named:
            base = f"{base}_{_ident(named[t.connection][0])}"
        t.name = _unique(base, used_ds)

    colinfo: dict[str, ET.Element] = {}
    for c in _kids(ds, "column"):
        colinfo[_strip(c.get("name", ""))] = c

    folders: dict[str, str] = {}
    for fc in _kids(ds, "folders-common"):
        for fo in _kids(fc, "folder"):
            for item in _kids(fo, "folder-item"):
                folders.setdefault(_strip(item.get("name", "")), fo.get("name", ""))

    empty = [t for t in tables if not t.columns]
    if empty:
        declared = [
            (_strip(c.get("name", "")), c.get("datatype", "string"))
            for c in _kids(ds, "column")
            if _kid(c, "calculation") is None and c.get("name", "").startswith("[")
            and not _strip(c.get("name", "")).startswith(":")
            and not c.get("{http://www.tableausoftware.com/xml/user}ExtractForeignKey")
            and not (c.get("auto-hidden") == "true" and _strip(c.get("name", "")).startswith(("fk:", "pk:")))
        ]
        if len(tables) == 1 and declared:
            tables[0].columns = declared
        else:
            for t in empty:
                losses.append(Loss("no_column_metadata", t.caption, "table has no column metadata; columns cannot be attributed"))

    # resolution of Tableau local names -> (table, remote column)
    resolve: dict[str, tuple[_Table, str]] = {}
    for t in tables:
        for remote, _ in t.columns:
            resolve.setdefault(remote, (t, remote))
            for label in {t.caption, t.caption.upper(), t.caption.lower(), t.name, t.name.upper()}:
                resolve[f"{remote} ({label})"] = (t, remote)
    meta_local: dict[tuple[int, str], str] = {}
    conn = _kid(ds, "connection")
    if conn is not None:
        for m in conn.iter():
            if _tag(m) == "metadata-record" and m.get("class") == "column":
                local = _strip(m.findtext("local-name") or "")
                remote = m.findtext("remote-name") or local
                parent = _strip(m.findtext("parent-name") or "")
                for t in tables:
                    if parent.lower() in (t.caption.lower(), t.name.lower()):
                        resolve[local] = (t, remote)
                        meta_local[(id(t), remote)] = local

    for c in _kids(ds, "column"):
        local = _strip(c.get("name", ""))
        cap = c.get("caption") or ""
        if local in resolve:
            if cap:
                resolve.setdefault(cap, resolve[local])
            continue
        if cap and cap in resolve:
            resolve.setdefault(local, resolve[cap])

    ds_fields: dict[str, list[OssieField]] = {}
    addr = addressing or {}
    otypes = ordering_types or {}
    metric_name_set: set[str] = set()
    _sql_dialect, _sql_dialects = _make_sql_resolver(ds_fields, wh, addr, metric_name_set)
    foreign_by_path: dict[str, list[OssieCustomExtension]] = {}
    fe_el = _user_kid(ds, "ossie-foreign-extensions")
    if fe_el is not None and (fe_el.text or "").strip():
        try:
            packed = json.loads(fe_el.text)
        except ValueError:
            packed = []
        for entry in packed if isinstance(packed, list) else []:
            if not isinstance(entry, dict) or not entry.get("path") or not entry.get("vendor_name") or "data" not in entry:
                continue
            foreign_by_path.setdefault(entry["path"], []).append(
                OssieCustomExtension(vendor_name=entry["vendor_name"], data=entry["data"]))

    def take_foreign(path: str) -> list[OssieCustomExtension] | None:
        return foreign_by_path.pop(path, None)
    ds_unique: dict[str, list[list[str]]] = {}
    ds_ext: dict[str, list[OssieCustomExtension] | None] = {}
    field_count = 0
    for t in tables:
        fields: list[OssieField] = []
        for remote, ttype in t.columns:
            fname = _unique(_ident(remote), t.used_fields)
            t.field_names[remote] = fname
            info = None
            for key in (remote, f"{remote} ({t.caption})"):
                if key in colinfo:
                    info = colinfo[key]
                    break
            if info is None:
                for local, (tt, rr) in resolve.items():
                    if tt is t and rr == remote and local in colinfo:
                        info = colinfo[local]
                        break
            role = info.get("role") if info is not None else None
            is_dim = (role == "dimension") if role in ("dimension", "measure") else ttype in ({"string", "boolean"} | TEMPORAL | UNSUPPORTED_TYPES)
            dimension = OssieDimension(is_time=True) if is_dim and ttype in TEMPORAL else (OssieDimension() if is_dim else None)
            label = info.get("caption") if info is not None else None
            final_label = label or (remote if remote != fname else None)
            extra: dict = {}
            default_caption = final_label or (fname if fname != remote else None)
            if info is not None and (_strip(info.get("name", "")) != remote or info.get("caption") != default_caption):
                extra["names"] = stash.pack("field_names", {"name": fname, "label": final_label},
                                            {"local": _strip(info.get("name", "")), "caption": info.get("caption")})
            elif info is None and meta_local.get((id(t), remote), remote) != remote:
                extra["names"] = stash.pack("field_names", {"name": fname, "label": final_label},
                                            {"local": meta_local[(id(t), remote)], "caption": None})
            if ttype in UNSUPPORTED_TYPES:
                losses.append(Loss("unsupported_datatype", f"{t.name}.{fname}", f"Tableau datatype {ttype!r} kept as Opaque; original type stashed", stashed=True))
                extra["datatype"] = stash.pack("datatype", OssieDataType.OPAQUE.value, ttype)
            ext = _field_extension(info, folders.get(_strip(info.get("name", ""))) if info is not None else None, extra)
            caption = info.get("caption") if info is not None else None
            fields.append(OssieField(
                name=fname,
                expression=OssieExpression(dialects=[OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=_sql_col(remote))]),
                dimension=dimension,
                label=final_label,
                description=_description(info) if info is not None else None,
                datatype=TYPE_MAP.get(ttype, OssieDataType.OPAQUE if ttype in UNSUPPORTED_TYPES else None),
                ai_context=_with_caption_synonym(_read_ai_context(info), fname, caption, remote),
                custom_extensions=_combine_exts(ext, take_foreign(f"field:{t.name}.{fname}")),
            ))
        field_count += len(fields)
        conn_ext = None
        if t.connection in named:
            cap, cls = named[t.connection]
            conn_ext = _ext({"connection": cap, "connection_class": cls})
        ds_fields[t.name] = fields
        ds_ext[t.name] = _combine_exts(conn_ext, take_foreign(f"dataset:{t.name}"))

    by_obj = {t.object_id: t for t in tables if t.object_id}
    relationships: list[OssieRelationship] = []
    used_r: set[str] = set()
    unhinted = 0

    def pairs(expr: ET.Element) -> list[tuple[str, str]]:
        op = expr.get("op", "")
        subs = [c for c in expr if _tag(c) == "expression"]
        if op == "=" and len(subs) == 2:
            return [(subs[0].get("op", ""), subs[1].get("op", ""))]
        out: list[tuple[str, str]] = []
        if op.upper() == "AND":
            for s in subs:
                out.extend(pairs(s))
            return out
        raise ValueError(f"unsupported join expression op {op!r}")

    def mark_unique(table: _Table, cols: list[str]) -> None:
        keys = ds_unique.setdefault(table.name, [])
        if cols not in keys:
            keys.append(cols)

    if og is not None:
        rels = _kid(og, "relationships")
        for r in (_kids(rels, "relationship") if rels is not None else []):
            try:
                e1, e2 = _kid(r, "first-end-point"), _kid(r, "second-end-point")
                t1, t2 = by_obj[e1.get("object-id")], by_obj[e2.get("object-id")]
                expr = _kid(r, "expression")
                fc, tc = [], []
                for a, b in pairs(expr):
                    a, b = _strip(a), _strip(b)

                    def col(name: str, t: _Table) -> str | None:
                        if name in t.field_names:
                            return t.field_names[name]
                        m = re.fullmatch(r"(.*) \(.*\)", name)
                        if m and m.group(1) in t.field_names:
                            return t.field_names[m.group(1)]
                        return None
                    ca, cb = col(a, t1), col(b, t2)
                    if ca is None or cb is None:
                        ca, cb = col(b, t1), col(a, t2)
                    if ca is None or cb is None:
                        raise ValueError(f"cannot resolve join columns {a!r}/{b!r}")
                    fc.append(ca)
                    tc.append(cb)
                u1, u2 = _attr(e1, "unique-key") == "true", _attr(e2, "unique-key") == "true"
                if u1 and not u2:
                    t1, t2, fc, tc = t2, t1, tc, fc  # the many side is `from`
                    u1, u2 = u2, u1
                if u1:
                    mark_unique(t1, fc)
                if u2:
                    mark_unique(t2, tc)
                if not (u1 or u2):
                    unhinted += 1
                rext = {"guaranteed_value": True} if _attr(e1, "guaranteed-value") == "true" or _attr(e2, "guaranteed-value") == "true" else None
                rname = _unique(f"{t1.name}_{t2.name}", used_r)
                relationships.append(OssieRelationship(
                    name=rname, from_dataset=t1.name, to=t2.name, from_columns=fc, to_columns=tc,
                    custom_extensions=_combine_exts(_ext(rext) if rext else None, take_foreign(f"relationship:{rname}"))))
            except (KeyError, ValueError, AttributeError) as ex:
                losses.append(Loss("relationship", "", str(ex)))
        if unhinted:
            losses.append(Loss("relationship_cardinality", "", "Tableau relationships carry no cardinality; first endpoint was assumed to be the many side"))

    by_cap = {t.caption: t for t in tables}
    for clause, join_type in joins:
        try:
            expr = _kid(clause, "expression")
            grouped: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
            for a, b in pairs(expr):
                ra, rb = REF_RE.findall(a), REF_RE.findall(b)
                if len(ra) != 2 or len(rb) != 2:
                    raise ValueError(f"unsupported legacy join operands {a!r}/{b!r}")
                ta, tb = by_cap[ra[0]], by_cap[rb[0]]
                fc, tc = grouped.setdefault((ta.caption, tb.caption), ([], []))
                fc.append(ta.field_names[ra[1]])
                tc.append(tb.field_names[rb[1]])
            for (a_cap, b_cap), (fc, tc) in grouped.items():
                rname = _unique(f"{by_cap[a_cap].name}_{by_cap[b_cap].name}", used_r)
                relationships.append(OssieRelationship(
                    name=rname,
                    from_dataset=by_cap[a_cap].name, to=by_cap[b_cap].name, from_columns=fc, to_columns=tc,
                    custom_extensions=_combine_exts(_ext({"join_type": join_type}) if join_type else None, take_foreign(f"relationship:{rname}"))))
        except (KeyError, ValueError, AttributeError) as ex:
            losses.append(Loss("relationship", "", f"legacy join: {type(ex).__name__} {ex}"))
    if joins and relationships and og is None:
        losses.append(Loss("relationship_cardinality", "", "legacy join cardinality is not represented (join type is kept as an extension); first table assumed to be the many side"))

    # calculated fields
    calcs: dict[str, tuple[ET.Element, ET.Element]] = {}
    own_params = [c for c in colinfo.values() if c.get("param-domain-type")]
    for iname, c in colinfo.items():
        calc = _kid(c, "calculation")
        if calc is not None and not c.get("param-domain-type"):
            calcs[iname] = (c, calc)
    numrec = {k for k, (c, _) in calcs.items() if _is_numrec(c)}
    internal_of: dict[str, str] = {}
    for k, (c, _) in calcs.items():
        internal_of.setdefault(k, k)
    for k, (c, _) in calcs.items():
        cap = c.get("caption")
        if cap and cap not in resolve:
            internal_of.setdefault(cap, k)

    metrics: list[OssieMetric] = []
    unconverted: list[dict] = []
    stashed_calcs: dict[str, ET.Element] = {}
    used_m: set[str] = set()
    emitted: dict[str, tuple[_Table, str, str | None]] = {}  # calc internal name -> (table, field name, tableau datatype)
    row_fields = 0

    def row_cause(formula: str, m: str) -> str:
        if RLS_RE.search(m):
            return "rls"
        refs = formula_refs(formula)
        if "Parameters" in refs:
            return "param"
        tabs: set[str] = set()
        for ref in refs:
            if ref in resolve:
                tabs.add(resolve[ref][0].name)
            elif (k := internal_of.get(ref)) in emitted:
                tabs.add(emitted[k][0].name)
            elif k in calcs:
                return "depends_on_lost"
            else:
                return "unresolved"
        return "cross_dataset" if len(tabs) > 1 else "other"

    def lose(iname: str, c: ET.Element, formula: str, reason: str, cause: str | None = None) -> None:
        label = c.get("caption") or iname
        detail = formula[:200] if cause is None else f"[{cause}] {formula[:190]}"
        losses.append(Loss(reason, label, detail))
        entry = {"name": label, "internal_name": iname, "reason": reason, "formula": formula, "role": c.get("role"), "datatype": c.get("datatype")}
        if cause is not None:
            entry["cause"] = cause
        unconverted.append(entry)
        stashed_calcs.setdefault(iname, c)

    bin_params: set[str] = set()
    param_vals = {_strip(p.get("name", "")): p.get("value") for p in list(params or []) + own_params}

    if inline_parameters:
        for iname, (c, calc) in list(calcs.items()):
            f = calc.get("formula", "")
            names = PARAM_REF_RE.findall(f) if f else []
            if not names or any(param_vals.get(n) is None for n in names):
                continue
            new = PARAM_REF_RE.sub(lambda g: f"({param_vals[g.group(1)]})", f)
            cp = copy.copy(calc)
            cp.set("formula", new)
            calcs[iname] = (c, cp)
            losses.append(Loss("parameter_inlined", c.get("caption") or iname, f"current parameter value substituted; the calculation no longer follows parameter changes: {f[:150]}"))

    def add_field(iname: str, c: ET.Element, t: _Table, dialects: list[OssieDialectExpression], calc_xml: ET.Element | None = None) -> str:
        nonlocal field_count, row_fields
        field_count += 1
        row_fields += 1
        fname = _unique(_ident(c.get("caption") or iname), t.used_fields)
        dt = c.get("datatype")
        flabel = c.get("caption") if c.get("caption") and c.get("caption") != fname else None
        ds_fields[t.name].append(OssieField(
            name=fname,
            expression=OssieExpression(dialects=dialects),
            dimension=(OssieDimension(is_time=dt in TEMPORAL) if c.get("role") == "dimension" else None),
            label=flabel,
            description=_description(c),
            datatype=TYPE_MAP.get(dt or ""),
            ai_context=_with_caption_synonym(_read_ai_context(c), fname, c.get("caption")),
            custom_extensions=_combine_exts(_field_extension(c, folders.get(iname), {
                "names": stash.pack("field_names", {"name": fname, "label": flabel}, {"local": iname, "caption": c.get("caption")}),
                **({"tableau_calc": stash.pack("tableau_calc", dialects[0].expression, {"xml": ET.tostring(calc_xml, encoding="unicode")})} if calc_xml is not None else {})}),
                take_foreign(f"field:{t.name}.{fname}")),
        ))
        emitted[iname] = (t, fname, dt)
        return fname

    def bin_field(iname: str, c: ET.Element, calc: ET.Element) -> bool:
        """Numeric bin -> FLOOR(x / size) * size. Returns False when it cannot be expressed."""
        refs = formula_refs(calc.get("formula", ""))
        hit = resolve.get(refs[0]) if len(refs) == 1 else None
        size = calc.get("size")
        sp = calc.get("size-parameter")
        if sp:
            pn = PARAM_REF_RE.findall(sp)
            size = param_vals.get(pn[0]) if pn else None
        try:
            ok = hit is not None and size is not None and float(size) > 0 and calc.get("peg", "0") in ("0", "0.0", "")
        except ValueError:
            ok = False
        if not ok:
            return False
        t, remote = hit
        add_field(iname, c, t, [
            OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=f"FLOOR([{t.field_names[remote]}] / {size}) * {size}"),
            OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=f"FLOOR({_sql_col(remote)} / {size}) * {size}")], calc)
        if sp:
            bin_params.update(PARAM_REF_RE.findall(sp))
            detail = f"bin size taken from parameter {sp} (current value {size}); the field does not follow later parameter changes"
        else:
            detail = f"bin of size {size}"
        losses.append(Loss("bin", c.get("caption") or iname, detail))
        return True

    def categorical_field(iname: str, c: ET.Element, calc: ET.Element) -> bool:
        """Single-field categorical bin -> IF/CASE over the member lists. Returns False when it cannot be expressed."""
        col = _strip(calc.get("column", ""))
        hit = resolve.get(col)
        if hit is None or c.get("datatype") not in (None, "string"):
            return False
        t, remote = hit
        arms: list[tuple[str, list[str]]] = []
        for b in _kids(calc, "bin"):
            vals = [(v.text or "") for v in _kids(b, "value")]
            if not vals:
                continue
            arms.append((b.get("value", ""), vals))
        if not arms:
            return False

        def lit(v: str) -> str | None:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                return v[1:-1]
            return None
        parsed = []
        for label, vals in arms:
            lab = lit(label)
            lv = [lit(v) for v in vals]
            if lab is None or any(x is None for x in lv):
                return False
            parsed.append((lab, lv))
        tcol = f"[{t.field_names[remote]}]"
        tq = lambda x: '"' + x.replace('"', '\\"') + '"'  # noqa: E731
        sq = lambda x: "'" + x.replace("'", "''") + "'"  # noqa: E731
        tab = "IF " + " ELSEIF ".join(f"{tcol} IN ({', '.join(tq(v) for v in lv)}) THEN {tq(lab)}" for lab, lv in parsed) + f" ELSE {tcol} END"
        sql = "CASE " + " ".join(f"WHEN {_sql_col(remote)} IN ({', '.join(sq(v) for v in lv)}) THEN {sq(lab)}" for lab, lv in parsed) + f" ELSE {_sql_col(remote)} END"
        dialects = [OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=tab),
                    OssieDialectExpression(dialect=OssieDialect.ANSI_SQL, expression=sql)]
        if wh.sqlglot_dialect != "snowflake":
            dialects.append(OssieDialectExpression(dialect=OssieDialect[wh.ossie_dialect],
                                                   expression=sqlglot.parse_one(sql).sql(dialect=wh.sqlglot_dialect)))
        add_field(iname, c, t, dialects, calc)
        losses.append(Loss("categorical_bin", c.get("caption") or iname, f"{len(parsed)} group(s) over {col} rebuilt as a CASE field"))
        return True

    # pass 1: class handling and row-level calculations that can become Ossie fields
    candidates: dict[str, str] = {}
    fixed_fields: set[str] = set()
    raw_candidates: dict[str, re.Match] = {}
    for iname, (c, calc) in calcs.items():
        formula = calc.get("formula", "")
        cls = calc.get("class")
        if cls == "bin" and bin_field(iname, c, calc):
            continue
        if cls == "categorical-bin" and categorical_field(iname, c, calc):
            continue
        if cls not in (None, "tableau"):
            lose(iname, c, formula or f"<{cls} calculation>", CALC_CLASS_LOSS.get(cls, "calc_class"))
            continue
        if not formula:
            continue
        m = masked(formula)
        if RLS_RE.search(m):
            losses.append(Loss("row_level_security", c.get("caption") or iname, formula[:200]))
        if (rm := RAWSQL_RE.match(formula)) and not _calc_aggregated(m):
            raw_candidates[iname] = rm
            continue
        if LOD_FIXED_FIELD.match(m) and iname not in numrec and "Parameters" not in formula_refs(formula):
            candidates[iname] = formula
            fixed_fields.add(iname)
            continue
        if iname in numrec or any(rx.search(m) for _, rx in UNSUPPORTED) or "Parameters" in formula_refs(formula) \
                or _calc_aggregated(m) or RLS_RE.search(m):
            continue
        candidates[iname] = formula

    def rawsql_field(iname: str, rm: re.Match) -> bool:
        """RAWSQL_* over resolvable columns of one dataset becomes a target-dialect field (approximated: the SQL runs unchanged)."""
        refs = formula_refs(rm.group("args"))
        tabs: set[str] = set()
        subs: list[str] = []
        for ref in refs:
            if ref in resolve:
                t, remote = resolve[ref]
                tabs.add(t.name)
                subs.append(f"({_sql_col(remote)})")
            elif (k := internal_of.get(ref)) in emitted:
                et, ename, _dt = emitted[k]
                ef = next((x for x in ds_fields[et.name] if x.name == ename), None)
                d = next((x for pref in (target_dialect, OssieDialect.ANSI_SQL) for x in ef.expression.dialects if x.dialect == pref), None) if ef else None
                if d is None:
                    return False
                tabs.add(et.name)
                subs.append(f"({d.expression})")
            else:
                return False
        if len(tabs) > 1 or not tabs:
            return False
        sql = re.sub(r"%(\d+)", lambda g: subs[int(g.group(1)) - 1] if 0 < int(g.group(1)) <= len(subs) else g.group(0), rm.group("sql"))
        try:
            sqlglot.parse_one(sql, read=wh.sqlglot_dialect)
        except sqlglot.errors.SqlglotError:
            return False
        c = calcs[iname][0]
        add_field(iname, c, next(x for x in tables if x.name in tabs), [OssieDialectExpression(dialect=target_dialect, expression=sql)])
        losses.append(Loss("rawsql_translated", c.get("caption") or iname, f"RAWSQL inner SQL used as the {wh.name} expression: {sql[:150]}"))
        return True

    for iname, rm in list(raw_candidates.items()):
        if rawsql_field(iname, rm):
            del raw_candidates[iname]

    for _ in range(len(candidates) + 1):
        progress = False
        for iname, formula in list(candidates.items()):
            c = calcs[iname][0]
            tabs: set[str] = set()
            ok = True

            def rw(ref: str) -> str | None:
                nonlocal ok
                if ref in resolve:
                    t, remote = resolve[ref]
                    tabs.add(t.name)
                    return f"[{t.field_names[remote]}]"
                k = internal_of.get(ref)
                if k in emitted:
                    tabs.add(emitted[k][0].name)
                    return f"[{emitted[k][1]}]"
                ok = False
                return None
            expr = sub_refs(formula, rw)
            if not ok or (not tabs and (formula_refs(formula) or not tables or iname in fixed_fields)):
                continue
            if not tabs:
                tabs.add(tables[0].name)
            if len(tabs) > 1:
                if iname in fixed_fields:
                    continue
                many = [r.from_dataset for r in relationships if r.from_dataset in tabs and r.to in tabs]
                t = next(x for x in tables if x.name == (many[0] if many else min(tabs, key=lambda n: [x.name for x in tables].index(n))))

                def rwq(ref: str) -> str | None:
                    if ref in resolve:
                        ft, remote = resolve[ref]
                        return f"[{ft.name}].[{ft.field_names[remote]}]"
                    k = internal_of.get(ref)
                    return f"[{emitted[k][0].name}].[{emitted[k][1]}]"
                qexpr = sub_refs(formula, rwq)
                add_field(iname, c, t, [OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=qexpr)])
                losses.append(Loss("row_level_cross_dataset", c.get("caption") or iname,
                                   f"row-level calculation over {', '.join(sorted(tabs))} emitted as a field on {t.name} with qualified references"))
                del candidates[iname]
                progress = True
                continue
            t = next(x for x in tables if x.name in tabs)
            dl = [OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=expr)]
            sq = _sql_dialect(expr, t.name, c.get("caption") or iname, losses)
            if iname in fixed_fields and sq is None:
                del candidates[iname]
                continue
            if sq is not None:
                dl.append(sq)
            add_field(iname, c, t, dl)
            if iname in fixed_fields:
                losses.append(Loss("lod_translated", c.get("caption") or iname, "single-dataset FIXED level-of-detail field emitted with its window SQL"))
            del candidates[iname]
            progress = True
        if not progress or not candidates:
            break

    for iname, rm in list(raw_candidates.items()):
        if rawsql_field(iname, rm):
            del raw_candidates[iname]

    metric_expr: dict[str, str] = {}
    metric_names: dict[str, str] = {}  # calc internal name -> Ossie metric name

    def _ref_is_field_or_metric(ref: str) -> bool:
        if ref in resolve or internal_of.get(ref) in emitted or internal_of.get(ref) in metric_expr:
            return True
        k = internal_of.get(ref)
        if k in calcs:
            _c, _calc = calcs[k]
            return _calc_aggregated(masked(_calc.get("formula", "")))
        return False

    # pass 2: aggregated calculations become metrics, everything else a classified loss
    remaining = [
        iname for iname, (c, calc) in calcs.items()
        if calc.get("class") in (None, "tableau") and calc.get("formula") and iname not in emitted
    ]
    while remaining:
        nxt: list[str] = []
        progress = False
        for iname in remaining:
            c, calc = calcs[iname]
            formula = calc.get("formula", "")
            label = c.get("caption") or iname
            m = masked(formula)
            reasons: list[str] = []
            if iname in numrec:
                mname = _unique(_ident(label), used_m)
                extra = []
                sq = _sql_dialect("COUNT(*)", None, mname, losses)
                if sq is not None:
                    extra.append(sq)
                metrics.append(OssieMetric(
                    name=mname,
                    description=_description(c),
                    expression=OssieExpression(dialects=[OssieDialectExpression(
                        dialect=OssieDialect.TABLEAU, expression="COUNT(*)")] + extra),
                    datatype=TYPE_MAP.get(c.get("datatype") or ""),
                    ai_context=_with_caption_synonym(_read_ai_context(c), mname, c.get("caption")),
                    custom_extensions=_combine_exts(_field_extension(c, folders.get(iname), {
                        "names": stash.pack("field_names", {"name": mname, "label": None}, {"local": iname, "caption": c.get("caption")})}),
                        take_foreign(f"metric:{mname}")),
                ))
                metric_expr[iname] = "COUNT(*)"
                metric_names[iname] = mname
                metric_name_set.add(mname)
                losses.append(Loss("record_count", label, "Tableau auto-column 1 emitted as COUNT(*)"))
                progress = True
                continue
            window_shape = bool(LOD_WINDOW_SHAPE.match(m))
            arith_shape = _viz_arith_formula(m)
            table_calc_shape = bool(TABLE_CALC_METRIC_SHAPE.match(m)) or arith_shape
            viz_shape = bool(LOD_VIZ_SHAPE.match(m)) or arith_shape
            if window_shape and not LOD_VIZ_SHAPE.match(m):
                # proven WINDOW_*/TOTAL pairs keep window SQL + lod_translated even when they also match _TC_ATOM
                table_calc_shape = False
                viz_shape = False
            reasons += [k for k, rx in UNSUPPORTED if rx.search(m)
                        and not (window_shape and k in ("lod", "table_calc"))
                        and not (table_calc_shape and k == "table_calc")
                        and not (viz_shape and k in ("lod", "table_calc"))]
            if "Parameters" in formula_refs(formula):
                reasons.append("parameter_ref")
            if not reasons and not _calc_aggregated(m) and not table_calc_shape and not viz_shape:
                refs = formula_refs(formula)
                if refs and all(_ref_is_field_or_metric(r) for r in refs):
                    if any(
                        internal_of.get(r) in calcs
                        and internal_of.get(r) not in metric_expr
                        and internal_of.get(r) not in emitted
                        and r not in resolve
                        for r in refs
                    ):
                        nxt.append(iname)
                        continue
                else:
                    reasons.append("row_level_calc")
            if not reasons and c.get("datatype") not in (None, "integer", "real"):
                reasons.append("non_numeric_result")
            out = None
            inlined: list[str] = []
            need_sql = False
            if not reasons:
                def sub(ref: str) -> str | None:
                    k = internal_of.get(ref)
                    if ref == OBJECT_ID_REF:
                        reasons.append("record_count")
                        return None
                    hit = resolve.get(ref)
                    if hit is None and k in emitted:
                        t, fname, _dt = emitted[k]
                        return f"[{t.name}].[{fname}]"
                    if hit is None and k in metric_expr:
                        if _inside_aggregate(formula, ref):
                            reasons.append("calc_reference")
                            return None
                        ref_name = metric_names[k]
                        if _is_referenceable_name(ref_name):
                            return ref_name
                        inlined.append(label)
                        return f"({metric_expr[k]})"
                    if hit is None and k in calcs:
                        reasons.append("calc_reference")
                        return None
                    if hit is None:
                        reasons.append("unresolved_reference")
                        return None
                    t, remote = hit
                    return f"[{t.name}].[{t.field_names[remote]}]"

                def resolve_qualified(table: str, col: str) -> str | None:
                    tmatch = next((x for x in tables if x.name.lower() == table.lower() or x.caption.lower() == table.lower()), None)
                    if tmatch is None:
                        reasons.append("unresolved_reference")
                        return None
                    if col in tmatch.field_names:
                        return f"[{tmatch.name}].[{tmatch.field_names[col]}]"
                    hit = resolve.get(col)
                    if hit and hit[0] is tmatch:
                        return f"[{hit[0].name}].[{hit[0].field_names[hit[1]]}]"
                    reasons.append("unresolved_reference")
                    return None

                toks = tokenize(formula)
                parts: list[str] = []
                i = 0
                while i < len(toks):
                    t = toks[i]
                    text = formula[t.start:t.end]
                    if t.kind == "ref" and i + 2 < len(toks) and formula[toks[i + 1].start:toks[i + 1].end] == "." and toks[i + 2].kind == "ref":
                        if t.value == OBJECT_ID_REF:
                            reasons.append("record_count")
                            parts.append(formula[t.start:toks[i + 2].end])
                            i += 3
                            continue
                        q = resolve_qualified(t.value, toks[i + 2].value)
                        parts.append(q if q is not None else formula[t.start:toks[i + 2].end])
                        i += 3
                        continue
                    if t.kind == "ref":
                        r = sub(t.value)
                        parts.append(text if r is None else r)
                    else:
                        parts.append(text)
                    i += 1
                out = "".join(parts)
                if not reasons and AGG_RE.search(BARE_AGG_RE.sub("", m)):
                    if _sql_dialect(out, None, label, []) is not None:
                        pass
                    else:
                        hf = _helper_aggregate(out, tables)
                        sq_in = _sql_dialect(hf[1], hf[2].name, label, losses) if hf else None
                        if hf is not None and sq_in is not None:
                            agg, inner, t0 = hf
                            hname = _unique(f"{_ident(label)}_EXPR", t0.used_fields)
                            ds_fields[t0.name].append(OssieField(
                                name=hname,
                                expression=OssieExpression(dialects=[OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=inner), sq_in]),
                                datatype=TYPE_MAP.get("real"),
                            ))
                            losses.append(Loss("aggregate_expression_helper", label, f"aggregate of an expression emitted as {agg} of helper field {t0.name}.{hname}"))
                            out = f"{agg}([{t0.name}].[{hname}])"
                        else:
                            need_sql = True
                if not reasons:
                    for mm in NUMERIC_AGG_RE.finditer(m):
                        refs_here = formula_refs(formula[mm.start():mm.end()])
                        if not refs_here:
                            continue
                        ref = refs_here[0]
                        hit = resolve.get(ref)
                        if hit:
                            typ = dict(hit[0].columns).get(hit[1], "string")
                        else:
                            typ = emitted[internal_of[ref]][2] if internal_of.get(ref) in emitted else None
                        if typ is not None and typ not in ("integer", "real"):
                            reasons.append("non_numeric_aggregate")
                            break
            if reasons:
                lose(iname, c, formula, reasons[0], row_cause(formula, m) if reasons[0] == "row_level_calc" else None)
                continue
            mname = _unique(_ident(label), used_m)
            mext = _field_extension(c, folders.get(iname), {
                "names": stash.pack("field_names", {"name": mname, "label": None}, {"local": iname, "caption": c.get("caption")})})
            extra: list = []
            if table_calc_shape or viz_shape:
                extra = _sql_dialects(
                    out, tables[0].name if tables else None, mname, losses, quiet=True,
                    order_names=addr.get(iname) or addr.get(label) or addr.get("*"),
                )
                # Brace / {FIXED : AGG} (lod_total) plus SIZE and {FIXED [d]: AGG} * n (it_sql_reconcile). Other viz shapes keep SQL + a loud loss.
                if extra and not (window_shape and "{" in m) and not _RECONCILED_VIZ.match(m):
                    otype = otypes.get(iname) or otypes.get(label) or otypes.get("*")
                    detail = "best-effort window SQL (table-wide OVER); not reconciled against Tableau Compute Using"
                    if otype:
                        detail += f"; TWB ordering-type={otype}"
                    losses.append(Loss("sql_unreconciled", label, detail))
            else:
                sq = _sql_dialect(out, "" if len(tables) > 1 else (tables[0].name if tables else None), mname, losses)
                extra = [sq] if sq is not None else []
                if need_sql and sq is None:
                    losses.append(Loss("sql_untranslated", label, "warehouse SQL could not be produced; TABLEAU expression kept"))
            if window_shape and not viz_shape:
                losses.append(Loss("lod_translated", label, "level-of-detail/TOTAL aggregate emitted with its window SQL" if extra
                                   else "level-of-detail/TOTAL aggregate emitted with the TABLEAU expression only; no SQL form"))
            metric_expr[iname] = out
            metric_names[iname] = mname
            metric_name_set.add(mname)
            if inlined:
                losses.append(Loss("metric_inlined", label, "references another calculation; its aggregate expression is inlined"))
            metrics.append(OssieMetric(
                name=mname,
                description=_description(c),
                expression=OssieExpression(dialects=[OssieDialectExpression(dialect=OssieDialect.TABLEAU, expression=out)] + extra),
                datatype=TYPE_MAP.get(c.get("datatype") or ""),
                ai_context=_with_caption_synonym(_read_ai_context(c), mname, c.get("caption")),
                custom_extensions=_combine_exts(mext, take_foreign(f"metric:{mname}")),
            ))
            progress = True
        if not progress:
            for iname in nxt:
                c, calc = calcs[iname]
                formula = calc.get("formula", "")
                lose(iname, c, formula, "row_level_calc", row_cause(formula, masked(formula)))
            break
        remaining = nxt

    raw: list[dict] = [{"tag": "calc", "xml": _xml(c)} for c in stashed_calcs.values()]
    for g in _kids(ds, "group"):
        raw.append({"tag": "group", "xml": _xml(g)})
    sets = [g for g in _kids(ds, "group") if any(k.endswith("ui-builder") and v == "filter-group" for k, v in g.attrib.items())]
    n = len(_kids(ds, "group")) - len(sets)
    if n:
        losses.append(Loss("group", "", f"{n} element(s) not converted to fields; kept verbatim in the TABLEAU extension", stashed=True))
    for st in sets:
        losses.append(Loss("set", st.get("caption") or st.get("name") or "", "Tableau set (a dynamic in/out membership) has no Ossie type; kept verbatim in the TABLEAU extension", stashed=True))
    for dp_box in _kids(ds, "drill-paths"):
        raw.append({"tag": "drill-paths", "xml": _xml(dp_box)})
        for dp in _kids(dp_box, "drill-path"):
            losses.append(Loss("hierarchy", dp.get("name", ""), f"drill path over {len(list(_kids(dp, 'field')))} field(s) kept in the TABLEAU extension",
                               stashed=True))
    n = len(_kids(ds, "folders-common"))
    if n:
        losses.append(Loss("folder", "", f"{n} folder set(s); membership kept per field in a TABLEAU extension"))
    for f in _kids(ds, "filter"):
        raw.append({"tag": "filter", "xml": _xml(f)})
    n_filters = len(_kids(ds, "filter"))
    if n_filters:
        losses.append(Loss("datasource_filter", "", f"{n_filters} datasource filter(s) not applied in Ossie; kept in the TABLEAU extension", stashed=True))
    for tag, kind in (("default-sorts", "default_sort"), ("field-sort-info", "default_sort"), ("column-instance", "column_instance"),
                      ("semantic-values", "semantic_values")):
        els = _kids(ds, tag)
        for e in els:
            raw.append({"tag": tag, "xml": _xml(e)})
        if els:
            losses.append(Loss(kind, tag, f"{len(els)} <{tag}> element(s) kept in the TABLEAU extension", stashed=True))
    ds_aliases = [a for a in _kids(ds, "aliases") if len(a)]
    for a in ds_aliases:
        raw.append({"tag": "aliases", "xml": _xml(a)})
    col_alias = 0
    for iname, c in colinfo.items():
        for tag in ("aliases", "semantic-values"):
            for e in _kids(c, tag):
                if tag == "aliases" and not len(e):
                    continue
                raw.append({"tag": tag, "column": iname, "xml": _xml(e)})
                if tag == "aliases":
                    col_alias += 1
    if ds_aliases or col_alias:
        losses.append(Loss("aliases", "", f"{len(ds_aliases)} datasource-level and {col_alias} column value alias set(s) kept in the TABLEAU extension",
                           stashed=True))
    used_params = sorted({m for k, (_c, calc) in calcs.items() for m in PARAM_REF_RE.findall(calc.get("formula", ""))}
                         | {_strip(c.get("name", "")) for c in own_params} | bin_params)
    params = list(params or []) + own_params
    if params and used_params:
        by_name = {_strip(p.get("name", "")): p for p in params}
        picked = [by_name[n] for n in used_params if n in by_name]
        for p in picked:
            raw.append({"tag": "parameter", "xml": _xml(p)})
        losses.append(Loss("parameters", ", ".join(used_params), f"{len(picked)} workbook parameter(s) referenced by this datasource kept in the TABLEAU extension", stashed=True))
    elif params:
        losses.append(Loss("parameters_unreferenced", "", f"{len(params)} workbook parameter(s), none referenced by this datasource; not converted"))
    n_sql = [(e.get("name", ""), (_attr(e, "one-time-sql") or "").strip()) for e in ds.iter() if _tag(e) == "connection" and (_attr(e, "one-time-sql") or "").strip()]
    if n_sql:
        seen_sql: set[str] = set()
        for nm, sql in n_sql:
            if sql not in seen_sql:
                seen_sql.add(sql)
                raw.append({"tag": "one-time-sql", "sql": sql, "connection": nm})
        losses.append(Loss("initial_sql", "", f"{len(n_sql)} connection(s) run initial SQL ({len(seen_sql)} distinct); kept in the TABLEAU extension", stashed=True))
    cust = next((e for e in ds.iter() if _tag(e) == "connection-customization"), None)
    if cust is not None:
        raw.append({"tag": "connection-customization", "xml": _xml(cust)})
        losses.append(Loss("connection_customization", "", "driver capability overrides on the connection kept in the TABLEAU extension", stashed=True))
    ex = _kid(ds, "extract")
    if ex is not None:
        raw.append({"tag": "extract", "attrs": dict(ex.attrib)})
        losses.append(Loss("extract", "", "datasource has a Tableau extract; the Ossie model points at the live source (extract settings kept as metadata only)", stashed=True))
    residue_tags: list[str] = []
    for child in list(ds):
        tag = _local_tag(child)
        if tag not in CONSUMED_DS_TAGS:
            raw.append({"tag": tag, "xml": _xml(child)})
            residue_tags.append(tag)
    if residue_tags:
        losses.append(Loss("unmodeled_xml", "", f"unmodeled datasource child tag(s): {', '.join(sorted(set(residue_tags)))}", stashed=True))

    def _keys(t: _Table) -> tuple[list[str] | None, list[list[str]] | None]:
        keys = ds_unique.get(t.name) or []
        return (keys[0] if keys else None), (keys[1:] or None)

    datasets = [
        OssieDataset(name=t.name, source=t.source, fields=ds_fields[t.name] or None,
                     primary_key=_keys(t)[0], unique_keys=_keys(t)[1],
                     ai_context=_with_caption_synonym(t.ai_context, t.name, t.caption, _source_table(t.source)),
                     custom_extensions=ds_ext[t.name])
        for t in tables
    ]
    model_foreign = take_foreign("model")
    for path in foreign_by_path:
        losses.append(Loss("stash_stale", path, f"foreign extension path {path} no longer exists; dropped"))
    ext = {
        "source_file": source_file,
        "datasource": ds_name,
        "connection_classes": sorted(classes),
        "connection": next((c for c in _conn_attrs(ds).values() if by_tableau_class(c.get("class", "")) is not None), next(iter(_conn_attrs(ds).values()), None)),
        "unconverted_calculations": unconverted,
        "root": {"name": _ident(ds_name), "caption": ds.get("caption"), "formatted_name": ds.get("formatted-name"),
                 "source_platform": ds.get("source-platform")},
    }
    if raw:
        ext["raw"] = stash.pack("raw_xml", sorted(t.name for t in tables), raw)
    doc = OssieDocument(
        name=_ident(ds_name),
        description=f"Converted from Tableau datasource {ds_name}",
        ai_context=_read_ai_context(ds),
        datasets=datasets,
        relationships=relationships or None,
        metrics=metrics or None,
        custom_extensions=_combine_exts(
            OssieCustomExtension(vendor_name="TABLEAU", data=json.dumps(ext)),
            model_foreign),
    )
    res.document = doc
    res.stats = {"datasets": len(datasets), "fields": field_count, "relationships": len(relationships), "metrics": len(metrics),
                 "unconverted_calcs": len(unconverted), "row_level_fields": row_fields}
    return res


def _blended_sheets(root: ET.Element) -> dict[str, set[str]]:
    """Worksheet name -> names of the datasources it blends, for worksheets using more than one."""
    names: dict[str, set[str]] = {}
    for ws in root.iter():
        if _tag(ws) != "worksheet":
            continue
        views = [v for v in ws.iter() if _tag(v) == "view"]
        used = {d.get("name") for v in views for dd in _kids(v, "datasources") for d in _kids(dd, "datasource")
                if d.get("name") and d.get("name") != "Parameters"}
        if len(used) > 1:
            names[ws.get("name") or "?"] = used
    return names


def _ds_signature(ds: ET.Element) -> tuple:
    conns = sorted({(c.get("class", ""), c.get("server", ""), c.get("dbname") or c.get("CATALOG") or "", c.get("schema", ""))
                    for c in ds.iter() if _tag(c) == "connection" and c.get("class")})
    tables = sorted({r.get("table", "") for r in ds.iter() if _tag(r) == "relation" and r.get("table")})
    return (tuple(conns), tuple(tables)) if tables else ()


def convert_file(path: str | Path, inline_parameters: bool = False, warehouse: Warehouse | None = None) -> list[Result]:
    p = Path(path)
    members = _read_all_xml(p)
    results: list[Result] = []
    seen: dict[tuple, str] = {}
    for inner, root in members:
        if _tag(root) == "datasource":
            dss = [root]
        else:
            container = _kid(root, "datasources")
            dss = _kids(container, "datasource") if container is not None else []
        params: list[ET.Element] = []
        addr, otypes = table_calc_meta(root)
        for ds in dss:
            if ds.get("name") == "Parameters":
                params = _kids(ds, "column")
        blended = _blended_sheets(root)
        first = len(results)
        ds_names: dict[int, str] = {}
        for ds in dss:
            if ds.get("name") != "Parameters":
                ds_names[len(results)] = ds.get("name", "")
                results.append(convert_datasource(ds, p.name, params, inline_parameters, warehouse, addr, otypes))
                sig = _ds_signature(ds)
                if sig and sig in seen:
                    results[-1].losses.append(Loss("duplicate_datasource", results[-1].datasource,
                                                   f"same connection and tables as data source '{seen[sig]}'; both are converted, consider keeping one"))
                elif sig:
                    seen[sig] = results[-1].datasource
        if len(members) > 1:
            tag = _ident(Path(inner).stem)
            for r in results[first:]:
                r.datasource = f"{tag}__{r.datasource}"
                r.source_file = f"{p.name}:{inner}"
        for i, r in enumerate(results[first:], first):
            sheets = [w for w, used in blended.items() if ds_names.get(i) in used]
            if sheets:
                r.losses.append(Loss("blend", r.datasource,
                                     f"{len(sheets)} worksheet(s) blend this datasource with others (data blending): {', '.join(sheets[:5])}"))
    return results
