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

import zipfile

import pytest

from ossie_tableau import convert as C

pytestmark = pytest.mark.unit

XXE = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "x">]><datasource name="d">&a;</datasource>'


def test_doctype_rejected(tmp_path):
    p = tmp_path / "x.tds"
    p.write_bytes(XXE)
    with pytest.raises(ValueError, match="DOCTYPE"):
        C._read_xml(p)


def test_doctype_rejected_in_archive(tmp_path):
    p = tmp_path / "x.tdsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("x.tds", XXE)
    with pytest.raises(ValueError, match="DOCTYPE"):
        C._read_xml(p)


def test_oversized_member_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "MAX_XML_BYTES", 10)
    p = tmp_path / "x.twbx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("x.twb", "<workbook>" + "a" * 100 + "</workbook>")
    with pytest.raises(ValueError, match="expands"):
        C._read_xml(p)


def test_empty_archive_and_corrupt_zip(tmp_path):
    p = tmp_path / "x.twbx"
    with zipfile.ZipFile(p, "w"):
        pass
    with pytest.raises(ValueError, match="no .twb/.tds"):
        C._read_xml(p)
    bad = tmp_path / "bad.twbx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        C._read_xml(bad)


def test_bad_xml(tmp_path):
    p = tmp_path / "x.tds"
    p.write_text("<datasource")
    with pytest.raises(Exception):
        C._read_xml(p)


PG_TDS = (
    '<datasource name="pg" version="18.1"><connection class="postgres" dbname="d" server="s">'
    '<relation name="t" table="[public].[t]" type="table"/>'
    '<cols><map key="[a]" value="[t].[a]"/></cols></connection>'
    '<column name="[a]" datatype="integer" role="dimension" type="ordinal"/></datasource>'
)


def test_unknown_warehouse_loss_and_cli_flags(tmp_path):
    from ossie_tableau.cli import main

    src = tmp_path / "pg.tds"
    src.write_text(PG_TDS)
    res = C.convert_file(src)
    assert any(x.kind == "unknown_warehouse" for r in res for x in r.losses)

    out = tmp_path / "out"
    out.mkdir()
    assert main(["convert", str(src), "-o", str(out), "-q", "--warehouse-type", "bigquery"]) == 0
    assert main(["convert", str(src), "-o", str(out), "--no-clobber"]) == 1


def test_archive_converts_all_inner_files(tmp_path):
    p = tmp_path / "m.tdsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.tds", PG_TDS)
        z.writestr("b.tds", PG_TDS.replace('name="pg"', 'name="pg2"'))
    res = C.convert_file(p)
    assert len(res) == 2
    assert len({r.datasource for r in res}) == 2


def test_blend_loss(tmp_path):
    ds = lambda n: f'<datasource name="{n}"><connection class="postgres"/></datasource>'
    twb = (f'<workbook><datasources>{ds("one")}{ds("two")}</datasources><worksheets><worksheet name="w"><table><view>'
           '<datasources><datasource name="one"/><datasource name="two"/></datasources></view></table></worksheet></worksheets></workbook>')
    p = tmp_path / "b.twb"
    p.write_text(twb)
    res = C.convert_file(p)
    assert all(any(x.kind == "blend" for x in r.losses) for r in res)


def test_warehouse_override_sets_dialect(tmp_path):
    src = tmp_path / "pg.tds"
    src.write_text(PG_TDS)
    bq = C.convert_file(src, warehouse=C.WAREHOUSES["bigquery"])[0]
    assert not any(x.kind == "unknown_warehouse" for x in bq.losses)


def test_set_is_reported_separately_from_group(tmp_path):
    xml = PG_TDS.replace("</datasource>", "<group caption='S' name='[S]' user:ui-builder='filter-group' xmlns:user='u'/></datasource>")
    xml = xml.replace("<datasource ", "<datasource xmlns:user='u' ", 1).replace(" xmlns:user='u'/>", "/>")
    p = tmp_path / "s.tds"
    p.write_text(xml)
    kinds = [x.kind for x in C.convert_file(p)[0].losses]
    assert "set" in kinds and "group" not in kinds


def test_duplicate_datasource_cross_reference(tmp_path):
    second = PG_TDS.replace('name="pg"', 'name="pg2"')
    wb = "<workbook><datasources>" + PG_TDS + second + "</datasources></workbook>"
    p = tmp_path / "d.twb"
    p.write_text(wb)
    res = C.convert_file(p)
    assert [any(x.kind == "duplicate_datasource" for x in r.losses) for r in res] == [False, True]


@pytest.mark.parametrize("version", ["18.1", "19.1", "20.1", "21.1", "22.1", "23.1", "24.1", "25.1"])
def test_version_spread_converts(tmp_path, version):
    p = tmp_path / "v.tds"
    p.write_text(PG_TDS.replace('version="18.1"', f'version="{version}"'))
    res = C.convert_file(p)
    assert res[0].document is not None


def test_non_utf8_bytes_fail_cleanly(tmp_path):
    p = tmp_path / "x.tds"
    p.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?><datasource name="\xff\xfe"/>')
    with pytest.raises(Exception):
        C.convert_file(p)


def test_traversal_entry_name_is_never_written_to_disk(tmp_path):
    p = tmp_path / "x.tdsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../evil.tds", PG_TDS)
    res = C.convert_file(p)
    assert len(res) == 1
    assert not (tmp_path.parent / "evil.tds").exists()
    from ossie_tableau.cli import main

    out = tmp_path / "out"
    assert main(["convert", str(p), "-o", str(out)]) == 0
    assert all(f.resolve().is_relative_to(out.resolve()) for f in out.iterdir())


def test_blend_loss_only_on_participating_datasources(tmp_path):
    ds = lambda n: f'<datasource name="{n}"><connection class="postgres"/></datasource>'
    sheet = lambda w, *names: (f'<worksheet name="{w}"><table><view><datasources>'
                               + "".join(f'<datasource name="{n}"/>' for n in names) + "</datasources></view></table></worksheet>")
    p = tmp_path / "b.twb"
    p.write_text(f'<workbook><datasources>{ds("one")}{ds("two")}{ds("three")}</datasources>'
                 f'<worksheets>{sheet("w", "one", "two")}{sheet("solo", "three")}</worksheets></workbook>')
    got = {r.datasource: [x.detail for x in r.losses if x.kind == "blend"] for r in C.convert_file(p)}
    assert got["three"] == []
    assert all(len(got[n]) == 1 and "w" in got[n][0] for n in ("one", "two"))


def test_duplicate_detection_spans_archive_members(tmp_path):
    p = tmp_path / "m.tdsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("a.tds", PG_TDS)
        z.writestr("b.tds", PG_TDS.replace('name="pg"', 'name="pg2"'))
    res = C.convert_file(p)
    assert [any(x.kind == "duplicate_datasource" for x in r.losses) for r in res] == [False, True]
