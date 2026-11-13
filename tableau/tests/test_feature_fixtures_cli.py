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

import json
from pathlib import Path

import pytest

from ossie_tableau.cli import main

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent / "fixtures" / "features"
CASES = sorted(p for p in ROOT.iterdir() if p.is_dir())
OK_CASES = [p for p in CASES if not json.loads((p / "expected.json").read_text())["fail"]]
FAIL_CASES = [p for p in CASES if json.loads((p / "expected.json").read_text())["fail"]]


def _src(case):
    return next(p for p in case.iterdir() if p.name.startswith("input."))


def _manifest(out):
    return json.loads((out / "manifest.json").read_text())


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_convert_writes_expected_files_and_manifest(case, tmp_path):
    exp = json.loads((case / "expected.json").read_text())
    assert main(["convert", str(_src(case)), "-o", str(tmp_path)]) == 0
    manifest = _manifest(tmp_path)
    assert len(manifest) == exp["outputs"]
    converted = [m for m in manifest if m["status"] == "converted"]
    assert len(converted) == sum(v["converted"] for v in exp["datasources"].values())
    for m in converted:
        assert (tmp_path / m["output"]).read_text()
        assert (tmp_path / m["output"].replace(".ossie.yaml", ".loss.json")).exists()
    for m in manifest:
        if m["status"] == "skipped":
            assert m["output"] is None
    assert len(list(tmp_path.glob("*.ossie.yaml"))) == len(converted)


@pytest.mark.parametrize("case", FAIL_CASES, ids=lambda p: p.name)
def test_convert_failure_exits_1_and_writes_no_output(case, tmp_path):
    assert main(["convert", str(_src(case)), "-o", str(tmp_path)]) == 1
    assert [m["status"] for m in _manifest(tmp_path)] == ["failed"]
    assert not list(tmp_path.glob("*.ossie.yaml"))
    assert not list(tmp_path.glob("*.loss.json"))


def test_no_connection_fixture_is_skipped_with_no_yaml(tmp_path):
    assert main(["convert", str(_src(ROOT / "no_connection")), "-o", str(tmp_path)]) == 0
    assert [m["status"] for m in _manifest(tmp_path)] == ["skipped"]
    assert not list(tmp_path.glob("*.ossie.yaml"))


def test_multi_inner_outputs_are_prefixed_by_inner_file(tmp_path):
    assert main(["convert", str(_src(ROOT / "multi_inner")), "-o", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.ossie.yaml")) == ["input__a__pg.ossie.yaml", "input__b__pg2.ossie.yaml"]


def test_twbx_and_twb_write_the_same_file_names(tmp_path):
    names = []
    for case in ("tpch_multi", "tpch_multi_twbx"):
        out = tmp_path / case
        assert main(["convert", str(_src(ROOT / case)), "-o", str(out)]) == 0
        names.append(sorted(p.name for p in out.iterdir()))
    assert names[0] == names[1]


@pytest.mark.parametrize("case,kind,rc", [
    ("blend", "blend", 1),
    ("no_connection", "blend", 0),
    ("no_connection", "no_tables", 1),
    ("tpch_multi", "lod", 1),
    ("tpch_multi", "blend", 0),
    ("unknown_warehouse", "unknown_warehouse", 1),
])
def test_strict_kinds_exit_code(case, kind, rc, tmp_path):
    assert main(["convert", str(_src(ROOT / case)), "-o", str(tmp_path), "--strict-kinds", kind]) == rc


def test_strict_kinds_rejects_unknown_kind(tmp_path):
    with pytest.raises(SystemExit) as ei:
        main(["convert", str(_src(ROOT / "blend")), "-o", str(tmp_path), "--strict-kinds", "not_a_kind"])
    assert ei.value.code != 0


@pytest.mark.parametrize("case", OK_CASES, ids=lambda p: p.name)
def test_strict_exit_matches_error_severity_losses(case, tmp_path):
    from ossie_tableau import convert as C

    has_error = any(x.severity == "error" for r in C.convert_file(_src(case)) for x in r.losses)
    assert main(["convert", str(_src(case)), "-o", str(tmp_path), "--strict"]) == (1 if has_error else 0)


def test_no_clobber_refuses_second_run(tmp_path):
    src = str(_src(ROOT / "unknown_warehouse"))
    assert main(["convert", src, "-o", str(tmp_path), "--no-clobber"]) == 0
    before = {p.name: p.read_text() for p in tmp_path.glob("*.ossie.yaml")}
    assert main(["convert", src, "-o", str(tmp_path), "--no-clobber"]) == 1
    assert {p.name: p.read_text() for p in tmp_path.glob("*.ossie.yaml")} == before


def test_quiet_prints_nothing_on_success(tmp_path, capsys):
    assert main(["convert", str(_src(ROOT / "tpch_multi")), "-o", str(tmp_path), "-q"]) == 0
    assert capsys.readouterr().out == ""
