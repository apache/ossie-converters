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

"""Reduce a TDS to an order- and id-independent structure so two files can be compared by meaning."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_SUFFIX = re.compile(r"\s*\([^)]*\)\s*(?=\])")


def _key(expr: str) -> str:
    return _SUFFIX.sub("", expr).strip("[]").upper()


def _table(t: str) -> str:
    return t.split(".")[-1].strip("[]").upper()


def normalize(xml: str) -> dict:
    root = ET.fromstring(xml)
    conn = next((c for c in root.iter("connection") if c.get("class") not in (None, "federated")), None)
    attrs = conn.attrib if conn is not None else {}
    objects = {o.get("id"): (o.get("caption") or "").upper() for o in root.iter("object")}
    relations = {((o.get("caption") or "").upper(), _table(r.get("table", "")))
                 for o in root.iter("object") for r in o.iter("relation")}
    relationships = set()
    for rel in root.iter("relationship"):
        ops = [e.get("op", "") for e in rel.find("expression").findall("expression")]
        a, b = (e.get("object-id") for e in (rel.find("first-end-point"), rel.find("second-end-point")))
        relationships.add((objects.get(a, a), _key(ops[0]), objects.get(b, b), _key(ops[1])))
    columns = {}
    for c in root.findall("column"):
        if c.get("datatype") == "table":
            continue
        calc = c.find("calculation")
        desc = " ".join(t.text or "" for t in c.iter("run"))
        columns[_key(c.get("caption") or c.get("name", ""))] = {
            "datatype": c.get("datatype"), "role": c.get("role"), "type": c.get("type"),
            "formula": calc.get("formula") if calc is not None else None, "desc": desc.strip(),
        }
    meta = {(m.findtext("parent-name", "").strip("[]").upper(), (m.findtext("remote-name") or "").upper(),
             m.findtext("local-type")) for m in root.iter("metadata-record")}
    return {
        "connection": {k: attrs.get(k) for k in ("class", "server", "dbname", "schema", "service", "warehouse")},
        "relations": relations, "relationships": relationships, "columns": columns, "metadata": meta,
    }


def yaml_to_tds(yaml_text: str, work, extra: list[str] | None = None):
    """Write `yaml_text` to work/model.ossie.yaml, run `to-tds`, return (exit code, tds path)."""
    from ossie_tableau.cli import main

    work.mkdir(parents=True, exist_ok=True)
    yml = work / "model.ossie.yaml"
    yml.write_text(yaml_text)
    rc = main(["to-tds", str(yml), "-o", str(work)] + (extra or []))
    return rc, work / "model.tds"
