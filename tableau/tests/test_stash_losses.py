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

import pytest

from ossie_tableau import cli, stash
from ossie_tableau.convert import Loss
from ossie_tableau.losses import CATALOG, CATEGORIES, KNOWN_KINDS, SEVERITIES, spec

pytestmark = pytest.mark.unit


def test_catalogue_entries_are_valid():
    assert KNOWN_KINDS == set(CATALOG)
    for kind, s in CATALOG.items():
        assert s.category in CATEGORIES and s.severity in SEVERITIES, kind
        assert s.remedy, kind


def test_loss_defaults_come_from_catalogue():
    x = Loss("relationship", "r", "d")
    assert (x.category, x.severity) == ("dropped", "error") and x.remedy and not x.stashed
    assert spec("no_such_kind").severity == "warning"


def test_stash_round_trip_and_witness():
    s = stash.pack("name", {"n": "A", "l": "L"}, {"local": "[A]"})
    assert stash.restore(s, {"l": "L", "n": "A"}, "name") == ({"local": "[A]"}, True)
    assert stash.restore(s, {"n": "B", "l": "L"}) == (None, False)


def test_field_names_witness_matches_leading_underscore_rename():
    s = stash.pack("field_names", {"name": "100_NULL_INDICATOR", "label": None}, {"local": "[100 Null]", "caption": None})
    assert stash.restore(s, {"name": "_100_NULL_INDICATOR", "label": None}, "field_names") == (
        {"local": "[100 Null]", "caption": None}, True)
    assert stash.restore(s, {"name": "OTHER", "label": None}, "field_names") == (None, False)


def test_stash_accepts_json_string_and_rejects_bad_payloads():
    import json

    s = stash.pack("k", 1, 2)
    assert stash.unpack(json.dumps(s))["data"] == 2
    for bad in ("not json", {"x": 1}, {**s, "_v": 99}, {k: v for k, v in s.items() if k != "data"}, [1]):
        with pytest.raises(stash.StashError):
            stash.unpack(bad)
    with pytest.raises(stash.StashError):
        stash.unpack(s, kind="other")


@pytest.fixture
def no_table_ds(tmp_path):
    f = tmp_path / "x.tds"
    f.write_text("<datasource name='d'><connection class='snowflake'/></datasource>")
    return f


def test_strict_flags(no_table_ds, tmp_path, capsys):
    o = str(tmp_path / "o")
    assert cli.main(["convert", str(no_table_ds), "-o", o]) == 0
    assert cli.main(["convert", str(no_table_ds), "-o", o, "--strict"]) == 1
    assert "STRICT: no_tables" in capsys.readouterr().err
    assert cli.main(["convert", str(no_table_ds), "-o", o, "--strict-kinds", "no_tables"]) == 1
    assert cli.main(["convert", str(no_table_ds), "-o", o, "--strict-kinds", "lod"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["convert", str(no_table_ds), "-o", o, "--strict-kinds", "bogus"])


def test_every_emitted_kind_is_in_catalogue():
    import re
    from pathlib import Path

    src = Path(cli.__file__).parent
    emitted = set()
    for p in src.glob("*.py"):
        emitted |= set(re.findall(r"""\bLoss\(\s*["']([a-z_0-9]+)["']""", p.read_text()))
        emitted |= set(re.findall(r"""\b(?:loss|_loss|add_loss|note)\(\s*["']([a-z_0-9]+)["']""", p.read_text()))
    assert emitted, "no Loss(...) call sites found"
    assert emitted <= KNOWN_KINDS, sorted(emitted - KNOWN_KINDS)


def test_every_catalogued_kind_is_reachable():
    import re
    from pathlib import Path

    src = Path(cli.__file__).parent
    text = "".join(p.read_text() for p in src.glob("*.py") if p.name != "losses.py")
    harness_only = {"field_renamed"}
    unused = sorted(k for k in KNOWN_KINDS - harness_only if not re.search(rf"""["']{k}["']""", text))
    assert not unused, unused


@pytest.mark.parametrize("formula,cause", [
    ("ISMEMBEROF('x')", "rls"),
    ("[nope] + 1", "unresolved"),
    ("[Calculation_2] + 1", "depends_on_lost"),
])
def test_row_level_calc_detail_has_subcause(formula, cause):
    from .test_units import BASE, calc, convert_datasource, ds

    extra = calc("WINDOW_STDEV(SUM([amt]))", name="Calculation_2", caption="Lost")
    r = convert_datasource(ds(BASE, extra + calc(formula, name="Calculation_3", caption="X")))
    hit = [x for x in r.losses if x.name == "X" and x.kind == "row_level_calc"]
    assert hit and hit[0].detail.startswith(f"[{cause}]")


def test_group_and_column_instance_are_restored_by_writer():
    from ossie_tableau.convert import convert_datasource
    from ossie_tableau.tds_writer import SnowflakeConn, write_tds
    from .test_units import BASE, ds

    extra = ("<group caption='G' name='[G]' name-style='unqualified'><groupfilter function='union'/></group>"
             "<column-instance column='[amt]' derivation='Sum' name='[sum:amt:qk]' pivot='key' type='quantitative'/>")
    r = convert_datasource(ds(BASE, extra))
    conn = SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")
    xml = write_tds(r.document, conn).xml
    assert "<group" in xml and "<column-instance" in xml
    assert spec("group").category == "preserved" and spec("column_instance").category == "preserved"


def test_dropped_calculations_are_restored_by_writer():
    from ossie_tableau.convert import convert_datasource
    from ossie_tableau.tds_writer import SnowflakeConn, write_tds
    from .test_units import BASE, calc, ds

    f = "WINDOW_STDEV(SUM([amt]))"
    r = convert_datasource(ds(BASE, calc(f, caption="TC") + calc("MODEL_QUANTILE(0.5, SUM([amt]))", name="Calculation_2", caption="Lod")))
    assert {"table_calc"} <= {x.kind for x in r.losses}
    assert all(spec(k).category == "parked" for k in ("table_calc", "lod", "row_level_calc", "calc_reference"))
    xml = write_tds(r.document, SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U")).xml
    assert f in xml and "MODEL_QUANTILE" in xml
    assert xml.count("caption=\"TC\"") == 1


def test_stale_calc_stash_is_not_restored():

    from ossie_tableau.convert import convert_datasource
    from ossie_tableau.tds_writer import SnowflakeConn, write_tds
    from .test_units import BASE, calc, ds

    r = convert_datasource(ds(BASE, calc("WINDOW_STDEV(SUM([amt]))", caption="TC")))
    doc = r.document.model_copy(update={"datasets": [r.document.datasets[0].model_copy(update={"name": "renamed"})]})
    res = write_tds(doc, SnowflakeConn("a.snowflakecomputing.com", "WH", "DB", "SCH", "R", "U"))
    assert "WINDOW_STDEV" not in res.xml and any(x.kind == "stash_stale" for x in res.losses)
