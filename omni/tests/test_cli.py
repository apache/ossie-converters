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

"""Tests for the `ossie-omni` command line, chiefly where export writes files."""

import os

import pytest

from ossie_omni import ConversionError
from ossie_omni._common import contained_output_path, dump_yaml
from ossie_omni.cli import main

MINIMAL = dump_yaml({
    "version": "0.2.0.dev0",
    "name": "m",
    "datasets": [{"name": "orders", "source": "db.sch.orders", "fields": [
        {"name": "amount", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "amount"}]}}]}],
})


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def test_export_writes_the_model_directory(tmp_path):
    src = tmp_path / "model.yaml"
    src.write_text(MINIMAL)
    out = tmp_path / "out"
    assert main(["export", "-i", str(src), "-o", str(out)]) == 0
    assert (out / "views" / "orders.view.yaml").is_file()
    assert (out / "topics" / "m.topic.yaml").is_file()


def test_contained_output_path_joins_inside_the_root(tmp_path):
    assert contained_output_path(str(tmp_path), "views/a.view.yaml") == os.path.join(
        str(tmp_path), "views", "a.view.yaml")


@pytest.mark.parametrize("rel", ["../x.yaml", "a/../../x.yaml"])
def test_contained_output_path_rejects_parent_escape(tmp_path, rel):
    with pytest.raises(ConversionError, match="escapes the output directory"):
        contained_output_path(str(tmp_path / "out"), rel)


def test_contained_output_path_rejects_symlink_escape(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "elsewhere").mkdir()
    _symlink_or_skip(tmp_path / "elsewhere", out / "link")
    with pytest.raises(ConversionError, match="escapes the output directory"):
        contained_output_path(str(out), "link/x.yaml")


def test_export_through_escaping_symlink_fails_without_writing(tmp_path, capsys):
    src = tmp_path / "model.yaml"
    src.write_text(MINIMAL)
    out = tmp_path / "out"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink_or_skip(elsewhere, out / "views")
    assert main(["export", "-i", str(src), "-o", str(out)]) == 1
    assert "escapes the output directory" in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []
    # Checked before anything is written: the topic file is not there either.
    assert sorted(p.name for p in out.iterdir()) == ["views"]
