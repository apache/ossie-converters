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

"""Property tests: the TDS writer and the converter reach a structural fixed point on generated models."""
import xml.etree.ElementTree as ET

import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ossie.models import OssieDocument
from ossie_tableau.convert import convert_datasource, convert_file
from ossie_tableau.losses import KNOWN_KINDS
from ossie_tableau.tds_writer import SnowflakeConn, write_tds

pytestmark = pytest.mark.unit
CONN = SnowflakeConn("acct.snowflakecomputing.com", "WH", "DB", "SCH", "R")
SET = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# names without a trailing "_": convert._ident strips it (a known gap until Phase 1 stashes original names)
# Also excluded: SQL keywords as column names (e.g. IF), which the writer reports as untranslatable_expression (known gap).
def _is_plain_column(n):
    try:
        return isinstance(sqlglot.parse_one(n, read="snowflake"), sqlglot.exp.Column)
    except sqlglot.errors.SqlglotError:
        return False


ident = st.from_regex(r"[A-Z]([A-Z0-9_]{0,6}[A-Z0-9])?", fullmatch=True).filter(_is_plain_column)
TYPES = {"String": "string", "Integer": "integer", "Decimal": "real"}


def _ex(e):
    return {"dialects": [{"dialect": "SNOWFLAKE", "expression": e}]}


@st.composite
def documents(draw):
    tables = draw(st.lists(ident, min_size=1, max_size=3, unique=True))
    datasets, cols = [], {}
    for t in tables:
        names = draw(st.lists(ident, min_size=1, max_size=5, unique=True))
        types = [draw(st.sampled_from(sorted(TYPES))) for _ in names]
        cols[t] = list(zip(names, types))
        datasets.append({"name": t, "source": f"DB.SCH.{t}", "fields": [
            {"name": n, "expression": _ex(n), "datatype": ty, **({"dimension": {}} if ty == "String" else {})} for n, ty in cols[t]]})
    rels = []
    for a, b in zip(tables, tables[1:]):
        ca, cb = draw(st.sampled_from(cols[a]))[0], draw(st.sampled_from(cols[b]))[0]
        rels.append({"name": f"{a}_{b}", "from": a, "to": b, "from_columns": [ca], "to_columns": [cb]})
        next(d for d in datasets if d["name"] == b)["primary_key"] = [cb]
    metrics = []
    for t in tables:
        for n, ty in cols[t]:
            if ty != "String" and draw(st.booleans()):
                metrics.append({"name": f"SUM_{t}_{n}", "expression": _ex(f"SUM({t}.{n})")})
    doc = {"name": "gen", "datasets": datasets, "relationships": rels}
    if metrics:
        doc["metrics"] = metrics
    return OssieDocument.model_validate(doc)


@SET
@given(doc=documents())
def test_write_then_convert_is_structural_fixed_point(doc, tmp_path_factory):
    res = write_tds(doc, CONN)
    assert {x.kind for x in res.losses} <= KNOWN_KINDS
    ET.fromstring(res.xml)
    p = tmp_path_factory.mktemp("p") / "g.tds"
    p.write_text(res.xml)
    (r,) = convert_file(p)
    out = r.document
    explained = {x.name for x in res.losses}
    assert [d.name for d in doc.datasets] == [d.name for d in out.datasets]
    for a, b in zip(doc.datasets, out.datasets):
        lost = {n.split(".", 1)[1] for n in explained if n.startswith(a.name + ".")}
        assert {f.name for f in a.fields} - {f.name for f in b.fields or []} <= lost
    assert {(x.from_dataset, x.to) for x in out.relationships or []} >= {(x.from_dataset, x.to) for x in doc.relationships or []} - {
        n for n in explained}
    assert {m.name for m in doc.metrics or []} - {m.name for m in out.metrics or []} <= explained


@st.composite
def legacy_ds(draw):
    tables = draw(st.lists(ident, min_size=1, max_size=3, unique=True))
    _body, recs = "", ""
    cols = {}
    for t in tables:
        cols[t] = draw(st.lists(ident, min_size=1, max_size=4, unique=True))
        types = [draw(st.sampled_from(["string", "integer", "real", "date", "boolean"])) for _ in cols[t]]
        recs += "".join(
            f"<metadata-record class='column'><remote-name>{c}</remote-name><local-name>[{c}]</local-name>"
            f"<parent-name>[{t}]</parent-name><local-type>{ty}</local-type></metadata-record>" for c, ty in zip(cols[t], types))
    rel = f"<relation connection='c' name='{tables[0]}' table='[S].[{tables[0]}]' type='table'/>"
    for a, b in zip(tables, tables[1:]):
        rel = (f"<relation join='inner' type='join'><clause type='join'><expression op='='><expression op='[{a}].[{cols[a][0]}]'/>"
               f"<expression op='[{b}].[{cols[b][0]}]'/></expression></clause>{rel}"
               f"<relation connection='c' name='{b}' table='[S].[{b}]' type='table'/></relation>")
    return ET.fromstring(f"<datasource name='d'><connection class='snowflake'>{rel}<metadata-records>{recs}</metadata-records></connection></datasource>")


@SET
@given(x=legacy_ds())
def test_convert_write_convert_is_fixed_point(x, tmp_path_factory):
    d0 = convert_datasource(x).document
    assert d0 is not None
    res = write_tds(d0, CONN)
    p = tmp_path_factory.mktemp("q") / "g.tds"
    p.write_text(res.xml)
    (r1,) = convert_file(p)
    d1 = r1.document
    assert [d.name for d in d0.datasets] == [d.name for d in d1.datasets]
    explained = {x.name for x in res.losses}
    for a, b in zip(d0.datasets, d1.datasets):
        lost = {n.split(".", 1)[1] for n in explained if n.startswith(a.name + ".")}
        assert {f.name for f in a.fields} - {f.name for f in b.fields or []} <= lost
    assert len(d1.relationships or []) >= len(d0.relationships or []) - len([l for l in res.losses if l.kind == "relationship"])
