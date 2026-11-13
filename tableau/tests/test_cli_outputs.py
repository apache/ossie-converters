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

"""CLI output naming (no silent overwrites) and to-tds username handling."""
import re
import xml.etree.ElementTree as ET

import pytest
import yaml

from ossie_tableau import cli
from ossie_tableau.convert import convert_datasource

from .test_units import BASE, ds

pytestmark = pytest.mark.unit


def _twb(path, *names):
    body = "".join(ET.tostring(ds(BASE), encoding="unicode").replace("<datasource ", f"<datasource caption='{n}' ", 1) for n in names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<workbook><datasources>{body}</datasources></workbook>")


def _yamls(d):
    return sorted(p.name for p in d.glob("*.ossie.yaml"))


def test_same_named_files_in_different_dirs_do_not_overwrite(tmp_path, capsys):
    _twb(tmp_path / "in" / "dirA" / "sales.twb", "S")
    _twb(tmp_path / "in" / "dirB" / "sales.twb", "S")
    assert cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o")]) == 0
    assert len(_yamls(tmp_path / "o")) == 2
    assert "WARN" in capsys.readouterr().out


def test_datasources_with_same_ident_get_distinct_files(tmp_path, capsys):
    _twb(tmp_path / "in" / "w.twb", "My DS", "My-DS")
    assert cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o")]) == 0
    assert len(_yamls(tmp_path / "o")) == 2
    assert "WARN" in capsys.readouterr().out


def test_rerun_gives_identical_filenames(tmp_path):
    _twb(tmp_path / "in" / "dirA" / "sales.twb", "S")
    _twb(tmp_path / "in" / "dirB" / "sales.twb", "S")
    cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o1")])
    cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o2")])
    assert _yamls(tmp_path / "o1") == _yamls(tmp_path / "o2")


def test_no_warning_without_collision(tmp_path, capsys):
    _twb(tmp_path / "in" / "a.twb", "S")
    _twb(tmp_path / "in" / "b.twb", "S")
    cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o")])
    assert "WARN" not in capsys.readouterr().out


@pytest.mark.parametrize("name,out", [
    ("my.model.ossie.yaml", "my.model.tds"),
    ("Sample Model.ossie.yaml", "Sample Model.tds"),
    ("plain.yaml", "plain.tds"),
    ("x.yml", "x.tds"),
])
def test_to_tds_output_name(tmp_path, name, out):
    from .test_remediation import _sf_ds
    y = tmp_path / name
    y.write_text(yaml.safe_dump(convert_datasource(_sf_ds()).document.model_dump(mode="json", exclude_none=True)))
    assert cli.main(["to-tds", str(y), "-o", str(tmp_path / "o")]) == 0
    assert (tmp_path / "o" / out).exists()


def test_integration_every_datasource_survives_to_tds(tmp_path):
    _twb(tmp_path / "in" / "dirA" / "sales.twb", "S", "T")
    _twb(tmp_path / "in" / "dirB" / "sales.twb", "S", "T")
    cli.main(["convert", str(tmp_path / "in"), "-o", str(tmp_path / "o")])
    ys = sorted((tmp_path / "o").glob("*.ossie.yaml"))
    assert len(ys) == 4
    for y in ys:
        assert cli.main(["to-tds", str(y), "-o", str(tmp_path / "t"), "--server", "a.b"]) == 0
    assert len(list((tmp_path / "t").glob("*.tds"))) == 4


def _saved(tmp_path, username):
    from .test_remediation import _sf_ds
    el = _sf_ds()
    if username:
        el.find("connection").set("username", username)
    y = tmp_path / "m.ossie.yaml"
    y.write_text(yaml.safe_dump(convert_datasource(el).document.model_dump(mode="json", exclude_none=True)))
    return y


def _user(tmp_path):
    xml = (tmp_path / "o" / "m.tds").read_text()
    return re.search(r"<connection [^>]*username='([^']*)'|<connection [^>]*username=\"([^\"]*)\"", xml)


def test_saved_username_written(tmp_path):
    cli.main(["to-tds", str(_saved(tmp_path, "alice")), "-o", str(tmp_path / "o")])
    m = _user(tmp_path)
    assert m and (m.group(1) or m.group(2)) == "alice"


def test_username_flag_overrides(tmp_path):
    cli.main(["to-tds", str(_saved(tmp_path, "alice")), "-o", str(tmp_path / "o"), "--username", "bob"])
    m = _user(tmp_path)
    assert m and (m.group(1) or m.group(2)) == "bob"


def test_no_saved_username_is_empty(tmp_path):
    cli.main(["to-tds", str(_saved(tmp_path, "")), "-o", str(tmp_path / "o")])
    m = _user(tmp_path)
    assert m is None or (m.group(1) or m.group(2) or "") == ""
